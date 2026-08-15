"""MiniMax provider — Anthropic SDK against MiniMax's compatible API.

MiniMax recommends the Anthropic SDK for integration:
  https://platform.minimax.io/docs/guides/quickstart-sdk

Base URL:  https://api.minimax.io/anthropic
Auth:      plain API key (no OAuth)
"""

from __future__ import annotations

import importlib
import json
import re
import threading
import uuid
from collections.abc import Generator
from typing import Any, ClassVar, NoReturn
from xml.sax.saxutils import unescape as _xml_unescape

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
    StreamChunk,
    ToolCall,
)
from .anthropic_provider import AnthropicProvider
from .base import LLMProvider

# ---------------------------------------------------------------------------
# Native tool-call recovery (client-side)
#
# The MiniMax Anthropic-compatible endpoint occasionally fails to convert
# the model's native XML tool-call output into structured ``tool_use``
# content blocks — with thinking enabled (M3 ``adaptive`` / M2.x always-on)
# the raw tool-call XML leaks into the text channel verbatim, prefixed by
# the tokenizer special token ``]<]minimax[>[`` (see the MiniMax-M2
# tokenizer_config.json ``added_tokens_decoder``; the whole ``]<]...[>[
# family are special tokens).  The agent loop then sees text with zero tool
# calls, classifies the turn as COMPLETED, and the chat silently stops
# (the "chat bị stop" symptom).
#
# Recovery mirrors what vLLM's ``minimax_m2_tool_parser`` and community
# proxies do: detect the native ``<tool_call>`` XML in the streamed text,
# strip the sentinel tokens, and convert each ``<invoke>`` into structured
# StreamChunks so the loop dispatches them normally.
# ---------------------------------------------------------------------------

#: Special token the model emits before every XML tag of its native
#: tool-call syntax.  ``]<]minimax[>[`` exactly as decoded from the stream.
_MINIMAX_SENTINEL = "]<]minimax[>["

#: Core of the sentinel used for cross-delta prefix matching.  The tolerant
#: strip regex below also eats an optional trailing ``[`` so either the
#: 12- or 13-char rendering is removed.
_SENTINEL_CORE = "]<]minimax[>"

#: Tolerant strip: ``]<]minimax[>`` plus any run of trailing ``[``.
_SENTINEL_RE = re.compile(re.escape(_SENTINEL_CORE) + r"\[*")

#: Native tool-call block openers seen in the wild.  M2 emits the
#: documented ``<minimax:tool_call>``; M3 (after the sentinel is stripped)
#: emits the bare ``<tool_call>`` form.
_TC_OPENERS = ("<tool_call>", "<minimax:tool_call>")
_TC_CLOSERS = ("</tool_call>", "</minimax:tool_call>")

#: Complete ``<tool_call>...</tool_call>`` span (used on finished text).
_TC_SPAN_RE = re.compile(r"<(?:minimax:)?tool_call>.*?</(?:minimax:)?tool_call>", re.DOTALL)

_INVOKE_RE = re.compile(
    r"<invoke\s+name\s*=\s*[\"']?([A-Za-z_][\w.-]*)[\"']?\s*>(.*?)</invoke>",
    re.DOTALL,
)

#: Documented M2 parameter form: ``<parameter name="k">v</parameter>``.
_PARAM_RE = re.compile(
    r"<parameter\s+name\s*=\s*[\"']?([\w.-]+)[\"']?\s*>(.*?)</parameter>",
    re.DOTALL,
)

#: M3 shorthand form: the parameter name *is* the tag (``<address>..</address>``).
#: The leading ``<tag>`` with no attributes keeps it from matching
#: ``<parameter name=...>``.
_GENERIC_PARAM_RE = re.compile(r"<([A-Za-z_][\w.-]*)>(.*?)</\1>", re.DOTALL)


def _strip_sentinel(text: str) -> str:
    """Remove ``]<]minimax[>[`` sentinel tokens from *text*."""
    return _SENTINEL_RE.sub("", text)


def _unescape_value(value: str) -> str:
    """Unescape XML entities in a parameter value, best-effort."""
    try:
        return _xml_unescape(value, {"&quot;": '"', "&apos;": "'"})
    except Exception:  # pragma: no cover - unescape is total for str input
        return value


