"""OpenAI provider adapter."""

from __future__ import annotations

import importlib
import json
import os
import threading
import uuid
from collections.abc import Generator
from typing import Any, NoReturn

from ..core.errors import (
    AuthenticationError,
    ContextLengthError,
    ProviderError,
    RateLimitError,
)
from ..core.logging import log_debug
from ..core.types import (
    LLMRequestContext,
    Message,
    ModelInfo,
    ProviderCapabilities,
    Role,
    StreamChunk,
    TokenUsage,
    ToolCall,
    coerce_token_count,
)
from .base import LLMProvider


def _format_openai_messages(
    messages: list[Message],
    *,
    include_reasoning_content: bool = False,
) -> list[dict[str, Any]]:
    """Format messages for the OpenAI Chat Completions API wire shape.

    Defensively repairs duplicate or missing ``tool_calls[].id`` values
    before sending the request, because OpenAI rejects requests with
    ``invalid params, duplicate tool_call id`` and older / restored
    sessions may already contain duplicates.

    Strategy:
    - Track every assistant ``tool_calls[].id`` emitted into the outgoing
      request (across all messages, not just the current one).
    - If a new ``tc.id`` is empty or collides with an id already used in
      this request, generate a safe replacement id of the form
      ``call_dedup_<counter>_<short_uuid>`` that cannot collide with any
      other rewritten id.
    - Rewrite the *immediately following* ``Role.TOOL`` message's
      ``tool_results[*].tool_call_id`` values so each tool result still
      references the (possibly rewritten) assistant tool call id.  Original
      ids can collide multiple times in pathological histories, so the
      rewrite uses an ordered queue per original id and pops the first
      unused replacement.

    ``include_reasoning_content`` controls whether assistant messages
    carry the ``reasoning_content`` field.  OpenAI omits it (reasoning is
    only surfaced in streamed responses); GLM includes it so the upstream
    endpoint sees prior thinking context on multi-turn conversations.

    The function does not mutate the input ``Message`` objects.
    """
    formatted: list[dict[str, Any]] = []
    # ``used_ids`` are the assistant ``tool_calls[].id`` values that have
    # already been emitted into ``formatted``.  Used only to detect
    # duplicates within the *outgoing* request (we do not need it to
    # track replacements because the counter suffix makes them unique by
    # construction).
    used_ids: set[str] = set()
    # ``pending_rewrites`` maps an *original* tool_call_id to a queue of
    # replacement ids that the next matching TOOL result messages must
    # consume, in order.  This handles the case where the same original
    # id appears multiple times in the assistant message (we still want
    # each tool result message to point at a unique replacement).
    pending_rewrites: dict[str, list[str]] = {}
    _counter = 0

    def _new_replacement_id() -> str:
        nonlocal _counter
        _counter += 1
        return f"call_dedup_{_counter}_{uuid.uuid4().hex[:8]}"

    for msg in messages:
        if msg.role == Role.SYSTEM:
            formatted.append({"role": "system", "content": msg.content})
        elif msg.role == Role.USER:
            formatted.append({"role": "user", "content": msg.content})
        elif msg.role == Role.ASSISTANT:
            d: dict[str, Any] = {"role": "assistant"}
            if msg.content:
                d["content"] = msg.content
            # GLM carries the prior reasoning trace on the wire so the
            # model can continue multi-turn thinking.  OpenAI never sends
            # this field — its reasoning text only appears in the streamed
            # response.
            if include_reasoning_content and msg.reasoning_content:
                d["reasoning_content"] = msg.reasoning_content
            if msg.tool_calls:
                out_tcs: list[dict[str, Any]] = []
                for tc in msg.tool_calls:
                    original = tc.id or ""
                    if not original or original in used_ids:
                        # Missing or duplicate within the request:
                        # synthesize a fresh, unique id.
                        new_id = _new_replacement_id()
                    else:
                        new_id = original
                    used_ids.add(new_id)
                    # Remember the rewrite so the matching TOOL result(s)
                    # can be updated.  Multiple duplicates of the *same*
                    # original id push multiple replacement ids; the next
                    # matching TOOL result pops them in order.
                    if not original or new_id != original:
                        pending_rewrites.setdefault(original, []).append(new_id)
                    out_tcs.append(
                        {
                            "id": new_id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                    )
                d["tool_calls"] = out_tcs
            formatted.append(d)
        elif msg.role == Role.TOOL:
            for tr in msg.tool_results:
                tr_id = tr.tool_call_id
                rewritten: str | None = None
                if pending_rewrites.get(tr_id):
                    rewritten = pending_rewrites[tr_id].pop(0)
                elif tr_id and tr_id in used_ids:
                    # Id is valid (matches a previous assistant tool_call)
                    # and unique.  No rewrite needed.
                    rewritten = None
                elif not tr_id:
                    # Empty tool_call_id: synthesize a fresh one.
                    rewritten = _new_replacement_id()
                else:
                    # ``tr_id`` does not match any assistant tool_call in
                    # the request.  This is a corrupt / stale history;
                    # rewrite to a fresh id so the request still parses
                    # (better than dropping the result, which would break
                    # tool sequencing).
                    rewritten = _new_replacement_id()
                    log_debug(
                        "_format_openai_messages: tool result with unknown "
                        f"tool_call_id={tr_id!r} was rewritten to {rewritten!r}."
                    )
                out_id = rewritten if rewritten is not None else tr_id
                formatted.append(
                    {
                        "role": "tool",
                        "tool_call_id": out_id,
                        "content": tr.content,
                    }
                )
    return formatted


class OpenAIProvider(LLMProvider):
    """Adapter for the OpenAI Chat Completions API."""

    #: Whether ``__init__`` may fall back to the ``OPENAI_API_KEY``
    #: environment variable when no explicit key is given.  Only the direct
    #: api.openai.com adapter opts in; adapters that reuse the OpenAI
    #: protocol against third-party endpoints (custom base URLs, Z.AI)
    #: override this to ``False`` so the user's real OpenAI key is never
    #: sent to a different host as a bearer credential.
    _ALLOW_OPENAI_ENV_KEY: bool = True

    def __init__(self, api_key: str = "", api_base: str = "", model: str = "gpt-4o", **kwargs: Any) -> None:
        if not api_key and self._ALLOW_OPENAI_ENV_KEY:
            api_key = os.environ.get("OPENAI_API_KEY", "")
        super().__init__(api_key=api_key, api_base=api_base, model=model)

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                openai = importlib.import_module("openai")
            except ImportError as exc:
                raise ProviderError(
                    "openai package not installed. Run: pip install openai",
                    provider="openai",
                ) from exc
            if not self.api_key:
                raise AuthenticationError(provider="openai")
            kwargs = {"api_key": self.api_key, "timeout": 120.0}
            if self.api_base:
                kwargs["base_url"] = self.api_base
            self._client = openai.OpenAI(**kwargs)
        return self._client

    @property
    def name(self) -> str:
        return "openai"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=True,
            tool_use=True,
            vision=True,
            max_context_window=128000,
            max_output_tokens=16384,
            supports_system_prompt=True,
        )

    def _fetch_models_live(self) -> list[ModelInfo]:
        """Fetch chat-capable models from the OpenAI API."""
        client = self._get_client()
        response = client.models.list()
        models = []
        chat_prefixes = ("gpt-", "o1-", "o3-", "o4-", "chatgpt-")
        skip_words = (
            "-instruct",
            "embedding",
            "tts",
            "whisper",
            "dall-e",
            "audio",
            "realtime",
            "transcribe",
        )
        for m in response.data:
            if not any(m.id.startswith(p) for p in chat_prefixes):
                continue
            if any(s in m.id for s in skip_words):
                continue
            models.append(
                ModelInfo(
                    id=m.id,
                    name=m.id,
                    provider="openai",
                    context_window=128000,
                    max_output_tokens=16384,
                    supports_tools=True,
                    supports_vision=True,
                )
            )
        models.sort(key=lambda m: m.id, reverse=True)
        return models if models else self._builtin_models()

    @staticmethod
    def _builtin_models() -> list[ModelInfo]:
        return [
            ModelInfo("gpt-4o", "GPT-4o", "openai", 128000, 16384, True, True),
            ModelInfo("gpt-4o-mini", "GPT-4o Mini", "openai", 128000, 16384, True, True),
            ModelInfo("o3-mini", "o3-mini", "openai", 200000, 100000, True, False),
        ]

    def _format_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Format messages for the OpenAI Chat Completions API.

        Defensively repairs duplicate or missing ``tool_calls[].id``
        values before sending the request, because OpenAI rejects
        requests with ``invalid params, duplicate tool_call id`` and
        older / restored sessions may already contain duplicates.

        The repair logic lives in :func:`_format_openai_messages` (module
        level) so the GLM dialect can reuse it without duplicating the
        tool-ID rewrite.  OpenAI itself does not send ``reasoning_content``
        on the wire (the reasoning text is only surfaced in the streamed
        response via ``delta.reasoning_content``), so
        ``include_reasoning_content`` is False here.

        The function does not mutate the input ``Message`` objects.
        """
        return _format_openai_messages(messages, include_reasoning_content=False)

    def _normalize_response(self, response: Any) -> Message:
        choice = response.choices[0]
        rm = choice.message

        tool_calls = []
        if rm.tool_calls:
            for tc in rm.tool_calls:
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=json.loads(tc.function.arguments),
                    )
                )

        usage = TokenUsage()
        if response.usage:
            usage = TokenUsage(
                prompt_tokens=getattr(response.usage, "prompt_tokens", 0),
                completion_tokens=getattr(response.usage, "completion_tokens", 0),
                total_tokens=getattr(response.usage, "total_tokens", 0),
            )

        # OpenAI o-series reasoning_content
        text = rm.content or ""
        reasoning = getattr(rm, "reasoning_content", None)
        if reasoning:
            text = f"<think>{reasoning}</think>\n{text}"

        return Message(
            role=Role.ASSISTANT,
            content=text,
            tool_calls=tool_calls,
            token_usage=usage,
        )

    def _handle_api_error(self, e: Exception) -> NoReturn:
        try:
            openai = importlib.import_module("openai")
        except ImportError:
            raise ProviderError(str(e), provider="openai") from e
        if isinstance(e, openai.AuthenticationError):
            raise AuthenticationError(provider="openai") from e
        if isinstance(e, openai.RateLimitError):
            raise RateLimitError(provider="openai") from e
        if isinstance(e, openai.BadRequestError):
            msg = str(e)
            if "context" in msg.lower() or "token" in msg.lower():
                raise ContextLengthError(msg, provider="openai") from e
            raise ProviderError(str(e), provider="openai") from e
        # Transient network / server errors — RETRYABLE.
        # APITimeoutError is checked first because the real OpenAI SDK
        # defines ``APITimeoutError`` as a *subclass* of
        # ``APIConnectionError``; checking the connection branch first
        # would mask timeout-specific classification and message.
        if isinstance(e, openai.APITimeoutError):
            raise ProviderError(
                f"Request timed out: {e}",
                provider="openai",
                retryable=True,
            ) from e
        if isinstance(e, openai.APIConnectionError):
            raise ProviderError(
                f"Connection error: {e}",
                provider="openai",
                retryable=True,
            ) from e
        if isinstance(e, openai.APIStatusError):
            status = getattr(e, "status_code", 0)
            if status >= 500:
                raise ProviderError(
                    f"Server error ({status}): {e}",
                    provider="openai",
                    status_code=status,
                    retryable=True,
                ) from e
            raise ProviderError(
                f"API error ({status}): {e}",
                provider="openai",
                status_code=status,
            ) from e
        raise ProviderError(str(e), provider="openai") from e

    def _build_request_kwargs(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        max_tokens: int,
        system: str,
        *,
        request_context: LLMRequestContext | None = None,
    ) -> dict[str, Any]:
        """Build kwargs dict for chat.completions.create.

        ``request_context`` is keyword-only and a pure pass-through for
        this provider — the base :meth:`LLMProvider.chat` already merges
        ``context.system_suffix`` into ``system`` and applies any
        ``max_tokens_override`` before this hook is reached, so for
        non-GLM providers the wire payload is identical with or without
        a context.
        """
        del request_context  # explicitly unused on the OpenAI wire
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(self._format_messages(messages))

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
        return kwargs

    def _call_api(self, client: Any, kwargs: dict[str, Any]) -> Any:
        """Invoke the OpenAI chat.completions.create API."""
        return client.chat.completions.create(**kwargs)

    def _stream_chunks(
        self,
        client: Any,
        kwargs: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> Generator[StreamChunk, None, None]:
        """Yield StreamChunks from the OpenAI streaming API.

        If ``cancel_event`` is set, a watchdog thread force-closes the
        underlying HTTP stream so the consumer's cancellation check fires
        within ~100ms instead of waiting for the next SSE chunk.

        Usage semantics
        ---------------
        OpenAI Chat Completions streams usage as a *cumulative* snapshot
        (each chunk's ``usage.prompt_tokens`` is the full context size
        so far, not a delta).  Yielding one usage ``StreamChunk`` per
        chunk would cause the agent loop accumulator to sum them and
        overcount by N times the number of chunks with usage.

        The previous implementation yielded one usage chunk per chunk
        (including the content-bearing chunks some OpenAI-compatible
        endpoints emit).  This implementation:

        * Tracks the most recently observed cumulative usage in
          ``last_usage_seen`` while content chunks stream past.
        * Yields a single usage ``StreamChunk`` only when we see the
          official final usage-only message (``choices == []`` and
          ``usage`` populated).  The loop exits immediately afterwards
          — there is no further content to stream.
        * If the stream ends without a final usage-only message
          (older endpoints, or 3rd-party proxies that include usage on
          a normal chunk), we yield ``last_usage_seen`` exactly once
          after the stream finishes so the agent loop still records
          the cumulative totals.

        Late-id argument replay
        -----------------------
        Some OpenAI-compatible proxies (and the official OpenAI
        streaming endpoint itself, on rare occasions) emit the first
        ``tool_calls[].function.arguments`` fragment *before* the
        non-empty ``tool_calls[].id`` is announced on a later delta.
        The previous code buffered the early fragment into
        ``current_tool_calls[idx]["args"]`` but skipped emitting a
        ``tool_args_delta`` because ``id`` was still empty — the
        fragment was then permanently lost.

        This implementation tracks ``emitted_args_len`` per index and
        replays the buffered argument text once the id arrives, so
        no JSON argument bytes can be silently dropped.
        """
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}

        try:
            stream = client.chat.completions.create(**kwargs)
        except Exception as e:
            self._handle_api_error(e)
            return

        # Cancel watchdog — closes the stream if cancel_event fires so the
        # consumer's per-chunk cancellation check is reached promptly.  The
        # returned ``done`` event is per-request: set in the finally below so
        # the watchdog exits when the stream finishes instead of parking
        # forever on the long-lived loop cancel event.
        stream_ref: list = []
        stream_ready = threading.Event()
        done = self._spawn_cancel_watchdog(cancel_event, stream_ref, stream_ready)

        stream_ref.append(stream)
        stream_ready.set()

        try:
            yield from self._iter_stream_chunks(stream)
        except Exception as e:
            if cancel_event is not None and cancel_event.is_set():
                log_debug(f"OpenAIProvider stream closed by cancel: {e}")
                return
            raise
        finally:
            done.set()

    def _iter_stream_chunks(
        self,
        stream: Any,
    ) -> Generator[StreamChunk, None, None]:
        """Inner streaming loop, factored out for testability.

        See :meth:`_stream_chunks` for the full contract.  Splitting
        the loop out lets unit tests drive a synthetic iterable of
        chunk-shaped objects without mocking the
        ``client.chat.completions.create`` boundary.
        """
        last_usage_seen: TokenUsage | None = None
        yielded_final_usage = False
        # Track tool-call lifecycle ids so we never emit more than
        # one ``is_tool_call_start`` and one ``is_tool_call_end`` per
        # id, even when the upstream stream repeats the same final
        # state (which is what causes OpenAI's "duplicate tool_call
        # id" 400 in the next request — duplicate ``ToolCall``
        # entries get persisted to the session, then the
        # request-formatter hits the duplicate-id rule).
        started_tool_call_ids: set[str] = set()
        emitted_tool_call_end_ids: set[str] = set()
        last_finish_reason: str | None = None
        current_tool_calls: dict[int, dict] = {}
        _in_reasoning = False

        try:
            for chunk in stream:
                # The official OpenAI final usage-only chunk has
                # ``choices == []`` and a populated ``usage`` field. Some
                # OpenAI-compatible endpoints also surface usage on a
                # normal chunk — capture it but DO NOT yield it as a
                # delta (it is cumulative, so yielding it would
                # overcount when the agent loop accumulates deltas).
                usage_obj = getattr(chunk, "usage", None)
                if usage_obj is not None:
                    pt = coerce_token_count(getattr(usage_obj, "prompt_tokens", 0))
                    ct = coerce_token_count(getattr(usage_obj, "completion_tokens", 0))
                    tt = coerce_token_count(getattr(usage_obj, "total_tokens", 0))
                    last_usage_seen = TokenUsage(
                        prompt_tokens=pt,
                        completion_tokens=ct,
                        total_tokens=tt,
                    )

                if not chunk.choices:
                    # Official final usage-only chunk — yield exactly one
                    # usage StreamChunk and remember we did so.  Skip
                    # content emission; this chunk carries no content.
                    # Guard against duplicates: some OpenAI-compatible
                    # proxies emit the final usage-only chunk more than
                    # once, and the agent loop accumulator would sum
                    # them and overcount.
                    if not yielded_final_usage and last_usage_seen is not None:
                        yield StreamChunk(usage=last_usage_seen)
                        yielded_final_usage = True
                    continue
                delta = chunk.choices[0].delta

                # Reasoning content.  Capability-gated:
                # - Providers that advertise ``reasoning_content`` (GLM)
                #   yield a dedicated ``reasoning_delta`` chunk so the
                #   agent loop can surface the thinking trace separately
                #   from the user-visible text.  No ``<think>`` tags are
                #   injected.
                # - Providers without the capability (plain OpenAI) keep
                #   the legacy inline-``<think>`` behavior so existing
                #   o-series sessions continue to render reasoning inline.
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning and self.capabilities.reasoning_content:
                    yield StreamChunk(reasoning_delta=reasoning)
                elif reasoning:
                    if not _in_reasoning:
                        yield StreamChunk(text="<think>")
                        _in_reasoning = True
                    yield StreamChunk(text=reasoning)
                elif _in_reasoning:
                    yield StreamChunk(text="</think>\n")
                    _in_reasoning = False

                if delta.content:
                    yield StreamChunk(text=delta.content)

                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in current_tool_calls:
                            current_tool_calls[idx] = {
                                "id": tc_delta.id or "",
                                "name": (
                                    tc_delta.function.name if tc_delta.function and tc_delta.function.name else ""
                                ),
                                "args": "",
                                # How much of the buffered argument
                                # string has already been emitted as
                                # a ``tool_args_delta`` chunk.  This
                                # lets us replay early fragments that
                                # arrived before the tool-call id
                                # was known, without ever emitting
                                # the same fragment twice.
                                "emitted_args_len": 0,
                                "started": False,
                            }
                            # Only emit a start chunk for a non-empty
                            # id — an empty id (some proxies defer
                            # the id to a later delta) cannot
                            # meaningfully start a tool call yet.
                            if tc_delta.id and tc_delta.id not in started_tool_call_ids:
                                current_tool_calls[idx]["started"] = True
                                started_tool_call_ids.add(tc_delta.id)
                                yield StreamChunk(
                                    tool_call_id=tc_delta.id,
                                    tool_name=tc_delta.function.name if tc_delta.function else "",
                                    is_tool_call_start=True,
                                )
                        else:
                            # A later delta for an already-known index
                            # may supply a missing id / name (some
                            # OpenAI-compatible proxies send the id
                            # only on the function-arguments delta).
                            # Update the stored info in place so the
                            # eventual end event reports the full
                            # tool call.  When the id was missing on
                            # the first delta, we now have one and
                            # can emit a single ``is_tool_call_start``
                            # chunk — but only once per id.
                            if not current_tool_calls[idx]["id"] and tc_delta.id:
                                current_tool_calls[idx]["id"] = tc_delta.id
                                if tc_delta.id not in started_tool_call_ids:
                                    current_tool_calls[idx]["started"] = True
                                    started_tool_call_ids.add(tc_delta.id)
                                    yield StreamChunk(
                                        tool_call_id=tc_delta.id,
                                        tool_name=current_tool_calls[idx]["name"],
                                        is_tool_call_start=True,
                                    )
                                    # Late-id replay: the buffered
                                    # ``args`` may already hold
                                    # argument bytes that arrived
                                    # before the id.  Emit them now
                                    # so the consumer sees the
                                    # full argument stream, and
                                    # update ``emitted_args_len`` so
                                    # subsequent fragments don't
                                    # double-emit.
                                    buffered = current_tool_calls[idx]["args"]
                                    if buffered:
                                        yield StreamChunk(
                                            tool_call_id=current_tool_calls[idx]["id"],
                                            tool_name=current_tool_calls[idx]["name"],
                                            tool_args_delta=buffered,
                                        )
                                        current_tool_calls[idx]["emitted_args_len"] = len(buffered)
                            if not current_tool_calls[idx]["name"] and tc_delta.function and tc_delta.function.name:
                                current_tool_calls[idx]["name"] = tc_delta.function.name

                        if tc_delta.function and tc_delta.function.arguments:
                            current_tool_calls[idx]["args"] += tc_delta.function.arguments
                            # If the id is already known, emit only
                            # the new portion of the argument
                            # string (anything past
                            # ``emitted_args_len``).  This is what
                            # makes late-id replay safe: an early
                            # fragment buffered before the id is
                            # emitted exactly once when the id
                            # arrives, and later fragments continue
                            # from where we left off.
                            if current_tool_calls[idx]["id"]:
                                buffered = current_tool_calls[idx]["args"]
                                emitted = current_tool_calls[idx]["emitted_args_len"]
                                if len(buffered) > emitted:
                                    delta_args = buffered[emitted:]
                                    current_tool_calls[idx]["emitted_args_len"] = len(buffered)
                                    yield StreamChunk(
                                        tool_call_id=current_tool_calls[idx]["id"],
                                        tool_name=current_tool_calls[idx]["name"],
                                        tool_args_delta=delta_args,
                                    )

                if chunk.choices[0].finish_reason:
                    if _in_reasoning:
                        yield StreamChunk(text="</think>\n")
                        _in_reasoning = False
                    for tc_info in current_tool_calls.values():
                        end_id = tc_info["id"]
                        # Skip end events for empty / duplicate ids:
                        # an empty id means the stream never supplied
                        # a usable tool-call id (broken / non-standard
                        # endpoint) and a duplicate id means we have
                        # already emitted the end event for this
                        # call.  The agent loop guard will also
                        # dedupe, but filtering at the source keeps
                        # the session history clean.
                        if not end_id or end_id in emitted_tool_call_end_ids:
                            if end_id in emitted_tool_call_end_ids:
                                log_debug(
                                    f"OpenAIProvider._stream_chunks: skipping duplicate tool_call_end for {end_id!r}"
                                )
                            continue
                        emitted_tool_call_end_ids.add(end_id)
                        yield StreamChunk(
                            tool_call_id=end_id,
                            tool_name=tc_info["name"],
                            is_tool_call_end=True,
                        )
                    # Also guard against duplicate finish_reason
                    # emissions (some proxies re-emit the same
                    # final chunk).
                    if chunk.choices[0].finish_reason != last_finish_reason:
                        last_finish_reason = chunk.choices[0].finish_reason
                        yield StreamChunk(finish_reason=chunk.choices[0].finish_reason)

        except Exception as e:
            self._handle_api_error(e)
            return

        # If the stream ended without an official final usage-only
        # chunk (older endpoints, proxies that attach usage to content
        # chunks), surface the most recently observed cumulative usage
        # exactly once.  This keeps Anthropic's delta-style and
        # OpenAI's cumulative-style semantics both producing a single
        # usage update for the agent loop accumulator.
        if not yielded_final_usage and last_usage_seen is not None:
            yield StreamChunk(usage=last_usage_seen)
