"""Tests for the per-run tool-call repeat and progress tracker."""

from __future__ import annotations

import pytest

from lucnhan.agent.progress import ProgressTracker, RepeatKind
from lucnhan.core.types import ToolCall, ToolResult


def _tc(name: str = "decompile_function", **arguments) -> ToolCall:
    return ToolCall(id="call-1", name=name, arguments=dict(arguments))


def _tr(content: str, name: str = "decompile_function") -> ToolResult:
    return ToolResult(tool_call_id="call-1", name=name, content=content)


def _tracker() -> ProgressTracker:
    return ProgressTracker(warn_at=3, block_at=5, stop_at=8)


def test_constructor_rejects_non_ordered_thresholds() -> None:
    with pytest.raises(ValueError, match="progress thresholds"):
        ProgressTracker(warn_at=5, block_at=3, stop_at=8)
    with pytest.raises(ValueError, match="progress thresholds"):
        ProgressTracker(warn_at=3, block_at=3, stop_at=8)


def test_first_call_is_not_a_repeat() -> None:
    tracker = _tracker()
    verdict = tracker.observe_turn(1, [_tc(address="0x401000")], [_tr("body")])

    assert verdict.repeat_kind is RepeatKind.NONE
    assert verdict.occurrences == 1
    assert verdict.no_progress is False


def test_second_identical_call_is_a_repeat() -> None:
    tracker = _tracker()
    tracker.observe_turn(1, [_tc(address="0x401000")], [_tr("body")])
    verdict = tracker.observe_turn(1, [_tc(address="0x401000")], [_tr("other body")])

    assert verdict.repeat_kind is RepeatKind.REPEAT
    assert verdict.occurrences == 2
    assert verdict.tool_name == "decompile_function"


def test_same_args_same_content_is_an_identical_result() -> None:
    tracker = _tracker()
    tracker.observe_turn(1, [_tc(address="0x401000")], [_tr("same body")])
    verdict = tracker.observe_turn(2, [_tc(address="0x401000")], [_tr("same body")])

    assert verdict.repeat_kind is RepeatKind.IDENTICAL_RESULT
    assert verdict.occurrences == 2


def test_different_arguments_are_not_a_repeat() -> None:
    tracker = _tracker()
    tracker.observe_turn(1, [_tc(address="0x401000")], [_tr("body")])
    verdict = tracker.observe_turn(2, [_tc(address="0x402000")], [_tr("body")])

    assert verdict.repeat_kind is RepeatKind.NONE
    assert verdict.occurrences == 1


def test_argument_order_does_not_change_the_signature() -> None:
    tracker = _tracker()
    first = ToolCall(id="a", name="t", arguments={"x": 1, "y": 2})
    second = ToolCall(id="b", name="t", arguments={"y": 2, "x": 1})

    assert tracker.signature(first) == tracker.signature(second)


def test_mixed_batch_is_not_no_progress() -> None:
    tracker = _tracker()
    tracker.observe_turn(1, [_tc(address="0x401000")], [_tr("body")])
    verdict = tracker.observe_turn(
        2,
        [_tc(address="0x401000"), _tc(address="0x402000")],
        [_tr("body"), _tr("fresh body")],
    )

    assert verdict.no_progress is False


def test_fully_repeated_batch_is_no_progress() -> None:
    tracker = _tracker()
    tracker.observe_turn(1, [_tc(address="0x401000")], [_tr("body")])
    verdict = tracker.observe_turn(2, [_tc(address="0x401000")], [_tr("body")])

    assert verdict.no_progress is True


def test_reset_user_turn_clears_history() -> None:
    tracker = _tracker()
    tracker.observe_turn(1, [_tc(address="0x401000")], [_tr("body")])
    tracker.reset_user_turn()

    verdict = tracker.observe_turn(2, [_tc(address="0x401000")], [_tr("body")])
    assert verdict.repeat_kind is RepeatKind.NONE
    assert verdict.occurrences == 1
    assert tracker.run_made_progress() is True  # the fresh result was new


def test_progress_is_reported_only_for_new_results_or_mutations() -> None:
    tracker = _tracker()
    assert tracker.run_made_progress() is False

    tracker.observe_turn(1, [_tc(address="0x401000")], [_tr("body")])
    assert tracker.run_made_progress() is True

    tracker.reset_user_turn()
    tracker.note_mutation_success()
    assert tracker.run_made_progress() is True


def test_repeat_outside_the_window_is_not_counted() -> None:
    tracker = _tracker()
    tracker.observe_turn(1, [_tc(address="0x401000")], [_tr("body")])
    # The repeat window is 12 turns; turn 20 is outside it.
    verdict = tracker.observe_turn(20, [_tc(address="0x401000")], [_tr("body")])

    assert verdict.repeat_kind is RepeatKind.NONE
    assert verdict.occurrences == 1
