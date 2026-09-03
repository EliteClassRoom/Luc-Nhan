"""Anthropic Claude provider adapter with OAuth token support."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import threading
from collections.abc import Generator
from typing import Any, NoReturn

from ..core.errors import (
    AuthenticationError,
    ContextLengthError,
    ProviderError,
    RateLimitError,
)
from ..core.logging import log_debug, log_error
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


def _read_oauth_from_keychain() -> str | None:
    """Read the Claude OAuth access token from macOS Keychain.

    `claude setup-token` stores credentials under "Claude Code-credentials"
    as JSON: {"claudeAiOauth": {"accessToken": "sk-ant-oat01-...", ...}}
    """
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                "Claude Code-credentials",
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout.strip())
        return data.get("claudeAiOauth", {}).get("accessToken")
    except Exception:
        return None


def resolve_anthropic_auth(
    api_key: str = "",
    allow_keychain: bool = True,
) -> tuple[str, str]:
    """Resolve the best available Anthropic credential.

    Returns (token, auth_type) where auth_type is "api_key" or "oauth".
    Priority:
      1. Explicit api_key argument
      2. ANTHROPIC_API_KEY env var
      3. CLAUDE_CODE_OAUTH_TOKEN env var
      4. OAuth token from macOS Keychain (requires *allow_keychain*)
    """
    # Explicit key or env var
    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        if key.startswith("sk-ant-oat"):
            return key, "oauth"
        return key, "api_key"

    # CLAUDE_CODE_OAUTH_TOKEN env var (claude setup-token pattern)
    oauth_env = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if oauth_env:
        return oauth_env, "oauth"

    # macOS Keychain — only if the user has accepted the OAuth consent
    if allow_keychain:
        oauth = _read_oauth_from_keychain()
        if oauth:
            return oauth, "oauth"

    return "", ""


class AnthropicProvider(LLMProvider):
    """Adapter for the Anthropic Messages API.

    Supports both API key and OAuth token authentication.
    OAuth tokens (from `claude setup-token`) are auto-detected from
    the macOS Keychain when no explicit API key is provided.
    """

    def __init__(
        self,
        api_key: str = "",
        api_base: str = "",
        model: str = "claude-sonnet-4-20250514",
        **kwargs: Any,
    ) -> None:
        if api_key:
            token, self._auth_type = resolve_anthropic_auth(api_key)
        else:
            # Go through the cache, which respects OAuth consent.
            from .auth_cache import resolve_auth_cached

            token, self._auth_type = resolve_auth_cached()
        super().__init__(api_key=token, api_base=api_base, model=model)

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                anthropic = importlib.import_module("anthropic")
            except ImportError as exc:
                raise ProviderError(
                    "anthropic package not installed. Run: pip install anthropic",
                    provider="anthropic",
                ) from exc
            if not self.api_key:
                raise AuthenticationError("No Anthropic credential found")  # guidance auto-appended from _AUTH_GUIDANCE
            # OAuth tokens use Bearer auth + beta header;
            # API keys use x-api-key header.
            kwargs: dict[str, Any] = {}
            if self.api_base:
                kwargs["base_url"] = self.api_base
            kwargs["timeout"] = 120.0  # 2min vs SDK default 10min
            if self._auth_type == "oauth":
                kwargs["auth_token"] = self.api_key
                kwargs["default_headers"] = {
                    "anthropic-beta": "oauth-2025-04-20,claude-code-20250219",
                }
                self._client = anthropic.Anthropic(**kwargs)
            else:
                kwargs["api_key"] = self.api_key
                self._client = anthropic.Anthropic(**kwargs)
        return self._client

    @property
    def name(self) -> str:
        return "anthropic"

    @property
    def auth_type(self) -> str:
        return self._auth_type

    def auth_status(self) -> tuple[str, str]:
        if self.api_key:
            if self._auth_type == "oauth":
                return "OAuth", "ok"
            return "API Key", "ok"
        return "No key", "error"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=True,
            tool_use=True,
            vision=True,
            max_context_window=200000,
            max_output_tokens=16384,
            supports_system_prompt=True,
            supports_cache_control=True,
        )

    def _fetch_models_live(self) -> list[ModelInfo]:
        """Fetch models from the Anthropic API."""
        client = self._get_client()
        response = client.models.list(limit=100)
        models = []
        for m in response.data:
            model_id = m.id
            display_name = getattr(m, "display_name", model_id)
            # API doesn't return context/output limits; use known defaults
            is_opus = "opus" in model_id
            ctx_window = 200000
            max_output = 16384 if is_opus else 8192
            models.append(
                ModelInfo(
                    id=model_id,
                    name=display_name,
                    provider="anthropic",
                    context_window=ctx_window,
                    max_output_tokens=max_output,
                    supports_tools=True,
                    supports_vision=True,
                )
            )
        # Sort: newest/best first
        models.sort(key=lambda m: m.id, reverse=True)
        return models if models else self._builtin_models()

    @staticmethod
    def _builtin_models() -> list[ModelInfo]:
        return [
            ModelInfo(
                "claude-sonnet-4-6",
                "Claude Sonnet 4.6",
                "anthropic",
                200000,
                8192,
                True,
                True,
            ),
            ModelInfo(
                "claude-opus-4-6",
                "Claude Opus 4.6",
                "anthropic",
                200000,
                16384,
                True,
                True,
            ),
            ModelInfo(
                "claude-opus-4-20250514",
                "Claude Opus 4",
                "anthropic",
                200000,
                16384,
                True,
                True,
            ),
            ModelInfo(
                "claude-sonnet-4-20250514",
                "Claude Sonnet 4",
                "anthropic",
                200000,
                8192,
                True,
                True,
            ),
            ModelInfo(
                "claude-haiku-4-5-20251001",
                "Claude Haiku 4.5",
                "anthropic",
                200000,
                8192,
                True,
                True,
            ),
        ]

    def _format_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Convert to Anthropic's messages format.

        For assistant messages, if ``_raw_parts`` is a valid Anthropic-shaped
        ``list[dict]`` (collected during a previous streaming or non-streaming
        call from an Anthropic-compatible provider such as MiniMax), replay
        those blocks as-is so ``signature`` fields on ``thinking`` blocks and
        the exact ``id`` / ``name`` ordering of ``tool_use`` blocks survive
        the round trip.  This matters for MiniMax-M3: the provider requires
        thinking signatures on follow-up tool-use turns, and reconstructing
        content from our internal ``content`` + ``tool_calls`` would strip
        them.

        The guard (``isinstance(raw_parts, list)`` + ``all dict`` +
        recognised ``type``) deliberately rejects Gemini-shaped raw parts
        (``list[genai_types.Part]``) so an Anthropic provider never tries to
        forward Gemini SDK objects as Anthropic content blocks.
        """
        # ``content`` is either a plain string (user/assistant text) or a
        # list of provider-native content blocks (assistant tool_use /
        # replayed raw parts). The explicit ``object`` value type lets mypy
        # accept both shapes instead of inferring ``dict[str, str]`` from
        # the first append and then rejecting the list-typed appends.
        formatted: list[dict[str, object]] = []
        for msg in messages:
            if msg.role == Role.SYSTEM:
                continue  # System goes in the `system` param

            if msg.role == Role.USER:
                formatted.append({"role": "user", "content": msg.content})

            elif msg.role == Role.ASSISTANT:
                raw_parts = getattr(msg, "_raw_parts", None)
                if self._is_valid_anthropic_raw_parts(raw_parts):
                    # Replay the original Anthropic-shaped block list
                    # (thinking + text + tool_use with signatures).
                    # ``_is_valid_anthropic_raw_parts`` guarantees *raw_parts*
                    # is a non-empty ``list[dict]``; the isinstance check
                    # lets mypy narrow the ``Any`` return of ``getattr`` so
                    # ``list(...)`` type-checks without an ignore.
                    assert isinstance(raw_parts, list)
                    formatted.append({"role": "assistant", "content": list(raw_parts)})
                    continue

                content: list = []
                if msg.content:
                    content.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    content.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    )
                formatted.append(
                    {"role": "assistant", "content": content or msg.content}  # type: ignore[dict-item]
                )

            elif msg.role == Role.TOOL:
                for tr in msg.tool_results:
                    formatted.append(
                        {
                            "role": "user",
                            "content": [  # type: ignore[dict-item]
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tr.tool_call_id,
                                    "content": tr.content,
                                    "is_error": tr.is_error,
                                }
                            ],
                        }
                    )

        return formatted

    @staticmethod
    def _is_valid_anthropic_raw_parts(raw_parts: Any) -> bool:
        """True if *raw_parts* is an Anthropic-shaped ``list[dict]`` we can replay.

        Accepts a non-empty list of plain ``dict`` blocks whose ``type`` is
        one of the recognised Anthropic content-block types.  This rules
        out Gemini SDK ``Part`` objects and other provider-native types.
        """
        if not isinstance(raw_parts, list) or not raw_parts:
            return False
        valid_types = {"thinking", "text", "tool_use", "tool_result"}
        for block in raw_parts:
            if not isinstance(block, dict):
                return False
            if block.get("type") not in valid_types:
                return False
        return True

    @staticmethod
    def _block_to_dict(block: Any) -> dict[str, Any] | None:
        """Convert an Anthropic SDK content block to an Anthropic-shaped dict.

        Returns ``None`` for unknown block types so the caller can skip them
        rather than forwarding an unrecognised shape to the API.
        """
        btype = getattr(block, "type", None)
        if btype == "thinking":
            out: dict[str, Any] = {
                "type": "thinking",
                "thinking": getattr(block, "thinking", "") or "",
            }
            signature = getattr(block, "signature", None)
            if signature:
                out["signature"] = signature
            return out
        if btype == "text":
            return {"type": "text", "text": getattr(block, "text", "") or ""}
        if btype == "tool_use":
            tool_input = getattr(block, "input", None)
            if isinstance(tool_input, str):
                try:
                    tool_input = json.loads(tool_input) if tool_input else {}
                except json.JSONDecodeError:
                    tool_input = {}
            elif not isinstance(tool_input, dict):
                tool_input = {}
            return {
                "type": "tool_use",
                "id": getattr(block, "id", ""),
                "name": getattr(block, "name", ""),
                "input": tool_input,
            }
        return None

    def _format_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert OpenAI-style tool schemas to Anthropic format."""
        anthropic_tools = []
        for t in tools:
            func = t.get("function", t)
            anthropic_tools.append(
                {
                    "name": func["name"],
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
                }
            )
        return anthropic_tools

    def _normalize_response(self, response: Any) -> Message:
        """Convert Anthropic response to internal Message."""
        content_text = ""
        tool_calls = []
        raw_blocks: list[dict[str, Any]] = []

        for block in response.content:
            if block.type == "thinking":
                content_text += f"<think>{block.thinking}</think>\n"
            elif block.type == "text":
                content_text += block.text
            elif block.type == "tool_use":
                tool_input = block.input
                if isinstance(tool_input, str):
                    try:
                        tool_input = json.loads(tool_input) if tool_input else {}
                    except json.JSONDecodeError:
                        tool_input = {}
                elif not isinstance(tool_input, dict):
                    tool_input = {}
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=tool_input,
                    )
                )
            # Preserve the Anthropic-shaped block list for the next turn's
            # request, so thinking signatures and tool_use id/name ordering
            # round-trip intact (required by MiniMax-M3).
            block_dict = self._block_to_dict(block)
            if block_dict is not None:
                raw_blocks.append(block_dict)

        usage_obj = getattr(response, "usage", None)
        if usage_obj is None:
            usage = TokenUsage()
        else:
            prompt = coerce_token_count(getattr(usage_obj, "input_tokens", 0))
            completion = coerce_token_count(getattr(usage_obj, "output_tokens", 0))
            usage = TokenUsage(
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=prompt + completion,
                cache_read_tokens=coerce_token_count(getattr(usage_obj, "cache_read_input_tokens", 0)),
                cache_creation_tokens=coerce_token_count(getattr(usage_obj, "cache_creation_input_tokens", 0)),
            )

        msg = Message(
            role=Role.ASSISTANT,
            content=content_text,
            tool_calls=tool_calls,
            token_usage=usage,
        )
        if raw_blocks:
            msg._raw_parts = raw_blocks
        return msg

    def _handle_api_error(self, e: Exception) -> NoReturn:
        """Raise the appropriate Rikugan error from an Anthropic API error."""
        try:
            anthropic = importlib.import_module("anthropic")
        except ImportError:
            raise ProviderError(str(e), provider="anthropic") from e

        if isinstance(e, anthropic.AuthenticationError):
            raise AuthenticationError(provider="anthropic") from e
        if isinstance(e, anthropic.RateLimitError):
            retry_after = 0.0
            # Try to extract retry-after from response headers
            resp = getattr(e, "response", None)
            if resp is not None:
                retry_hdr = getattr(resp, "headers", {}).get("retry-after", "")
                try:
                    retry_after = float(retry_hdr)
                except (ValueError, TypeError) as parse_err:
                    log_debug(f"Could not parse retry-after header {retry_hdr!r}: {parse_err}")
            raise RateLimitError(provider="anthropic", retry_after=retry_after or 5.0) from e
        if isinstance(e, anthropic.BadRequestError):
            msg = str(e)
            if "context" in msg.lower() or "token" in msg.lower():
                raise ContextLengthError(str(e), provider="anthropic") from e
            raise ProviderError(str(e), provider="anthropic") from e

        # Connection errors — RETRYABLE
        if isinstance(e, anthropic.APIConnectionError):
            raise ProviderError(
                f"Connection error: {e}",
                provider="anthropic",
                retryable=True,
            ) from e

        # Timeout errors — RETRYABLE
        if isinstance(e, anthropic.APITimeoutError):
            raise ProviderError(
                f"Request timed out: {e}",
                provider="anthropic",
                retryable=True,
            ) from e

        # Server errors (500+) — RETRYABLE
        if isinstance(e, anthropic.APIStatusError):
            status = getattr(e, "status_code", 0)
            if status >= 500:
                raise ProviderError(
                    f"Server error ({status}): {e}",
                    provider="anthropic",
                    status_code=status,
                    retryable=True,
                ) from e
            raise ProviderError(
                f"API error ({status}): {e}",
                provider="anthropic",
                status_code=status,
            ) from e

        raise ProviderError(str(e), provider="anthropic") from e

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
        """Build kwargs dict for messages.create/stream.

        ``request_context`` is keyword-only and a pure pass-through for
        this provider — the base :meth:`LLMProvider.chat` already merges
        ``context.system_suffix`` into ``system`` and applies any
        ``max_tokens_override`` before this hook is reached, so the wire
        payload is identical with or without a context.
        """
        del request_context  # explicitly unused on the Anthropic wire
        formatted_messages = self._format_messages(messages)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": formatted_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        # System prompt with cache_control for prompt caching
        if system:
            system_blocks: list[dict[str, Any]] = []
            # OAuth billing attribution — required by Anthropic for
            # Claude Code subscription tokens.
            if self._auth_type == "oauth":
                system_blocks.append(
                    {
                        "type": "text",
                        "text": ("x-anthropic-billing-header: cc_version=2.1.77; cc_entrypoint=cli; cch=00000;"),
                    }
                )
            system_blocks.append(
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            )
            kwargs["system"] = system_blocks

        if tools:
            formatted_tools = self._format_tools(tools)
            # Mark the last tool with cache_control so the full tool list is cached
            if formatted_tools:
                formatted_tools[-1]["cache_control"] = {"type": "ephemeral"}
            kwargs["tools"] = formatted_tools

        # Mark the last user message with cache_control to cache conversation history
        # (only if there are enough messages for caching to be worthwhile)
        if len(formatted_messages) >= 4:
            last_msg = formatted_messages[-1]
            if isinstance(last_msg.get("content"), list) and last_msg["content"]:
                last_msg["content"][-1]["cache_control"] = {"type": "ephemeral"}
            elif isinstance(last_msg.get("content"), str):
                # Convert string content to block format for cache_control
                last_msg["content"] = [
                    {
                        "type": "text",
                        "text": last_msg["content"],
                        "cache_control": {"type": "ephemeral"},
                    }
                ]

        return kwargs

    def _call_api(self, client: Any, kwargs: dict[str, Any]) -> Any:
        """Invoke the Anthropic messages.create API."""
        return client.messages.create(**kwargs)

    def _stream_chunks(
        self,
        client: Any,
        kwargs: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> Generator[StreamChunk, None, None]:
        """Yield StreamChunks from the Anthropic streaming API.

        If ``cancel_event`` is set, a watchdog thread force-closes the
        underlying HTTP stream so the consumer's cancellation check fires
        within ~100ms instead of waiting for the next SSE chunk.
        """
        stream_ref: list = []
        stream_ready = threading.Event()
        # Per-request watchdog: force-closes the stream when cancel_event
        # fires; ``done`` (set in the finally below) makes it exit when the
        # stream finishes instead of parking forever on the loop cancel event.
        done = self._spawn_cancel_watchdog(cancel_event, stream_ref, stream_ready)

        try:
            with client.messages.stream(**kwargs) as stream:
                stream_ref.append(stream)
                stream_ready.set()
                current_tool_id = None
                current_tool_name = None

                in_thinking = False

                # Anthropic-shaped raw content block list collected in
                # parallel with the UI text/tool chunks.  Emitted as a
                # final ``StreamChunk(raw_parts=...)`` once the stream
                # ends so the agent loop can attach it to the assistant
                # ``Message._raw_parts`` for later replay.
                raw_blocks: list[dict[str, Any]] = []
                # Working buffer for the block currently being built.
                _raw_text: list[str] = []
                _raw_thinking: list[str] = []
                _raw_signature: str | None = None
                _raw_tool_input_parts: list[str] = []
                _raw_tool_id: str | None = None
                _raw_tool_name: str | None = None

                def _flush_current_block() -> None:
                    """Append the in-progress block (if any) to ``raw_blocks``."""
                    nonlocal _raw_text, _raw_thinking, _raw_signature
                    nonlocal _raw_tool_input_parts, _raw_tool_id, _raw_tool_name
                    if _raw_text:
                        raw_blocks.append({"type": "text", "text": "".join(_raw_text)})
                    elif _raw_thinking or _raw_signature is not None:
                        thinking_block: dict[str, Any] = {
                            "type": "thinking",
                            "thinking": "".join(_raw_thinking),
                        }
                        if _raw_signature:
                            thinking_block["signature"] = _raw_signature
                        raw_blocks.append(thinking_block)
                    elif _raw_tool_id is not None:
                        raw_input = "".join(_raw_tool_input_parts)
                        parsed_input: Any = {}
                        if raw_input:
                            try:
                                parsed_input = json.loads(raw_input)
                            except json.JSONDecodeError as je:
                                # Malformed partial JSON — degrade gracefully
                                # to an empty input rather than raising.  The
                                # existing tool-call path already tolerates
                                # empty arguments, so the downstream request
                                # will still go through.
                                log_debug(
                                    f"AnthropicProvider raw block: tool_use JSON parse failed "
                                    f"(id={_raw_tool_id}, name={_raw_tool_name}): {je}"
                                )
                                parsed_input = {}
                        raw_blocks.append(
                            {
                                "type": "tool_use",
                                "id": _raw_tool_id,
                                "name": _raw_tool_name or "",
                                "input": parsed_input if isinstance(parsed_input, dict) else {},
                            }
                        )
                    # Reset working buffers
                    _raw_text = []
                    _raw_thinking = []
                    _raw_signature = None
                    _raw_tool_input_parts = []
                    _raw_tool_id = None
                    _raw_tool_name = None

                for event in stream:
                    etype = event.type

                    if etype == "content_block_start":
                        block = event.content_block
                        if block.type == "tool_use":
                            current_tool_id = block.id
                            current_tool_name = block.name
                            _raw_tool_id = block.id
                            _raw_tool_name = block.name
                            yield StreamChunk(
                                tool_call_id=block.id,
                                tool_name=block.name,
                                is_tool_call_start=True,
                            )
                        elif block.type == "thinking":
                            in_thinking = True
                            yield StreamChunk(text="<think>\n")
                        elif block.type == "text":
                            if block.text:
                                _raw_text.append(block.text)
                                yield StreamChunk(text=block.text)

                    elif etype == "content_block_delta":
                        delta = event.delta
                        if delta.type == "thinking_delta":
                            _raw_thinking.append(delta.thinking)
                            yield StreamChunk(text=delta.thinking)
                        elif delta.type == "text_delta":
                            _raw_text.append(delta.text)
                            yield StreamChunk(text=delta.text)
                        elif delta.type == "signature_delta":
                            # Anthropic emits a separate ``signature_delta``
                            # event for the thinking block's signature.
                            _raw_signature = getattr(delta, "signature", None)
                        elif delta.type == "input_json_delta":
                            _raw_tool_input_parts.append(delta.partial_json)
                            yield StreamChunk(
                                tool_call_id=current_tool_id,
                                tool_name=current_tool_name,
                                tool_args_delta=delta.partial_json,
                            )

                    elif etype == "content_block_stop":
                        _flush_current_block()
                        if in_thinking:
                            yield StreamChunk(text="\n</think>\n")
                            in_thinking = False
                        elif current_tool_id:
                            yield StreamChunk(
                                tool_call_id=current_tool_id,
                                tool_name=current_tool_name,
                                tool_args_delta="",
                                is_tool_call_end=True,
                            )
                            current_tool_id = None
                            current_tool_name = None

                    elif etype == "message_delta":
                        sr = getattr(event, "delta", None)
                        if sr and hasattr(sr, "stop_reason"):
                            yield StreamChunk(finish_reason=sr.stop_reason)
                        # Capture final output_tokens from message_delta usage
                        usage_delta = getattr(event, "usage", None)
                        if usage_delta is not None:
                            output_tokens = coerce_token_count(getattr(usage_delta, "output_tokens", 0))
                            if output_tokens > 0:
                                yield StreamChunk(
                                    usage=TokenUsage(
                                        prompt_tokens=0,
                                        completion_tokens=output_tokens,
                                    )
                                )

                    elif etype == "message_start":
                        msg = event.message
                        msg_usage = getattr(msg, "usage", None)
                        if msg_usage is not None:
                            yield StreamChunk(
                                usage=TokenUsage(
                                    prompt_tokens=getattr(msg_usage, "input_tokens", 0),
                                    completion_tokens=0,
                                    cache_read_tokens=coerce_token_count(
                                        getattr(msg_usage, "cache_read_input_tokens", 0)
                                    ),
                                    cache_creation_tokens=coerce_token_count(
                                        getattr(msg_usage, "cache_creation_input_tokens", 0)
                                    ),
                                )
                            )

                # Final flush: in case the stream ended without an explicit
                # content_block_stop for the last block (defensive).
                _flush_current_block()

                # Emit the complete raw block list exactly once at the end
                # of the stream.  The agent loop in
                # ``rikugan/agent/loop.py`` captures the last
                # ``chunk.raw_parts`` it sees and attaches it to the
                # assistant ``Message._raw_parts``.
                if raw_blocks:
                    yield StreamChunk(raw_parts=list(raw_blocks))

        except Exception as e:
            # If the watchdog closed the stream mid-iteration, the SDK raises
            # a connection-related exception. Suppress when cancel is set so
            # the consumer's _check_cancelled() can handle it cleanly.
            if cancel_event is not None and cancel_event.is_set():
                log_debug(f"AnthropicProvider stream closed by cancel: {e}")
                return
            log_error(f"AnthropicProvider.chat_stream error: {e}")
            self._handle_api_error(e)
        finally:
            done.set()
