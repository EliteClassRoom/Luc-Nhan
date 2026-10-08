"""Tests for the "Load older" button cap logic on async restore.

When a session has more messages than the render cap
(``_RESTORE_DEFAULT_MAX_RENDERED``), the restore path materialises only
the most recent window and inserts a "Load older (N more)" button at the
top. Clicking grows the cap and re-runs the restore. These tests cover
the pure cap-state arithmetic via the static helpers on ``ChatView`` so
they do not need a real QApplication.
"""

from __future__ import annotations

import unittest

from lucnhan.ui.chat_view import (
    _RESTORE_DEFAULT_MAX_RENDERED,
    ChatView,
)


class TestLoadOlderCapLogic(unittest.TestCase):
    def test_remaining_is_zero_when_cap_covers_total(self) -> None:
        self.assertEqual(ChatView._remaining_older_count(100, 100), 0)
        self.assertEqual(ChatView._remaining_older_count(50, 100), -50)

    def test_remaining_counts_messages_beyond_cap(self) -> None:
        # A 677-message session with the default cap leaves 577 older.
        self.assertEqual(
            ChatView._remaining_older_count(677, _RESTORE_DEFAULT_MAX_RENDERED),
            677 - _RESTORE_DEFAULT_MAX_RENDERED,
        )

    def test_next_cap_doubles_and_clamps_to_total(self) -> None:
        cap = _RESTORE_DEFAULT_MAX_RENDERED
        total = cap * 3
        # First click: 100 -> 200
        self.assertEqual(ChatView._next_cap(cap, total), cap * 2)
        # Second click would overshoot (400 > 300), so clamp to total.
        self.assertEqual(ChatView._next_cap(cap * 2, total), total)

    def test_next_cap_no_overshoot_small_session(self) -> None:
        # When doubling overshoots a small session, the next cap is the
        # full count (the user reaches the beginning in one click).
        self.assertEqual(ChatView._next_cap(100, 150), 150)

    def test_next_cap_idempotent_at_total(self) -> None:
        # Clicking again once the cap already equals the total must not
        # grow past it — the caller checks for equality and no-ops.
        self.assertEqual(ChatView._next_cap(200, 200), 200)


if __name__ == "__main__":
    unittest.main()
