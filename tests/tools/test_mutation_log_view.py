"""Tests for lucnhan.ui.mutation_log_view — pure list logic."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from tests.qt_stubs import ensure_pyside6_stubs

ensure_pyside6_stubs()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(reversible: bool = True, description: str = "desc", tool_name: str = "tool") -> MagicMock:
    rec = MagicMock()
    rec.reversible = reversible
    rec.description = description
    rec.tool_name = tool_name
    rec.timestamp = 0.0
    return rec


def _make_panel():
    """Build a MutationLogPanel with all Qt calls mocked out."""
    from lucnhan.ui.mutation_log_view import MutationLogPanel
    # Use ``MutationLogPanel.__new__`` rather than ``object.__new__``
    # — ``MutationLogPanel`` inherits from ``QFrame`` (a C-level Qt
    # class) which rejects ``object.__new__`` with ``TypeError``.
    # The class-bound ``__new__`` delegates to the C-level allocator
    # and is the idiom used in sibling test files
    # (``test_panel_core.py`` and ``test_settings_dialog.py``).
    panel = MutationLogPanel.__new__(MutationLogPanel)
    panel._entries = []
    panel._count_label = MagicMock()
    panel._undo_btn = MagicMock()
    panel._entries_layout = MagicMock()
    return panel


# ---------------------------------------------------------------------------
# _update_count — label text and button state
# ---------------------------------------------------------------------------

class TestUpdateCount(unittest.TestCase):
    def setUp(self):
        self.panel = _make_panel()

    def _make_entry(self, reversible: bool):
        entry = MagicMock()
        entry.record = _make_record(reversible=reversible)
        return entry

    def test_zero_entries_label(self):
        self.panel._update_count()
        self.panel._count_label.setText.assert_called_with("0 mutations")

    def test_one_entry_singular(self):
        self.panel._entries = [self._make_entry(True)]
        self.panel._update_count()
        self.panel._count_label.setText.assert_called_with("1 mutation")

    def test_two_entries_plural(self):
        self.panel._entries = [self._make_entry(True), self._make_entry(True)]
        self.panel._update_count()
        self.panel._count_label.setText.assert_called_with("2 mutations")

    def test_three_entries_plural(self):
        self.panel._entries = [self._make_entry(True)] * 3
        self.panel._update_count()
        self.panel._count_label.setText.assert_called_with("3 mutations")

    def test_undo_disabled_when_empty(self):
        self.panel._update_count()
        self.panel._undo_btn.setEnabled.assert_called_with(False)

    def test_undo_enabled_when_reversible_entry(self):
        self.panel._entries = [self._make_entry(True)]
        self.panel._update_count()
        self.panel._undo_btn.setEnabled.assert_called_with(True)

    def test_undo_disabled_when_all_irreversible(self):
        self.panel._entries = [self._make_entry(False), self._make_entry(False)]
        self.panel._update_count()
        self.panel._undo_btn.setEnabled.assert_called_with(False)

    def test_undo_enabled_when_mixed_reversibility(self):
        self.panel._entries = [self._make_entry(False), self._make_entry(True)]
        self.panel._update_count()
        self.panel._undo_btn.setEnabled.assert_called_with(True)


# ---------------------------------------------------------------------------
# remove_last
# ---------------------------------------------------------------------------

class TestRemoveLast(unittest.TestCase):
    def _panel_with_n(self, n: int, reversible: bool = True):
        panel = _make_panel()
        for _ in range(n):
            entry = MagicMock()
            entry.record = _make_record(reversible=reversible)
            panel._entries.append(entry)
        return panel

    def test_remove_one(self):
        panel = self._panel_with_n(3)
        panel.remove_last(1)
        self.assertEqual(len(panel._entries), 2)

    def test_remove_multiple(self):
        panel = self._panel_with_n(5)
        panel.remove_last(3)
        self.assertEqual(len(panel._entries), 2)

    def test_remove_more_than_available(self):
        panel = self._panel_with_n(2)
        panel.remove_last(10)
        self.assertEqual(len(panel._entries), 0)

    def test_remove_zero(self):
        panel = self._panel_with_n(3)
        panel.remove_last(0)
        self.assertEqual(len(panel._entries), 3)

    def test_remove_from_empty(self):
        panel = self._panel_with_n(0)
        panel.remove_last(5)  # must not raise
        self.assertEqual(len(panel._entries), 0)

    def test_remove_last_pops_tail(self):
        panel = self._panel_with_n(3)
        last_entry = panel._entries[-1]
        panel.remove_last(1)
        self.assertNotIn(last_entry, panel._entries)

    def test_remove_calls_delete_later(self):
        panel = self._panel_with_n(2)
        entry_to_remove = panel._entries[-1]
        panel.remove_last(1)
        entry_to_remove.deleteLater.assert_called_once()

    def test_remove_calls_remove_widget(self):
        panel = self._panel_with_n(2)
        entry_to_remove = panel._entries[-1]
        panel.remove_last(1)
        panel._entries_layout.removeWidget.assert_called_with(entry_to_remove)


# ---------------------------------------------------------------------------
# clear_all
# ---------------------------------------------------------------------------

class TestClearAll(unittest.TestCase):
    def _panel_with_n(self, n: int):
        panel = _make_panel()
        for _ in range(n):
            entry = MagicMock()
            entry.record = _make_record()
            panel._entries.append(entry)
        return panel

    def test_clears_all_entries(self):
        panel = self._panel_with_n(4)
        panel.clear_all()
        self.assertEqual(len(panel._entries), 0)

    def test_clear_empty_noop(self):
        panel = self._panel_with_n(0)
        panel.clear_all()  # must not raise
        self.assertEqual(len(panel._entries), 0)

    def test_clear_calls_delete_later_for_each(self):
        panel = self._panel_with_n(3)
        entries = list(panel._entries)
        panel.clear_all()
        for e in entries:
            e.deleteLater.assert_called_once()

    def test_clear_calls_remove_widget_for_each(self):
        panel = self._panel_with_n(3)
        entries = list(panel._entries)
        panel.clear_all()
        for e in entries:
            panel._entries_layout.removeWidget.assert_any_call(e)

    def test_count_label_updated_after_clear(self):
        panel = self._panel_with_n(3)
        panel.clear_all()
        panel._count_label.setText.assert_called_with("0 mutations")


if __name__ == "__main__":
    unittest.main()
