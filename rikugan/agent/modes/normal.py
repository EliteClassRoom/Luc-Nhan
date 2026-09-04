"""Normal mode runner: standard tool-use loop."""

from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING

from ...core.logging import log_debug
from ..turn import TurnEvent
from .turn_helpers import execute_single_turn

if TYPE_CHECKING:
    from ..loop import AgentLoop


def run_normal_loop(
    loop: AgentLoop,
    system_prompt: str,
    tools_schema: list,
) -> Generator[TurnEvent, None, None]:
    """Run the standard agentic while loop (non-plan, non-exploration)."""
    # Hard ceiling for the number of turns. Subagents set ``loop._max_turns``
    # via AgentLoop(max_turns=...); the loop falls back to the legacy 100-turn
    # default when ``None``, preserving top-level behaviour.
    max_turns: int = getattr(loop, "_max_turns", None) or 100
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
            log_debug(f"Turn {turn} end (final)")
            yield TurnEvent.turn_end(turn)
            break

        # Consecutive error recovery: hint at 3, disable tools at 5
        loop._maybe_inject_error_hint()

        log_debug(f"Turn {turn} end ({len(result.tool_calls)} tool calls)")
        yield TurnEvent.turn_end(turn)
