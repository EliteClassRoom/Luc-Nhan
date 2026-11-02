"""Normal mode runner: standard tool-use loop."""

from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING

from ...core.logging import log_debug
from ...core.types import Message, Role
from ..loop_commands import ACTIVE_GOAL_METADATA_KEY
from ..reminders import GOAL_PENDING_REMINDER, STREAM_STALL_REMINDER, ReminderKind
from ..turn import TurnEvent
from .turn_helpers import execute_single_turn

if TYPE_CHECKING:
    from ..loop import AgentLoop

# Hard ceiling for the number of turns in a normal-mode run. Subagents set
# ``loop._max_turns`` explicitly via ``AgentLoop(max_turns=...)``; this is the
# fallback used when none is supplied.
_DEFAULT_MAX_TURNS = 100


def run_normal_loop(
    loop: AgentLoop,
    system_prompt: str,
    tools_schema: list,
) -> Generator[TurnEvent, None, None]:
    """Run the standard agentic while loop (non-plan, non-exploration)."""
    # Hard ceiling for the number of turns. Subagents set ``loop._max_turns``
    # via AgentLoop(max_turns=...); the loop falls back to the legacy 100-turn
    # default when ``None``, preserving top-level behaviour. Use an explicit
    # ``is None`` check (NOT ``or 100``) so a legitimate 0 would not be
    # silently promoted to 100; the runner constructor rejects ``max_turns=0``
    # outright so we should never see one here.
    mt = getattr(loop, "_max_turns", None)
    max_turns: int = _DEFAULT_MAX_TURNS if mt is None else int(mt)
    # One-shot: the goal continuation fires at most once per run.
    goal_continue_used = False
    turn = 0
    while True:
        loop._check_cancelled()
        turn += 1
        if turn > max_turns:
            yield TurnEvent.error_event(f"Reached max turns limit ({max_turns}).")
            break
        loop.session.current_turn = turn
        log_debug(f"Turn {turn} start")
        yield TurnEvent.turn_start(turn)

        # If tools were disabled due to consecutive errors, force text-only
        turn_tools = None if loop._tools_disabled_for_turn else tools_schema
        loop._tools_disabled_for_turn = False

        result = yield from execute_single_turn(loop, system_prompt, turn_tools)

        if not result.ok:
            return

        if not result.has_tool_calls:
            loop._consecutive_errors = 0
            if result.stalled and loop._reminders.should_emit(ReminderKind.STREAM_STALL, turn):
                loop._reminders.mark_emitted(ReminderKind.STREAM_STALL, turn)
                loop.session.add_message(Message(role=Role.USER, content=STREAM_STALL_REMINDER))
                log_debug(f"Turn {turn} end (stream stalled, continuing)")
                yield TurnEvent.turn_end(turn)
                continue
            # A goal is still set but the turn produced no verifiable
            # progress: give the agent exactly one more chance to act on
            # it instead of stopping with the goal half-done.
            if (
                loop.config.goal_continuation_enabled
                and not goal_continue_used
                and loop.session.metadata.get(ACTIVE_GOAL_METADATA_KEY)
                and not loop._progress.run_made_progress()
                and loop._reminders.should_emit(ReminderKind.GOAL_PENDING, turn)
            ):
                goal_continue_used = True
                loop._reminders.mark_emitted(ReminderKind.GOAL_PENDING, turn)
                loop.session.add_message(Message(role=Role.USER, content=GOAL_PENDING_REMINDER))
                log_debug(f"Turn {turn} end (goal pending, continuing)")
                yield TurnEvent.turn_end(turn)
                continue
            log_debug(f"Turn {turn} end (final)")
            yield TurnEvent.turn_end(turn)
            break

        # Consecutive error recovery: hint at 3, disable tools at 5
        loop._maybe_inject_error_hint()

        log_debug(f"Turn {turn} end ({len(result.tool_calls)} tool calls)")
        yield TurnEvent.turn_end(turn)
