"""Tests for the rate-limited reminder scheduler."""

from __future__ import annotations

from lucnhan.agent.reminders import (
    DEFAULT_POLICIES,
    NO_PROGRESS_REMINDER,
    TOOL_REPEAT_REMINDER,
    ReminderKind,
    ReminderPolicy,
    ReminderScheduler,
)


def test_tool_repeat_reminder_is_gap_limited() -> None:
    scheduler = ReminderScheduler()

    assert scheduler.should_emit(ReminderKind.TOOL_REPEAT, 1) is True
    scheduler.mark_emitted(ReminderKind.TOOL_REPEAT, 1)

    # min_gap_turns == 2 — the next turn is too soon.
    assert scheduler.should_emit(ReminderKind.TOOL_REPEAT, 2) is False

    assert scheduler.should_emit(ReminderKind.TOOL_REPEAT, 3) is True


def test_tool_repeat_reminder_respects_the_per_run_budget() -> None:
    scheduler = ReminderScheduler()

    scheduler.mark_emitted(ReminderKind.TOOL_REPEAT, 1)
    scheduler.mark_emitted(ReminderKind.TOOL_REPEAT, 3)

    # max_per_run == 2 — the third emission is refused.
    assert scheduler.should_emit(ReminderKind.TOOL_REPEAT, 5) is False


def test_reset_run_restores_the_budget_and_the_gap() -> None:
    scheduler = ReminderScheduler()
    scheduler.mark_emitted(ReminderKind.TOOL_REPEAT, 1)
    scheduler.mark_emitted(ReminderKind.TOOL_REPEAT, 3)
    assert scheduler.should_emit(ReminderKind.TOOL_REPEAT, 4) is False

    scheduler.reset_run()

    # The budget is back to two emissions...
    assert scheduler.should_emit(ReminderKind.TOOL_REPEAT, 4) is True
    scheduler.mark_emitted(ReminderKind.TOOL_REPEAT, 4)
    # ...and the gap applies again from the new emission.
    assert scheduler.should_emit(ReminderKind.TOOL_REPEAT, 5) is False
    assert scheduler.should_emit(ReminderKind.TOOL_REPEAT, 6) is True


def test_budgets_are_tracked_per_kind() -> None:
    scheduler = ReminderScheduler()
    scheduler.mark_emitted(ReminderKind.TOOL_REPEAT, 1)

    assert scheduler.should_emit(ReminderKind.NO_PROGRESS, 1) is True
    assert scheduler.should_emit(ReminderKind.TOOL_REPEAT, 1) is False


def test_custom_policies_are_honoured() -> None:
    scheduler = ReminderScheduler({ReminderKind.TOOL_REPEAT: ReminderPolicy(max_per_run=1, min_gap_turns=0)})

    assert scheduler.should_emit(ReminderKind.TOOL_REPEAT, 1) is True
    scheduler.mark_emitted(ReminderKind.TOOL_REPEAT, 1)
    assert scheduler.should_emit(ReminderKind.TOOL_REPEAT, 2) is False


def test_default_policies_cover_every_kind() -> None:
    assert set(DEFAULT_POLICIES) == set(ReminderKind)
    for policy in DEFAULT_POLICIES.values():
        assert policy.max_per_run >= 1
        assert policy.min_gap_turns >= 0


def test_reminder_texts_name_the_tool_and_the_count() -> None:
    text = TOOL_REPEAT_REMINDER.format(tool="decompile_function", count=3)
    assert "decompile_function" in text
    assert "3 times" in text
    assert NO_PROGRESS_REMINDER.startswith("[SYSTEM]")
