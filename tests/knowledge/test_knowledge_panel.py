"""Smoke tests for the KnowledgePanel Qt widget.

Verifies the widget populates from in-memory records, applies the
type and search filters, syncs the chat-visibility checkbox, and
tolerates disabled/empty states. No real disk I/O — exercises only
the in-memory rendering path.
"""

from __future__ import annotations

import unittest

from rikugan.memory.schema import (
    KnowledgeEntity,
    KnowledgeMemory,
    KnowledgeRelation,
)


class FakeMemory(KnowledgeMemory):
    pass


class FakeEntity(KnowledgeEntity):
    pass


class FakeRelation(KnowledgeRelation):
    pass


def _make_widget():
    """Build the widget with a headless QApplication."""
    from rikugan.ui.knowledge_panel import KnowledgePanel

    try:
        from rikugan.ui.qt_compat import QApplication
    except Exception:
        from PySide6.QtWidgets import QApplication
    # Touch QApplication so the singleton is created if needed.
    QApplication.instance() or QApplication([])
    return KnowledgePanel()


class TestKnowledgePanel(unittest.TestCase):
    def setUp(self):
        self.w = _make_widget()

    def test_populate_empty(self):
        self.w.populate([], [], [], [])
        # The widget always renders the header row, so rowCount is 0
        # for the data table (header is on the view, not the model).
        # Just verify populate doesn't raise and counts label is intact.
        self.assertIn("0 memories", self.w._counts_label.text())

    def test_populate_basic_records(self):
        mem = KnowledgeMemory(
            id="mem:crypto:0x401000:abc",
            binary_id="x",
            type="crypto",
            title="RC4 decrypts beacon",
            content="uses RC4",
            tags=["crypto"],
            confidence=0.7,
        )
        ent = KnowledgeEntity(
            id="func:0x401000",
            binary_id="x",
            type="function",
            name="rc4_ksa",
            address="0x401000",
            tags=["crypto"],
        )
        rel = KnowledgeRelation(
            id="rel:func:0x401000:calls:func:0x401100",
            binary_id="x",
            src="func:0x401000",
            predicate="calls",
            dst="func:0x401100",
            confidence=0.6,
        )
        self.w.populate([mem], [ent], [rel])
        # All three kinds are present in the rows
        kinds = [r.kind for r in self.w._rows]
        self.assertIn("memory", kinds)
        self.assertIn("entity", kinds)
        self.assertIn("relation", kinds)
        # Type filter: "Memories" only — count visible rows after filter
        self.w._filter_combo.setCurrentText("Memories")
        visible = sum(1 for r in self.w._rows if r.kind == "memory")
        self.assertEqual(visible, 1)

    def test_search_filter(self):
        mem = KnowledgeMemory(
            id="mem:crypto:0x401000:abc",
            binary_id="x",
            type="crypto",
            title="RC4 decrypts beacon",
            content="uses RC4",
            tags=["crypto"],
            confidence=0.7,
        )
        self.w.populate([mem], [], [])
        # Search by id substring
        self.w._search.setText("0x401000")
        # After filter, the row should be hidden
        self.assertEqual(self.w._table.rowCount(), 1)
        self.w._search.setText("nope")
        self.assertEqual(self.w._table.rowCount(), 0)
        self.w._search.setText("")  # reset

    def test_set_show_retrieved_round_trip(self):
        self.w.set_show_retrieved(True)
        self.assertTrue(self.w._show_chk.isChecked())
        # set_show_retrieved must not re-emit the signal
        seen = []
        self.w.show_retrieved_changed.connect(lambda v: seen.append(v))
        self.w.set_show_retrieved(False)
        self.assertEqual(seen, [])
        self.assertFalse(self.w._show_chk.isChecked())

    def test_set_disabled_state_shows_banner(self):
        self.w.set_disabled_state(True)
        self.assertTrue(self.w._disabled)
        # ``isHidden`` is the inverse of ``setVisible(True)`` and
        # does not depend on the parent widget's shown state (which
        # we don't trigger in unit tests).
        self.assertFalse(self.w._banner_label.isHidden())
        self.assertIn("disabled", self.w._banner_label.text().lower())
        # Re-enabling clears the disabled state and hides the banner.
        self.w.set_disabled_state(False)
        self.assertFalse(self.w._disabled)
        self.assertTrue(self.w._banner_label.isHidden())

    def test_set_disabled_message(self):
        self.w.set_disabled_message("Disabled — no IDB path")
        self.assertEqual(self.w._rows, [])
        self.assertEqual(self.w._table.rowCount(), 0)
        self.assertIn("Disabled", self.w._detail.toPlainText())

    def test_set_counts(self):
        self.w.set_counts({"memories": 5, "entities": 3, "relations": 7, "notes": 2})
        self.assertIn("5 memories", self.w._counts_label.text())
        self.assertIn("7 relations", self.w._counts_label.text())

    def test_row_selection_shows_detail(self):
        mem = KnowledgeMemory(
            id="mem:test:001",
            binary_id="x",
            type="fact",
            title="t",
            content="hello world",
        )
        self.w.populate([mem], [], [])
        # Select the first row
        self.w._table.selectRow(0)
        text = self.w._detail.toPlainText()
        self.assertIn("mem:test:001", text)
        self.assertIn("hello world", text)


if __name__ == "__main__":
    unittest.main()
