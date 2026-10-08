"""Headless prompt runner: drain AgentLoop events to completion."""

from __future__ import annotations

import dataclasses
import time
import uuid
from typing import TYPE_CHECKING, Any

from ..agent.turn import TurnEventType
from ..core.logging import log_debug

if TYPE_CHECKING:
    from ..ida.headless_controller import HeadlessSessionController


# ---------------------------------------------------------------------------
# Exit codes (mirrors the plan specification)
# ---------------------------------------------------------------------------

EXIT_SUCCESS = 0
EXIT_GENERIC_ERROR = 1
EXIT_BAD_ARGS = 2
EXIT_BOOTSTRAP_FAILURE = 3
EXIT_CONFIG_ERROR = 4
EXIT_TOOL_FAILURE = 5
EXIT_CANCELLED = 6
EXIT_APPROVAL_REQUIRED = 7
EXIT_SERVER_AUTH_FAILED = 8


# Approval event types that cannot be auto-resolved in one-shot mode.
_APPROVAL_EVENT_TYPES: frozenset[TurnEventType] = frozenset(
    {
        TurnEventType.PLAN_GENERATED,
        TurnEventType.SAVE_APPROVAL_REQUEST,
        TurnEventType.USER_QUESTION,
        TurnEventType.TOOL_APPROVAL_REQUEST,
    }
)

# GLM reasoning-resilience events that pass through the headless runner
# without mutating final_text, errors, status, or exit_code.  They are
# still captured in the JSON event buffer via ``to_dict()`` so downstream
# consumers (CLI, control server, analytics) can observe the recovery
# lifecycle.
_PASS_THROUGH_EVENT_TYPES: frozenset[TurnEventType] = frozenset(
    {
        TurnEventType.REASONING_DELTA,
        TurnEventType.RECOVERY_START,
        TurnEventType.TOOL_CALL_DISCARDED,
    }
)


@dataclasses.dataclass
class RunResult:
    """Result of a single run_prompt() invocation."""

    exit_code: int
    final_text: str = ""
    errors: list[str] = dataclasses.field(default_factory=list)
    events: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    run_id: str = ""
    session_id: str = ""
    elapsed: float = 0.0
    turn_count: int = 0


def run_prompt(
    controller: HeadlessSessionController,
    prompt: str,
    *,
    json_events: bool = False,
) -> RunResult:
    """Run a single prompt to completion and collect the result.

    In one-shot mode without a human operator, approval events are
    automatically denied so the agent does not hang.  All events are
    drained until the runner sends its sentinel.

    Parameters
    ----------
    controller:
        A ``HeadlessSessionController`` with a live dispatcher and
        provider configuration.
    prompt:
        The user message to send.
    json_events:
        If True, include the serialized event list in the result.
        Also forces event collection regardless of flag for full
        diagnostics.

    Returns
    -------
    RunResult:
        Exit code, final assistant text, errors, and optional event
        dump.
    """
    run_id = uuid.uuid4().hex[:8]
    start = time.perf_counter()
    error = controller.start_agent(prompt)
    if error is not None:
        return RunResult(
            exit_code=EXIT_CONFIG_ERROR,
            errors=[error],
            run_id=run_id,
            session_id=controller.session.id if controller.session else "",
            elapsed=time.perf_counter() - start,
        )

    runner = controller.get_runner()
    if runner is None:
        controller.on_agent_finished()
        return RunResult(
            exit_code=EXIT_GENERIC_ERROR,
            errors=["Agent runner not created"],
            run_id=run_id,
            elapsed=time.perf_counter() - start,
        )

    errors: list[str] = []
    final_text_deltas: list[str] = []
    final_text_done: str = ""
    events: list[dict[str, Any]] = []
    exit_code = EXIT_SUCCESS
    turn_count = 0
    finished: bool = False

    # Ensure on_agent_finished() is called exactly once regardless of
    # which error path we take.
    try:
        while True:
            event = runner.get_event(timeout=0.5)
            if event is None:
                if not controller.is_agent_running:
                    break
                continue

            turn_count = max(turn_count, event.turn_number)

            if json_events:
                events.append(event.to_dict())

            if event.type == TurnEventType.TEXT_DELTA and event.text:
                final_text_deltas.append(event.text)
            elif event.type == TurnEventType.TEXT_DONE and event.text:
                final_text_done = event.text
            elif event.type == TurnEventType.ERROR:
                if event.error:
                    errors.append(event.error)
                if exit_code == EXIT_SUCCESS:
                    exit_code = EXIT_TOOL_FAILURE
            elif event.type == TurnEventType.CANCELLED:
                if exit_code == EXIT_SUCCESS:
                    exit_code = EXIT_CANCELLED
            elif event.type in _APPROVAL_EVENT_TYPES:
                detail = event.type.value
                if event.tool_name:
                    detail = f"{detail} ({event.tool_name})"
                errors.append(f"Approval required for {detail} — auto-denied in one-shot mode")
                if exit_code in (EXIT_SUCCESS, EXIT_TOOL_FAILURE):
                    exit_code = EXIT_APPROVAL_REQUIRED
                # Unblock the agent so it does not hang on approval queues.
                _auto_deny_approval(runner, event.type)
            elif event.type in _PASS_THROUGH_EVENT_TYPES:
                # GLM reasoning/recovery events pass through without
                # mutating final_text, errors, status, or exit_code.
                # They are already captured in the JSON event buffer via
                # the to_dict() call above.
                pass

    except KeyboardInterrupt:
        exit_code = EXIT_CANCELLED
        errors.append("Cancelled by user (KeyboardInterrupt)")
    finally:
        if not finished:
            controller.on_agent_finished()

    # Prefer TEXT_DONE as the authoritative final text; fall back to
    # accumulated TEXT_DELTA parts.
    final_text = final_text_done if final_text_done else "".join(final_text_deltas).strip()

    return RunResult(
        exit_code=exit_code,
        final_text=final_text.strip(),
        errors=errors,
        events=events,
        run_id=run_id,
        session_id=controller.session.id if controller.session else "",
        elapsed=time.perf_counter() - start,
        turn_count=turn_count,
    )


def _auto_deny_approval(runner: Any, event_type: TurnEventType) -> None:
    """Auto-deny an approval event so the agent does not deadlock.

    Called from the event-drain loop (NOT from the agent thread),
    so these ``submit_*`` calls are safe.
    """
    try:
        agent_loop = runner.agent_loop
    except AttributeError:
        return

    if event_type == TurnEventType.TOOL_APPROVAL_REQUEST:
        try:
            agent_loop.submit_tool_approval("deny")
        except Exception as exc:
            log_debug(f"Headless auto-deny tool_approval failed: {exc}")
    elif event_type in (TurnEventType.PLAN_GENERATED, TurnEventType.SAVE_APPROVAL_REQUEST):
        try:
            agent_loop.submit_approval("deny")
        except Exception as exc:
            log_debug(f"Headless auto-deny approval failed: {exc}")
    elif event_type == TurnEventType.USER_QUESTION:
        try:
            agent_loop.submit_user_answer("")
        except Exception as exc:
            log_debug(f"Headless auto-answer user_question failed: {exc}")
