"""Per-run tool-call repetition and progress tracking.

The tracker watches the tool calls a turn issues and answers two
questions:

* *Is this the same call again?*  A tool call is identified by its name
  plus its canonicalised arguments, so the same call with the same
  arguments is recognised regardless of key order or JSON formatting.
* *Did the turn learn anything?*  A result whose content has never been
  seen before counts as new information; a result identical to one the
  agent already received does not.

Both answers feed the escalation ladder in
:meth:`AgentLoop._after_tool_batch`: warn, then block the tool, then
disable tools for the turn.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass
from enum import Enum

from ..core.types import ToolCall, ToolResult

#: Number of observations retained for repeat detection.
HISTORY = 32

#: How many turns back a matching observation still counts as a repeat.
REPEAT_WINDOW_TURNS = 12


class RepeatKind(str, Enum):
    """How a tool call relates to an earlier one."""

    NONE = "none"
    REPEAT = "repeat"
    IDENTICAL_RESULT = "identical_result"


@dataclass(frozen=True)
class ProgressVerdict:
    """Outcome of observing one turn's tool batch."""

    repeat_kind: RepeatKind = RepeatKind.NONE
    tool_name: str = ""
    occurrences: int = 0
    no_progress: bool = False


@dataclass(frozen=True)
class _Observation:
    """One previously executed tool call."""

    signature: str
    result_hash: str
    turn: int


class ProgressTracker:
    """Tracks repeated tool calls and whether a turn made progress."""

    def __init__(self, *, warn_at: int, block_at: int, stop_at: int) -> None:
        if not (1 < warn_at < block_at < stop_at):
            raise ValueError(
                f"progress thresholds must satisfy 1 < warn < block < stop, got {warn_at}/{block_at}/{stop_at}"
            )
        self._warn_at = warn_at
        self._block_at = block_at
        self._stop_at = stop_at
        self._history: deque[_Observation] = deque(maxlen=HISTORY)
        self._seen_results: set[str] = set()
        self._mutation_success = False
        self._new_result_seen = False

    @staticmethod
    def signature(tc: ToolCall) -> str:
        """Stable identity of a tool call (name + canonical arguments)."""
        payload = json.dumps(tc.arguments, sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(f"{tc.name}\x00{payload}".encode()).hexdigest()[:16]

    @staticmethod
    def _result_hash(content: str) -> str:
        """Stable identity of a tool result's content."""
        return hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()[:16]

    def reset_user_turn(self) -> None:
        """Forget everything — called at the start of each user request."""
        self._history.clear()
        self._seen_results.clear()
        self._mutation_success = False
        self._new_result_seen = False

    def note_mutation_success(self) -> None:
        """Record that a mutating tool changed state (always progress)."""
        self._mutation_success = True

    def run_made_progress(self) -> bool:
        """True when the run produced new information or a state change."""
        return self._mutation_success or self._new_result_seen

    def observe_turn(
        self,
        turn: int,
        tool_calls: list[ToolCall],
        tool_results: list[ToolResult],
    ) -> ProgressVerdict:
        """Record one turn's tool batch and classify it.

        The returned verdict names the *worst* repetition in the batch
        (the call with the most prior occurrences) and reports whether
        every call in the batch matched an earlier observation — the
        "no progress" signal.
        """
        best_kind = RepeatKind.NONE
        best_tool = ""
        best_occurrences = 0
        all_repeated = bool(tool_calls)
        for tc, tr in zip(tool_calls, tool_results, strict=False):
            signature = self.signature(tc)
            result_hash = self._result_hash(tr.content)
            prior = [
                obs for obs in self._history if obs.signature == signature and turn - obs.turn < REPEAT_WINDOW_TURNS
            ]
            kind = RepeatKind.NONE
            if prior:
                kind = (
                    RepeatKind.IDENTICAL_RESULT
                    if any(o.result_hash == result_hash for o in prior)
                    else RepeatKind.REPEAT
                )
            if kind is RepeatKind.NONE:
                all_repeated = False
            if result_hash not in self._seen_results:
                self._seen_results.add(result_hash)
                self._new_result_seen = True
            self._history.append(_Observation(signature, result_hash, turn))
            occurrences = len(prior) + 1
            if occurrences > best_occurrences:
                best_kind, best_tool, best_occurrences = kind, tc.name, occurrences
        return ProgressVerdict(best_kind, best_tool, best_occurrences, all_repeated)
