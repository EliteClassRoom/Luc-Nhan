"""Shared helpers for mode turn execution to avoid boilerplate duplication."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ...core.errors import CancellationError, ProviderError
from ...core.logging import log_structured
from ...core.types import (
    AttemptUsage,
    LLMRequestContext,
    Message,
    Role,
    TokenUsage,
    ToolResult,
    TurnDisposition,
)
from ..turn import TurnEvent

if TYPE_CHECKING:
    from ..loop import AgentLoop


@dataclass
class TurnResult:
    """Outcome of a single agent turn.

    The base fields (``text``, ``tool_calls``, ``usage``, ``cancelled``,
    ``error``) are consumed by every mode runner via ``.ok``,
    ``.has_tool_calls``, etc.  The recovery fields are added by
    :func:`execute_single_turn` when a GLM reasoning degeneration triggers
    a one-shot retry; existing callers that only read the base fields are
    unaffected.
    """

    text: str = ""
    tool_calls: list = field(default_factory=list)
    usage: TokenUsage | None = None
    cancelled: bool = False
    error: str | None = None
    # --- GLM one-shot recovery extension ---
    finish_reason: str | None = None
    disposition: TurnDisposition | str | None = None
    recovery_attempted: bool = False
    recovery_failed: bool = False
    attempt_usages: list[AttemptUsage] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.cancelled and self.error is None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


def build_assistant_message(
    assistant_text: str,
    tool_calls: list,
    last_usage: TokenUsage | None,
    raw_parts: list | None,
) -> Message:
    """Build an assistant Message, attaching raw_parts if present."""
    msg = Message(
        role=Role.ASSISTANT,
        content=assistant_text,
        tool_calls=tool_calls,
        token_usage=last_usage,
    )
    if raw_parts is not None:
        msg._raw_parts = raw_parts
    return msg


@dataclass
class _EmptyOutcome:
    """Minimal stand-in for a TurnOutcome when the provider raised before
    returning any stream data.  All fields are empty so the telemetry
    helper emits zeros without touching the exception.
    """

    visible_text: str = ""
    reasoning_content: str = ""
    tool_calls: list = field(default_factory=list)
    usage: TokenUsage | None = None
    raw_parts: Any = None
    finish_reason: str | None = None
    disposition: TurnDisposition = TurnDisposition.FAILED
    attempt_usage: AttemptUsage | None = None
    guard_trigger: str = ""
    repetition_ratio_millis: int = 0


def _emit_attempt_telemetry(
    loop: AgentLoop,
    outcome: Any,
    *,
    attempt_number: int,
    discarded_attempt: bool,
    recovery_result: str | None = None,
) -> None:
    """Emit one content-free ``agent_attempt`` record to the JSONL stream.

    Only allowlisted scalar fields are populated.  No reasoning text, tool
    arguments, tool results, message bodies, or provider exceptions are
    forwarded — the structured log is a safe telemetry surface, not a
    debugging trace.

    ``repetition_ratio_millis`` is an integer in thousandths (0-1000) so
    downstream consumers can compare without float parsing.
    """
    usage = outcome.attempt_usage.usage if outcome.attempt_usage else None
    provenance = outcome.attempt_usage.provenance if outcome.attempt_usage else "estimated"

    # Derive scalar config values safely — never forward raw dicts.
    provider_name = loop.config.provider.name or ""
    model_name = loop.config.provider.model or ""
    dialect = loop.config.provider.extra.get("dialect", "") if isinstance(loop.config.provider.extra, dict) else ""

    # Thinking mode / effort are scalar config strings only.
    thinking_mode = ""
    reasoning_effort = ""
    thinking_cfg = loop.config.provider.extra.get("thinking") if isinstance(loop.config.provider.extra, dict) else None
    if isinstance(thinking_cfg, dict):
        thinking_mode = "enabled" if thinking_cfg.get("enabled", True) else "disabled"
        reasoning_effort = str(thinking_cfg.get("reasoning_effort", ""))

    # Repetition ratio: integer thousandths.
    rep_millis = outcome.repetition_ratio_millis if hasattr(outcome, "repetition_ratio_millis") else 0

    # Character counts — scalars only, never the text itself.
    reasoning_chars = (
        len(outcome.reasoning_content) if hasattr(outcome, "reasoning_content") and outcome.reasoning_content else 0
    )
    visible_chars = len(outcome.visible_text) if hasattr(outcome, "visible_text") and outcome.visible_text else 0

    # Tool counts — TurnOutcome.tool_calls contains only surviving completed
    # (safe, non-degenerated) tool calls.  Partial or discarded tool calls are
    # already stripped by the guard before the outcome is returned.  Both
    # tool_start_count and tool_end_count are set to the same value because
    # the outcome does not distinguish started-vs-ended lifecycle states —
    # every surviving call both started and completed.  Downstream consumers
    # should interpret these as "surviving completed tool call count", not
    # as independent start/end lifecycle metrics.
    surviving_tool_calls = 0
    if hasattr(outcome, "tool_calls") and outcome.tool_calls:
        surviving_tool_calls = len(outcome.tool_calls)

    finish_reason = outcome.finish_reason if hasattr(outcome, "finish_reason") and outcome.finish_reason else ""
    disposition = outcome.disposition.value if hasattr(outcome, "disposition") and outcome.disposition else ""
    guard_trigger = outcome.guard_trigger if hasattr(outcome, "guard_trigger") and outcome.guard_trigger else ""

    event: dict[str, Any] = {
        "provider": provider_name,
        "model": model_name,
        "dialect": dialect,
        "attempt_number": attempt_number,
        "thinking_mode": thinking_mode,
        "reasoning_effort": reasoning_effort,
        "prompt_tokens": usage.prompt_tokens if usage else 0,
        "completion_tokens": usage.completion_tokens if usage else 0,
        "total_tokens": usage.total_tokens if usage else 0,
        "usage_provenance": provenance,
        "reasoning_chars": reasoning_chars,
        "visible_chars": visible_chars,
        "tool_start_count": surviving_tool_calls,
        "tool_end_count": surviving_tool_calls,
        "finish_reason": finish_reason,
        "disposition": disposition,
        "guard_trigger": guard_trigger,
        "repetition_ratio_millis": rep_millis,
        "discarded_attempt": discarded_attempt,
    }
    if recovery_result is not None:
        event["recovery_result"] = recovery_result

    # Fail-fast: do NOT catch KeyError/TypeError from log_structured.  The
    # allowlist validation is the single defense against leaking untrusted
    # content into the telemetry stream.  Silently swallowing a KeyError
    # would let a bad call site introduce an unknown field that carries
    # raw reasoning text or tool arguments — the exact scenario the
    # content-free guard exists to prevent.
    log_structured(event)


def execute_single_turn(
    loop: AgentLoop,
    system_prompt: str,
    tools_schema: list | None,
) -> Generator[TurnEvent, None, TurnResult]:
    """Execute one LLM turn: stream, store assistant msg, execute tools.

    Yields TurnEvents (text_done, tool progress).  Returns a TurnResult
    so callers can inspect what happened without duplicating the
    stream->store->execute plumbing.

    When the GLM reasoning guard fires on the first logical attempt
    (``TurnDisposition.DEGENERATED``), a single recovery attempt is
    dispatched with thinking disabled, a capped ``max_tokens_override``,
    and a request-local suffix.  The degenerated attempt's usage is
    recorded once via :meth:`SessionState.record_usage` but its assistant
    message is never persisted.  No third attempt is ever made.
    """
    # Capture pre-turn message IDs + length so we can assert the
    # degenerated attempt left history untouched.
    pre_turn_ids = [m.id for m in loop.session.messages]
    pre_turn_len = len(loop.session.messages)

    attempt_usages: list[AttemptUsage] = []

    # --- Attempt 1 ---
    ctx1 = LLMRequestContext(attempt_number=1)
    try:
        outcome = yield from loop._stream_llm_turn(system_prompt, tools_schema, request_context=ctx1)
    except ProviderError as e:
        # Telemetry: attempt 1 failed with a provider error (discarded).
        _emit_attempt_telemetry(
            loop,
            _EmptyOutcome(),
            attempt_number=1,
            discarded_attempt=True,
            recovery_result="failure",
        )
        msg = loop._format_provider_error_for_user(e)
        yield TurnEvent.error_event(msg)
        return TurnResult(error=msg)
    except CancellationError:
        # Telemetry: attempt 1 was cancelled (discarded), then re-raise so
        # the outer run() loop converts this into a CANCELLED event.
        _emit_attempt_telemetry(
            loop,
            _EmptyOutcome(disposition=TurnDisposition.CANCELLED),
            attempt_number=1,
            discarded_attempt=True,
            recovery_result="cancelled",
        )
        raise

    if outcome.attempt_usage:
        attempt_usages.append(outcome.attempt_usage)

    if outcome.disposition != TurnDisposition.DEGENERATED:
        # Normal path: persist the assistant message + tool results.
        _emit_attempt_telemetry(loop, outcome, attempt_number=1, discarded_attempt=False)
        return (  # type: ignore[return-value]
            yield from _finalize_accepted_outcome(loop, outcome, attempt_usages)
        )

    # --- Degeneration detected: emit telemetry for the discarded attempt ---
    _emit_attempt_telemetry(loop, outcome, attempt_number=1, discarded_attempt=True)

    # --- Degeneration detected: record discarded usage once ---
    # The degenerated attempt's usage was NOT persisted (no assistant
    # message was added), so we record it directly to avoid losing the
    # token cost from session totals.
    if outcome.attempt_usage is not None:
        loop.session.record_usage(outcome.attempt_usage.usage)

    # Guard: the degenerated attempt must NOT have mutated session history.
    # This is a load-bearing invariant — use an explicit RuntimeError (not
    # ``assert``) so it survives ``python -O`` in optimized IDA Pro builds.
    post_attempt1_ids = [m.id for m in loop.session.messages]
    post_attempt1_len = len(loop.session.messages)
    if post_attempt1_len != pre_turn_len:
        raise RuntimeError(
            f"Degenerated attempt must not mutate session messages: pre={pre_turn_len}, post={post_attempt1_len}"
        )
    if post_attempt1_ids != pre_turn_ids:
        raise RuntimeError("Degenerated attempt must not change message IDs")

    # --- Recovery boundary: cancellation check ---
    loop._check_cancelled()

    # --- Attempt 2: one-shot recovery ---
    yield TurnEvent.recovery_start(
        attempt=2,
        reason="reasoning_degenerated",
        discard_transient_reasoning=True,
    )

    ctx2 = _build_recovery_context(loop)
    try:
        outcome2 = yield from loop._stream_llm_turn(system_prompt, tools_schema, request_context=ctx2)
    except ProviderError as e:
        # Telemetry for the failed recovery attempt — no exception text forwarded.
        _emit_attempt_telemetry(
            loop,
            _EmptyOutcome(),
            attempt_number=2,
            discarded_attempt=True,
            recovery_result="failure",
        )
        msg = loop._format_provider_error_for_user(e)
        yield TurnEvent.error_event(msg)
        return TurnResult(
            error=msg,
            recovery_attempted=True,
            recovery_failed=True,
            attempt_usages=attempt_usages,
        )
    except CancellationError:
        # Telemetry: attempt 2 was cancelled (discarded), then re-raise.
        _emit_attempt_telemetry(
            loop,
            _EmptyOutcome(disposition=TurnDisposition.CANCELLED),
            attempt_number=2,
            discarded_attempt=True,
            recovery_result="cancelled",
        )
        raise

    if outcome2.attempt_usage:
        attempt_usages.append(outcome2.attempt_usage)

    if outcome2.disposition == TurnDisposition.DEGENERATED:
        # Recovery also degenerated: record usage once, emit compact
        # error, do NOT persist any assistant body.  Setting
        # ``error="recovery_failed"`` makes ``TurnResult.ok`` False so
        # mode runners stop correctly without a generated body.
        if outcome2.attempt_usage is not None:
            loop.session.record_usage(outcome2.attempt_usage.usage)
        _emit_attempt_telemetry(
            loop,
            outcome2,
            attempt_number=2,
            discarded_attempt=True,
            recovery_result="failure",
        )
        yield TurnEvent.error_event(
            "Reasoning degeneration persisted after recovery attempt. The response was discarded."
        )
        return TurnResult(
            text="",
            tool_calls=[],
            usage=None,
            error="recovery_failed",
            recovery_attempted=True,
            recovery_failed=True,
            disposition="recovery_failed",
            attempt_usages=attempt_usages,
        )

    # Recovery succeeded: emit telemetry then persist attempt 2's output.
    _emit_attempt_telemetry(
        loop,
        outcome2,
        attempt_number=2,
        discarded_attempt=False,
        recovery_result="success",
    )
    return (  # type: ignore[return-value]
        yield from _finalize_accepted_outcome(
            loop,
            outcome2,
            attempt_usages,
            recovery_attempted=True,
        )
    )


def _build_recovery_context(
    loop: AgentLoop,
) -> LLMRequestContext:
    """Build the LLMRequestContext for the one-shot recovery attempt.

    Thinking is disabled, max_tokens is capped at
    ``min(recovery_max_tokens, model_max_output_tokens)``, and a
    request-local suffix is appended to steer the model toward producing
    visible output without further reasoning.
    """
    recovery_max = 16_384  # default
    try:
        from ...core.glm_config import get_glm_model_metadata, parse_glm_extra

        is_glm = loop.config.provider.extra.get("dialect") == "glm" or loop.provider.name == "glm"
        if is_glm:
            # Prefer the provider's already-validated ``GLMConfig``
            # snapshot so we don't re-validate on every recovery attempt.
            parsed = getattr(loop.provider, "glm_config", None)
            if parsed is None:
                parsed = parse_glm_extra(loop.config.provider.extra, loop.config.provider.model)
            recovery_max = parsed.guard.recovery_max_tokens
            metadata = get_glm_model_metadata(loop.config.provider.model)
            model_max = metadata.max_output_tokens
            recovery_max = min(recovery_max, model_max)
    except ImportError:
        # Module unavailable (e.g. circular import edge case) — keep default.
        # ValueError from parse_glm_extra propagates so invalid config
        # surfaces to the user rather than silently degrading the recovery.
        pass

    suffix = (
        "The previous reasoning attempt degenerated and was discarded. "
        "Produce a direct, concise response without extended reasoning."
    )

    return LLMRequestContext(
        attempt_number=2,
        recovery=True,
        disable_thinking=True,
        max_tokens_override=recovery_max,
        system_suffix=suffix,
    )


def _finalize_accepted_outcome(
    loop: AgentLoop,
    outcome: Any,
    attempt_usages: list[AttemptUsage],
    recovery_attempted: bool = False,
) -> Generator[TurnEvent, None, TurnResult]:
    """Persist an accepted (non-degenerated) TurnOutcome to session history.

    Emits ``text_done``, adds the assistant message, executes tool calls
    if present, and returns a fully-populated :class:`TurnResult`.
    """
    assistant_text = outcome.visible_text
    tool_calls = outcome.tool_calls
    last_usage = outcome.usage

    if assistant_text:
        yield TurnEvent.text_done(assistant_text)

    assistant_msg = build_assistant_message(
        assistant_text,
        tool_calls,
        last_usage,
        outcome.raw_parts,
    )
    # Propagate reasoning_content so it is persisted in checkpoints and
    # replayed in subsequent requests (spec §6.2, §7.2).  Without this,
    # GLM preserved-thinking is silently a no-op for normal turns.
    reasoning = getattr(outcome, "reasoning_content", "") or ""
    if reasoning:
        assistant_msg.reasoning_content = reasoning
    loop.session.add_message(assistant_msg)

    # Collect remaining attempt_usages that were not from a degeneration.
    # In the non-recovery path, there's exactly one attempt; in the
    # recovery path, attempt_usages already has both entries.
    all_usages = list(attempt_usages)

    if not tool_calls:
        return TurnResult(
            text=assistant_text,
            usage=last_usage,
            finish_reason=outcome.finish_reason,
            disposition=outcome.disposition,
            recovery_attempted=recovery_attempted,
            attempt_usages=all_usages,
        )

    # Execute tools and store results
    tool_results: list[ToolResult] = yield from loop._execute_tool_calls(tool_calls)
    loop.session.add_message(Message(role=Role.TOOL, tool_results=tool_results))

    return TurnResult(
        text=assistant_text,
        tool_calls=tool_calls,
        usage=last_usage,
        finish_reason=outcome.finish_reason,
        disposition=outcome.disposition,
        recovery_attempted=recovery_attempted,
        attempt_usages=all_usages,
    )