def _parse_native_invokes(payload: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse ``<invoke>`` blocks from native tool-call XML.

    Handles both parameter spellings (``<parameter name="k">`` and the M3
    ``<k>`` shorthand).  Incomplete invokes — no closing ``</invoke>`` — are
    ignored, so a truncated stream still yields every invoke that closed.
    """
    calls: list[tuple[str, dict[str, Any]]] = []
    for name, body in _INVOKE_RE.findall(payload):
        args: dict[str, Any] = {}
        for pname, pval in _PARAM_RE.findall(body):
            args[pname] = _unescape_value(pval.strip())
        if not args:
            for tag, val in _GENERIC_PARAM_RE.findall(body):
                args[tag] = _unescape_value(val.strip())
        calls.append((name, args))
    return calls


def _clean_complete_text(text: str) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
    """Strip sentinels and extract native tool calls from finished text.

    Returns ``(clean_text, calls)`` where *clean_text* is the user-visible
    prose (sentinels stripped, tool-call spans removed) and *calls* is the
    parsed ``(name, args)`` list.
    """
    calls: list[tuple[str, dict[str, Any]]] = []
    text = _strip_sentinel(text)
    for span in _TC_SPAN_RE.findall(text):
        calls.extend(_parse_native_invokes(span))
    text = _TC_SPAN_RE.sub("", text)
    # Unterminated trailing span (truncated stream): parse whatever invokes
    # closed inside it, then drop the dangling opener and everything after.
    for opener in _TC_OPENERS:
        idx = text.rfind(opener)
        if idx != -1:
            calls.extend(_parse_native_invokes(text[idx:]))
            text = text[:idx]
            break
    return text, calls


def _split_holdback(text: str) -> tuple[str, str]:
    """Split *text* into ``(safe, tail)`` for cross-delta marker detection.

    *tail* is the longest suffix that is a (possibly complete) prefix of the
    sentinel core or a tool-call opener — it may be completed by the next
    delta, so it must not be emitted or searched yet.
    """
    tail_len = 0
    for marker in (_SENTINEL_CORE, *_TC_OPENERS):
        for k in range(1, len(marker) + 1):
            if k > tail_len and text.endswith(marker[:k]):
                tail_len = k
    if not tail_len:
        return text, ""
    return text[:-tail_len], text[-tail_len:]


def _find_first(text: str, markers: tuple[str, ...]) -> tuple[int | None, int]:
    """Earliest occurrence of any *marker* in *text* → ``(index, length)``."""
    best_idx: int | None = None
    best_len = 0
    for m in markers:
        i = text.find(m)
        if i != -1 and (best_idx is None or i < best_idx):
            best_idx, best_len = i, len(m)
    return best_idx, best_len


class _NativeToolCallFilter:
    """Stateful pass-through filter over ``StreamChunk`` text.

    Converts MiniMax native tool-call XML leaked into the text channel into
    structured tool-call chunks.  Plain text (no XML present) passes through
    unchanged, so the normal server-side ``tool_use`` path is unaffected.
    """

    def __init__(self) -> None:
        self._pending = ""  # held-back text (possible split marker)
        self._buf = ""  # text inside an open <tool_call> block
        self._in_tool = False
        # Parsed calls as (id, name, args) for raw_parts rewriting.
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        # Set when the server itself sent structured tool-call chunks; the
        # text filter then stands down entirely to avoid double execution.
        self._server_tool_calls = False

    # -- streaming interface ---------------------------------------------

    def feed(self, chunk: StreamChunk) -> Generator[StreamChunk, None, None]:
        """Process one upstream chunk, yielding filtered chunks."""
        if chunk.is_tool_call_start:
            self._server_tool_calls = True
        if self._server_tool_calls:
            yield chunk
            return
        if chunk.raw_parts is not None:
            yield StreamChunk(raw_parts=self._rewrite_raw_parts(chunk.raw_parts))
            return
        if not chunk.text:
            yield chunk
            return
        yield from self._feed_text(chunk.text)

    def flush(self) -> Generator[StreamChunk, None, None]:
        """Finalise at end of stream: emit held text, salvage truncated XML."""
        if self._pending:
            text, self._pending = self._pending, ""
            clean = _strip_sentinel(text)
            if self._in_tool:
                self._buf += clean
            else:
                idx, olen = _find_first(clean, _TC_OPENERS)
                if idx is None:
                    if clean:
                        yield StreamChunk(text=clean)
                else:
                    if idx:
                        yield StreamChunk(text=clean[:idx])
                    self._buf += clean[idx + olen :]
                    self._in_tool = True
        if self._in_tool and self._buf:
            payload, self._buf = self._buf, ""
            self._in_tool = False
            calls = _parse_native_invokes(payload)
            if calls:
                yield from self._emit_tool_chunks(calls)
            elif payload.strip():
                # Nothing salvageable — surface as text so nothing vanishes.
                yield StreamChunk(text=payload)

    # -- internals ---------------------------------------------------------

    def _feed_text(self, delta: str) -> Generator[StreamChunk, None, None]:
        self._pending += delta
        safe, tail = _split_holdback(self._pending)
        self._pending = tail
        clean = _strip_sentinel(safe)
        if self._in_tool:
            self._buf += clean
            yield from self._drain_buffer()
            return
        idx, olen = _find_first(clean, _TC_OPENERS)
        if idx is None:
            if clean:
                yield StreamChunk(text=clean)
            return
        if idx:
            yield StreamChunk(text=clean[:idx])
        self._in_tool = True
        self._buf = clean[idx + olen :]
        yield from self._drain_buffer()

    def _drain_buffer(self) -> Generator[StreamChunk, None, None]:
        """Emit complete invokes each time a ``</tool_call>`` closer arrives."""
        while True:
            idx, clen = _find_first(self._buf, _TC_CLOSERS)
            if idx is None:
                return
            payload = self._buf[:idx]
            self._buf = self._buf[idx + clen :]
            self._in_tool = False
            calls = _parse_native_invokes(payload)
            if calls:
                yield from self._emit_tool_chunks(calls)
            else:
                log_debug("MiniMax native tool-call block parsed to zero invokes")
            if self._buf:
                rest, self._buf = self._buf, ""
                yield from self._feed_text(rest)
            return

    def _emit_tool_chunks(self, calls: list[tuple[str, dict[str, Any]]]) -> Generator[StreamChunk, None, None]:
        """Convert parsed invokes into start/args/end StreamChunk triplets."""
        for name, args in calls:
            tc_id = f"toolu_mm_{uuid.uuid4().hex[:24]}"
            self.calls.append((tc_id, name, args))
            yield StreamChunk(tool_call_id=tc_id, tool_name=name, is_tool_call_start=True)
            yield StreamChunk(tool_call_id=tc_id, tool_name=name, tool_args_delta=json.dumps(args))
            yield StreamChunk(tool_call_id=tc_id, tool_name=name, tool_args_delta="", is_tool_call_end=True)

    def _rewrite_raw_parts(self, raw_parts: Any) -> Any:
        """Clean text blocks and append our tool_use blocks for the next turn."""
        if not isinstance(raw_parts, list):
            return raw_parts
        has_server_tool_use = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in raw_parts)
        if has_server_tool_use and not self.calls:
            return raw_parts  # server already structured — nothing to rewrite
        out: list[Any] = []
        changed = False
        for block in raw_parts:
            if isinstance(block, dict) and block.get("type") == "text":
                original = block.get("text", "")
                cleaned, _ = _clean_complete_text(original)
                if cleaned != original:
                    changed = True
                if cleaned.strip():
                    out.append({"type": "text", "text": cleaned})
            else:
                out.append(block)
        if not self.calls and not changed:
            return raw_parts
        for tc_id, name, args in self.calls:
            out.append({"type": "tool_use", "id": tc_id, "name": name, "input": args})
        return out


class MiniMaxProvider(AnthropicProvider):
    """MiniMax LLM provider using the Anthropic-compatible API at api.minimax.io."""

    DEFAULT_API_BASE = "https://api.minimax.io/anthropic"

    # Model metadata is not exposed by MiniMax's /anthropic/v1/models endpoint,
    # so we maintain it locally and resolve it by model id.  Values follow the
    # MiniMax Anthropic-compatible Messages API documentation.
    _MODEL_LIMITS: ClassVar[dict[str, dict[str, int]]] = {
        "MiniMax-M3": {
            "context_window": 1_000_000,
            "max_output_tokens": 524_288,
        },
        "MiniMax-M2.7": {
            "context_window": 204_800,
            "max_output_tokens": 204_800,
        },
        "MiniMax-M2.7-highspeed": {
            "context_window": 204_800,
            "max_output_tokens": 204_800,
        },
        "MiniMax-M2.5": {
            "context_window": 204_800,
            "max_output_tokens": 204_800,
        },
        "MiniMax-M2.5-highspeed": {
            "context_window": 204_800,
            "max_output_tokens": 204_800,
        },
        "MiniMax-M2.1": {
            "context_window": 204_800,
            "max_output_tokens": 204_800,
        },
        "MiniMax-M2.1-highspeed": {
            "context_window": 204_800,
            "max_output_tokens": 204_800,
        },
        "MiniMax-M2": {
            "context_window": 204_800,
            "max_output_tokens": 204_800,
        },
    }

    @classmethod
    def _limits_for_model(cls, model_id: str) -> tuple[int, int]:
        """Return ``(context_window, max_output_tokens)`` for a MiniMax model id.

        MiniMax's /anthropic/v1/models endpoint only returns id/display_name
        metadata — no context or output-token limits — so we resolve them from
        the local ``_MODEL_LIMITS`` table.  Unknown ids fall back to the
        documented M2.x defaults.
        """
        if not model_id:
            limits = cls._MODEL_LIMITS["MiniMax-M2.5"]
        else:
            limits = cls._MODEL_LIMITS.get(model_id) or cls._MODEL_LIMITS["MiniMax-M2.5"]
        return limits["context_window"], limits["max_output_tokens"]

    def __init__(
        self,
        api_key: str = "",
        api_base: str = "",
        model: str = "MiniMax-M3",
        **kwargs: Any,
    ) -> None:
        # Bypass AnthropicProvider.__init__ — MiniMax uses plain API keys only,
        # no OAuth keychain lookup.
        LLMProvider.__init__(
            self,
            api_key=api_key,
            api_base=api_base or self.DEFAULT_API_BASE,
            model=model,
        )
        self._auth_type = "api_key"

    @property
    def name(self) -> str:
        return "minimax"

    @property
    def capabilities(self) -> ProviderCapabilities:
        # Documented maximum across the MiniMax family — M3 supports a
        # 1M context window and 524288 output tokens.  We expose the
        # largest supported advertised limit so the UI's spin box can be
        # driven by ``ModelInfo.max_output_tokens`` rather than this value.
        return ProviderCapabilities(
            streaming=True,
            tool_use=True,
            vision=True,  # M3 (largest model) accepts image/video input
            max_context_window=1_000_000,
            max_output_tokens=524_288,
            supports_system_prompt=True,
            supports_cache_control=False,
        )

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                anthropic = importlib.import_module("anthropic")
            except ImportError as exc:
                raise ProviderError(
                    "anthropic package not installed. Run: pip install anthropic",
                    provider="minimax",
                ) from exc
            if not self.api_key:
                raise AuthenticationError(provider="minimax")
            self._client = anthropic.Anthropic(
                api_key=self.api_key,
                base_url=self.api_base,
                timeout=120.0,  # 2min vs SDK default 10min
            )
        return self._client

    def auth_status(self) -> tuple[str, str]:
        if self.api_key:
            return "API Key", "ok"
        return "", "none"

    @staticmethod
    def _builtin_models() -> list[ModelInfo]:
        def _make(model_id: str, display_name: str) -> ModelInfo:
            ctx, max_out = MiniMaxProvider._limits_for_model(model_id)
            return ModelInfo(
                id=model_id,
                name=display_name,
                provider="minimax",
                context_window=ctx,
                max_output_tokens=max_out,
                supports_tools=True,
                supports_vision=model_id == "MiniMax-M3",  # M3 multimodal per docs; M2.x text-only
            )

        return [
            _make("MiniMax-M3", "MiniMax M3"),
            _make("MiniMax-M2.7", "MiniMax M2.7"),
            _make("MiniMax-M2.7-highspeed", "MiniMax M2.7 Highspeed"),
            _make("MiniMax-M2.5", "MiniMax M2.5"),
            _make("MiniMax-M2.5-highspeed", "MiniMax M2.5 Highspeed"),
            _make("MiniMax-M2.1", "MiniMax M2.1"),
            _make("MiniMax-M2.1-highspeed", "MiniMax M2.1 Highspeed"),
            _make("MiniMax-M2", "MiniMax M2"),
        ]

    def _fetch_models_live(self) -> list[ModelInfo]:
        try:
            client = self._get_client()
            response = client.models.list(limit=50)
            models: list[ModelInfo] = []
            for m in response.data:
                model_id = m.id
                if not model_id.lower().startswith("minimax"):
                    continue
                ctx, max_out = self._limits_for_model(model_id)
                models.append(
                    ModelInfo(
                        id=model_id,
                        name=getattr(m, "display_name", None) or model_id,
                        provider="minimax",
                        context_window=ctx,
                        max_output_tokens=max_out,
                        supports_tools=True,
                        supports_vision=model_id == "MiniMax-M3",
                    )
                )
            return models or self._builtin_models()
        except Exception:
            return self._builtin_models()

    def _build_request_kwargs(
        self,
        messages: list[Any],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        max_tokens: int,
        system: str,
        *,
        request_context: LLMRequestContext | None = None,
    ) -> dict[str, Any]:
        """Build request kwargs, stripping cache_control (not supported by MiniMax).

        Additionally enables automatic ``thinking`` for ``MiniMax-M3`` (per the
        MiniMax Anthropic-compatible API docs: ``thinking: {"type": "adaptive"}``).
        M2.x models already have thinking permanently enabled and cannot disable
        it, so no explicit ``thinking`` payload is added for them.

        ``request_context`` is keyword-only and a pure pass-through for
        this provider — the base :meth:`LLMProvider.chat` already merges
        ``context.system_suffix`` into ``system`` and applies any
        ``max_tokens_override`` before this hook is reached, so the wire
        payload is identical with or without a context.
        """
        kwargs = super()._build_request_kwargs(
            messages,
            tools,
            temperature,
            max_tokens,
            system,
            request_context=request_context,
        )

        # System prompt: strip cache_control from blocks
        if isinstance(kwargs.get("system"), list):
            for block in kwargs["system"]:
                block.pop("cache_control", None)
            # If only one plain text block, collapse to a string
            if len(kwargs["system"]) == 1 and kwargs["system"][0].get("type") == "text":
                kwargs["system"] = kwargs["system"][0]["text"]

        # Messages: strip cache_control from content blocks
        for msg in kwargs.get("messages", []):
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        block.pop("cache_control", None)

        # Tools: strip cache_control
        for tool in kwargs.get("tools", []):
            if isinstance(tool, dict):
                tool.pop("cache_control", None)

        # MiniMax-M3 thinking: enabled automatically.  The MiniMax docs only
        # describe the ``adaptive`` mode for M3 — no manual token budget.
        if (self.model or "").strip().lower() == "minimax-m3":
            kwargs["thinking"] = {"type": "adaptive"}

        return kwargs

    def _stream_chunks(
        self,
        client: Any,
        kwargs: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> Generator[StreamChunk, None, None]:
        """Stream with client-side native tool-call recovery.

        Wraps the Anthropic streaming path with :class:`_NativeToolCallFilter`
        so tool-call XML the MiniMax server leaked into the text channel is
        converted to structured tool calls instead of ending the turn.
        """
        flt = _NativeToolCallFilter()
        deferred_raw_parts: StreamChunk | None = None
        for chunk in super()._stream_chunks(client, kwargs, cancel_event=cancel_event):
            if chunk.raw_parts is not None:
                # Hold the raw-parts chunk until after flush: when the stream
                # is truncated mid-block the invokes are only parsed at
                # flush, and the rewrite needs the complete call list.
                deferred_raw_parts = chunk
                continue
            yield from flt.feed(chunk)
        yield from flt.flush()
        if deferred_raw_parts is not None:
            yield from flt.feed(deferred_raw_parts)

    def _normalize_response(self, response: Any) -> Message:
        """Non-streaming variant of the native tool-call recovery.

        Applies the same text cleaning + invoke extraction to a finished
        response so one-shot callers (e.g. bulk rename) also get structured
        tool calls and a clean visible text.
        """
        msg = super()._normalize_response(response)
        if msg.tool_calls:
            return msg  # server converted tool calls properly
        if not msg.content:
            return msg
        cleaned, calls = _clean_complete_text(msg.content)
        if not calls:
            # Still strip stray sentinels so the user never sees them.
            if cleaned != msg.content:
                msg.content = cleaned
            return msg
        msg.content = cleaned
        for name, args in calls:
            tc_id = f"toolu_mm_{uuid.uuid4().hex[:24]}"
            msg.tool_calls.append(ToolCall(id=tc_id, name=name, arguments=args))
        # Keep _raw_parts consistent for the next-turn replay.
        raw_parts = getattr(msg, "_raw_parts", None)
        if isinstance(raw_parts, list) and not any(
            isinstance(b, dict) and b.get("type") == "tool_use" for b in raw_parts
        ):
            rewritten: list[Any] = []
            for block in raw_parts:
                if isinstance(block, dict) and block.get("type") == "text":
                    block_text, _ = _clean_complete_text(block.get("text", ""))
                    if block_text.strip():
                        rewritten.append({"type": "text", "text": block_text})
                else:
                    rewritten.append(block)
            rewritten.extend(
                {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments} for tc in msg.tool_calls
            )
            msg._raw_parts = rewritten
        return msg

    def _handle_api_error(self, e: Exception) -> NoReturn:
        """Translate SDK exceptions to Rikugan errors with MiniMax-aware handling."""
        try:
            anthropic = importlib.import_module("anthropic")
        except ImportError:
            raise ProviderError(str(e), provider="minimax") from e

        # Authentication
        if isinstance(e, anthropic.AuthenticationError):
            raise AuthenticationError(provider="minimax") from e

        # Rate limiting
        if isinstance(e, anthropic.RateLimitError):
            retry_after = 0.0
            resp = getattr(e, "response", None)
            if resp is not None:
                retry_hdr = getattr(resp, "headers", {}).get("retry-after", "")
                try:
                    retry_after = float(retry_hdr)
                except (ValueError, TypeError) as parse_err:
                    log_debug(f"Could not parse retry-after header {retry_hdr!r}: {parse_err}")
            raise RateLimitError(provider="minimax", retry_after=retry_after or 5.0) from e

        # Bad request (context length, etc.) — NOT retryable
        if isinstance(e, anthropic.BadRequestError):
            msg = str(e)
            if "context" in msg.lower() or "token" in msg.lower():
                raise ContextLengthError(str(e), provider="minimax") from e
            raise ProviderError(str(e), provider="minimax") from e

        # Connection errors — RETRYABLE
        if isinstance(e, anthropic.APIConnectionError):
            raise ProviderError(
                f"Connection error: {e}",
                provider="minimax",
                retryable=True,
            ) from e

        # Timeout errors — RETRYABLE
        if isinstance(e, anthropic.APITimeoutError):
            raise ProviderError(
                f"Request timed out: {e}",
                provider="minimax",
                retryable=True,
            ) from e

        # Server errors (500, 502, 503, 504) — RETRYABLE
        if isinstance(e, anthropic.APIStatusError):
            status = getattr(e, "status_code", 0)
            if status >= 500:
                raise ProviderError(
                    f"Server error ({status}): {e}",
                    provider="minimax",
                    status_code=status,
                    retryable=True,
                ) from e
            # Non-retryable status errors
            raise ProviderError(
                f"API error ({status}): {e}",
                provider="minimax",
                status_code=status,
            ) from e

        # Fallback: any other error is NOT retryable
        raise ProviderError(str(e), provider="minimax") from e
