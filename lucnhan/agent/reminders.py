"""Rate-limited hidden reminders injected as ``[SYSTEM]`` user messages.

A reminder is a nudge the agent gives itself when it is going in
circles.  Two rules keep the nudge from becoming noise:

* **gap** — at least *min_gap_turns* turns must pass between two
  reminders of the same kind;
* **budget** — at most *max_per_run* reminders of the same kind are
  emitted per user request.

Both are per run, so a new user request starts with a fresh budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ReminderKind(str, Enum):
    """The kinds of reminder the agent can inject."""

    TOOL_REPEAT = "tool_repeat"
    NO_PROGRESS = "no_progress"
    STREAM_STALL = "stream_stall"
    GOAL_PENDING = "goal_pending"


@dataclass(frozen=True)
class ReminderPolicy:
    """Rate limit for one reminder kind."""

    max_per_run: int
    min_gap_turns: int


DEFAULT_POLICIES: dict[ReminderKind, ReminderPolicy] = {
    ReminderKind.TOOL_REPEAT: ReminderPolicy(max_per_run=2, min_gap_turns=2),
    ReminderKind.NO_PROGRESS: ReminderPolicy(max_per_run=1, min_gap_turns=5),
    ReminderKind.STREAM_STALL: ReminderPolicy(max_per_run=3, min_gap_turns=1),
    ReminderKind.GOAL_PENDING: ReminderPolicy(max_per_run=1, min_gap_turns=0),
}

TOOL_REPEAT_REMINDER = (
    "[SYSTEM] You have called `{tool}` with identical arguments {count} times and received the same result. "
    "Repeating it cannot change the outcome. Take a different approach: use a different tool, narrow or widen "
    "the query, or explain to the user what is blocking you."
)
NO_PROGRESS_REMINDER = (
    "[SYSTEM] This turn repeated earlier tool calls without producing new information. State what you already "
    "know, what is still unknown, and the single next action that would resolve it. Do not re-run the same calls."
)
STREAM_STALL_REMINDER = (
    "[SYSTEM] The previous response was interrupted mid-stream. Continue exactly where it stopped. "
    "Do not repeat text that was already shown."
)
GOAL_PENDING_REMINDER = (
    "[SYSTEM] An active goal is still set but the last turn made no verifiable progress toward it. "
    "Either take the next concrete action now, or state precisely what is missing."
)


class ReminderScheduler:
    """Decides whether a reminder may be emitted right now."""

    def __init__(self, policies: dict[ReminderKind, ReminderPolicy] | None = None) -> None:
        self._policies = dict(policies or DEFAULT_POLICIES)
        self._counts: dict[ReminderKind, int] = {}
        self._last_turn: dict[ReminderKind, int] = {}

    def should_emit(self, kind: ReminderKind, turn: int) -> bool:
        """True when a reminder of *kind* may be emitted on *turn*."""
        policy = self._policies[kind]
        if self._counts.get(kind, 0) >= policy.max_per_run:
            return False
        last = self._last_turn.get(kind)
        return last is None or turn - last >= policy.min_gap_turns

    def mark_emitted(self, kind: ReminderKind, turn: int) -> None:
        """Record that a reminder of *kind* was emitted on *turn*."""
        self._counts[kind] = self._counts.get(kind, 0) + 1
        self._last_turn[kind] = turn

    def reset_run(self) -> None:
        """Restore the full budget — called at the start of each request."""
        self._counts.clear()
        self._last_turn.clear()
