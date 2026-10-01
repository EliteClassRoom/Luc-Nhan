"""Core data types for Rikugan."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


def _safe_persisted_text(value: object) -> str:
    """Sanitize a text value loaded from a persisted session JSON file.

    Persisted strings are NOT trusted: they can contain
      * lone UTF-16 surrogates (binary-originated IDA strings),
      * injection markers (``[SYSTEM]``, ``<|im_start|>``, …),
      * ``None`` or non-strings (from JSON round-trips or schema drift).

    We coerce to ``str``, replace lone surrogates with ``U+FFFD`` so the
    next UTF-8 encode never crashes, then strip role markers. The result
    is safe to feed back into the system prompt.

    We deliberately do NOT strip angle brackets here — legitimate
    reverse-engineering content routinely contains ``std::vector<int>``
    template syntax, ``a < b && c > d`` comparisons, and IDA-generated
    output with literal angle brackets. The closing-tag defense lives
    at the prompt-bound wrapper boundary (``sanitize_tool_result``,
    ``quote_untrusted``, ``sanitize_memory``, …) via
    ``_neutralize_closing_tag``.

    Use :func:`_safe_persisted_identifier` for fields that flow into a
    wrapper WITHOUT downstream closing-tag neutralization (id, name,
    tool_call_id, metadata values).

    Sanitize helpers are imported lazily to avoid a circular import —
    ``core.sanitize`` itself imports ``Message`` and ``ToolResult`` from
    this module.
    """
    if value is None:
        return ""
    # Local import: avoid a top-level cycle with ``core.sanitize``.
    from .sanitize import strip_injection_markers, strip_lone_surrogates

    text = strip_lone_surrogates(str(value))
    return strip_injection_markers(text)


def _safe_persisted_identifier(value: object) -> str:
    """Strict variant of :func:`_safe_persisted_text` for identifiers.

    Also strips angle brackets so that a poisoned identifier like
    ``"</tool_result>system"`` cannot break out of a prompt-bound
    wrapper. Used for fields where downstream closing-tag neutralization
    does NOT run: message ``id`` / ``name`` / ``tool_call_id``,
    metadata values, and category-like labels.

    Do NOT use this for ``content`` or ``tool_results[].content`` —
    those flow through prompt-bound wrappers that handle closing tags
    on their own (and the content may legitimately contain ``<``/``>``).
    """
    text = _safe_persisted_text(value)
    text = text.replace("<", "").replace(">", "")
    return text


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

    @staticmethod
    def make_id() -> str:
        return f"call_{uuid.uuid4().hex[:24]}"


@dataclass
class ToolResult:
    tool_call_id: str
    name: str
    content: str
    is_error: bool = False


def coerce_token_count(value: Any) -> int:
    """Coerce an arbitrary value into a non-negative integer token count.

    Provider SDKs and JSON round-trips may surface ``None`` or non-numeric
    values for token fields. This helper centralizes the coercion so that
    arithmetic and comparisons in agent/state code never have to guard
    against ``None`` or unexpected types.
    """
    if value is None:
        return 0
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    if n < 0:
        return 0
    return n


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    def __post_init__(self) -> None:
        # Every internal token count is a non-negative int. Providers and
        # persisted JSON can supply None / floats / strings; this is the
        # single point of normalization.
        raw_total = self.total_tokens
        self.prompt_tokens = coerce_token_count(self.prompt_tokens)
        self.completion_tokens = coerce_token_count(self.completion_tokens)
        self.cache_read_tokens = coerce_token_count(self.cache_read_tokens)
        self.cache_creation_tokens = coerce_token_count(self.cache_creation_tokens)
        self.total_tokens = coerce_token_count(raw_total)
        # If the provider omitted total_tokens, derive it from prompt+completion.
        computed_total = self.prompt_tokens + self.completion_tokens
        if self.total_tokens <= 0 and computed_total > 0:
            self.total_tokens = computed_total

    @property
    def context_tokens(self) -> int:
        """Total tokens occupying the context window (including cache hits/writes)."""
        return self.prompt_tokens + self.cache_read_tokens + self.cache_creation_tokens


@dataclass(frozen=True)
class LLMRequestContext:
    attempt_number: int = 1
    recovery: bool = False
    max_tokens_override: int | None = None
    system_suffix: str = ""
    disable_thinking: bool = False
    # Transport hint: True when the request is dispatched through the
    # streaming pipeline (``chat_stream``).  Providers use this to gate
    # transport-only wire fields (e.g. GLM ``tool_stream``) so they are
    # not sent on non-streaming ``chat()`` calls.  Defaults to False to
    # preserve payload equivalence for non-GLM providers and non-stream
    # callers.
    streaming: bool = False


@dataclass(frozen=True)
class AttemptUsage:
    usage: TokenUsage
    provenance: Literal["authoritative", "estimated"]


class TurnDisposition(str, Enum):
    COMPLETED = "completed"
    TOOL_USE = "tool_use"
    TRUNCATED_TEXT = "truncated_text"
    TRUNCATED_PARTIAL_TOOL_USE = "truncated_partial_tool_use"
    DEGENERATED = "degenerated"
    FILTERED = "filtered"
    STREAM_BROKEN = "stream_broken"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class TurnOutcome:
    visible_text: str = ""
    reasoning_content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: TokenUsage | None = None
    raw_parts: Any = None
    finish_reason: str | None = None
    disposition: TurnDisposition = TurnDisposition.FAILED
    attempt_usage: AttemptUsage | None = None
    guard_trigger: str = ""
    repetition_ratio_millis: int = 0


@dataclass
class Message:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    timestamp: float = field(default_factory=time.time)
    token_usage: TokenUsage | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    # Provider-neutral reasoning trace (OpenAI o-series, GLM thinking, etc.).
    # Persisted to JSON only when non-empty so legacy sessions stay compatible.
    # Sanitized on load through ``_safe_persisted_text`` to neutralize
    # injection markers and lone surrogates before re-entering the prompt.
    reasoning_content: str = ""
    # Provider-specific raw response data (e.g. Gemini parts with thought_signatures).
    # Not serialized to JSON — only kept in-memory for the current session.
    _raw_parts: Any = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "role": self.role.value,
            "id": self.id,
            "timestamp": self.timestamp,
        }
        if self.content:
            d["content"] = self.content
        if self.tool_calls:
            d["tool_calls"] = [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in self.tool_calls]
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name:
            d["name"] = self.name
        if self.tool_results:
            d["tool_results"] = [
                {
                    "tool_call_id": tr.tool_call_id,
                    "name": tr.name,
                    "content": tr.content,
                    "is_error": tr.is_error,
                }
                for tr in self.tool_results
            ]
        if self.token_usage:
            d["token_usage"] = {
                "prompt_tokens": self.token_usage.prompt_tokens,
                "completion_tokens": self.token_usage.completion_tokens,
                "total_tokens": self.token_usage.total_tokens,
                "cache_read_tokens": self.token_usage.cache_read_tokens,
                "cache_creation_tokens": self.token_usage.cache_creation_tokens,
            }
        if self.reasoning_content:
            d["reasoning_content"] = self.reasoning_content
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Message:
        # Validate the role up front so a bad value raises the right exception
        # type before we do any string sanitization.
        try:
            role = Role(d.get("role", ""))
        except ValueError as exc:
            raise ValueError(f"Invalid message role {d.get('role')!r}: {exc}") from exc

        tool_calls: list[ToolCall] = []
        for tc in d.get("tool_calls", []) or []:
            if not isinstance(tc, dict):
                continue
            tool_calls.append(
                ToolCall(
                    id=_safe_persisted_identifier(tc.get("id", "")) or f"call_{uuid.uuid4().hex[:24]}",
                    name=_safe_persisted_identifier(tc.get("name", "")),
                    arguments=tc.get("arguments") or {},
                )
            )

        tool_results: list[ToolResult] = []
        for tr in d.get("tool_results", []) or []:
            if not isinstance(tr, dict):
                continue
            tool_results.append(
                ToolResult(
                    tool_call_id=_safe_persisted_identifier(tr.get("tool_call_id", "")),
                    name=_safe_persisted_identifier(tr.get("name", "")),
                    # Content uses the lenient helper — legitimate
                    # reverse-engineering output (C++ templates, IDA
                    # comments) routinely contains ``<``/``>``.
                    # Closing-tag neutralization happens at the prompt
                    # boundary via ``sanitize_tool_result``.
                    content=_safe_persisted_text(tr.get("content", "")),
                    is_error=bool(tr.get("is_error", False)),
                )
            )

        usage = None
        if "token_usage" in d:
            u = d["token_usage"] or {}
            usage = TokenUsage(
                # Pass raw values through TokenUsage.__post_init__ which
                # normalizes None/strings/floats to non-negative ints.
                prompt_tokens=u.get("prompt_tokens") or 0,
                completion_tokens=u.get("completion_tokens") or 0,
                total_tokens=u.get("total_tokens") or 0,
                cache_read_tokens=u.get("cache_read_tokens") or 0,
                cache_creation_tokens=u.get("cache_creation_tokens") or 0,
            )
        return cls(
            role=role,
            # Content uses the lenient helper (see tool_results comment).
            content=_safe_persisted_text(d.get("content", "")),
            tool_calls=tool_calls,
            tool_results=tool_results,
            tool_call_id=_safe_persisted_identifier(d.get("tool_call_id")) or None,
            name=_safe_persisted_identifier(d.get("name")) or None,
            timestamp=d.get("timestamp", time.time()),
            token_usage=usage,
            id=_safe_persisted_identifier(d.get("id")) or uuid.uuid4().hex[:12],
            reasoning_content=_safe_persisted_text(d.get("reasoning_content")),
        )


@dataclass
class ProviderCapabilities:
    streaming: bool = True
    tool_use: bool = True
    vision: bool = False
    max_context_window: int = 128000
    max_output_tokens: int = 4096
    supports_system_prompt: bool = True
    supports_cache_control: bool = False
    # Provider can surface a separate reasoning channel (OpenAI o-series,
    # GLM thinking, Anthropic extended thinking, etc.).
    reasoning_content: bool = False
    # Provider streams tool-call argument deltas as they arrive (vs. only at end).
    streaming_tool_calls: bool = False
    # Provider accepts an explicit reasoning-effort knob (low/medium/high).
    reasoning_effort: bool = False


@dataclass
class ModelInfo:
    id: str
    name: str
    provider: str
    context_window: int = 128000
    max_output_tokens: int = 4096
    supports_tools: bool = True
    supports_vision: bool = False


@dataclass
class StreamChunk:
    """A single chunk from a streaming response."""

    text: str = ""
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_args_delta: str = ""
    finish_reason: str | None = None
    usage: TokenUsage | None = None
    # Incremental reasoning/thinking text from the provider (separate from
    # the user-visible ``text`` channel). None when the provider does not
    # stream reasoning.
    reasoning_delta: str | None = None
    # True for chunks emitted while the model is inside a thinking block
    # (``<think...`` for Anthropic-style providers). Lets downstream
    # filters (e.g. MiniMax native tool-call recovery) pass thinking text
    # through verbatim instead of treating it as user-visible output.
    is_thinking: bool = False
    # Tool-call lifecycle flags. ``is_tool_call_start`` opens a new
    # call; ``is_tool_call_end`` closes it.
    is_tool_call_start: bool = False
    is_tool_call_end: bool = False
    # Provider-specific raw response parts (e.g. Gemini parts with thought_signatures).
    raw_parts: Any = None

# ---------------------------------------------------------------------------
# User approval / decision protocol
# ---------------------------------------------------------------------------


class UserDecision(str, Enum):
    """Typed decisions for approval and save flows."""

    APPROVE = "approve"
    CANCEL = "cancel"
    REGENERATE = "regenerate"
    SAVE = "save"
    DISCARD = "discard"
    FEEDBACK = "feedback"


@dataclass
class UserAnswer:
    """Parsed user answer with optional free-text feedback."""

    decision: UserDecision
    feedback: str = ""


_APPROVE_WORDS = frozenset({"approve", "1", "yes", "y"})
_CANCEL_WORDS = frozenset({"cancel", "no", "n"})
_SAVE_WORDS = frozenset({"save all", "save", "1", "yes", "y"})


def parse_approval(raw: str) -> UserAnswer:
    """Parse a raw user string into an approval decision.

    Used for plan approval and modification-plan approval flows.
    """
    text = raw.strip().lower()
    if text in _APPROVE_WORDS:
        return UserAnswer(UserDecision.APPROVE)
    if text in _CANCEL_WORDS:
        return UserAnswer(UserDecision.CANCEL)
    if text == "regenerate":
        return UserAnswer(UserDecision.REGENERATE)
    return UserAnswer(UserDecision.FEEDBACK, feedback=raw.strip())


def parse_save_decision(raw: str) -> UserAnswer:
    """Parse a raw user string into a save/discard decision.

    Used for the exploration mode save phase.
    """
    text = raw.strip().lower()
    if text in _SAVE_WORDS:
        return UserAnswer(UserDecision.SAVE)
    return UserAnswer(UserDecision.DISCARD)
