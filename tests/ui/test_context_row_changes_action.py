"""REAL-module regression tests for the G3 context-row ``Changes`` action.

GUI contract G3 fixes the context-row sequence as
``label -> symbol -> address -> optional binary metadata -> Changes
action/count``, with Changes opening Mutations.  Mutations therefore lives
in the CONTEXT row, not in the composer's action row.

This file deliberately lives outside ``test_action_button_layout.py``:
``tests/tools/test_panel_core.py`` installs a ``types.ModuleType`` stub for
``lucnhan.ui.context_bar`` at import time, so whichever module imports
``panel_core`` first in a session decides whether it binds the real context
bar.  A test that needs the REAL module cannot live in a file that another
file can stub out from under it.
"""

from __future__ import annotations

import unittest

from tests.qt_real import requires_real_qt


class _StubInputArea:
    """The composer's editor is irrelevant here; this file builds chrome."""

    def set_submit_callback(self, cb) -> None:
        pass

    def set_cancel_callback(self, cb) -> None:
        pass

    def set_skill_slugs(self, slugs) -> None:
        pass


def _ensure_qapplication():
    """Real Qt needs a live QApplication before any widget is built."""
    from lucnhan.ui.qt_compat import QApplication

    return QApplication.instance() or QApplication([])


def _panel_with_context_row():
    from lucnhan.ui.panel_core import LucNhanPanelCore

    _ensure_qapplication()

    panel = LucNhanPanelCore.__new__(LucNhanPanelCore)
    panel._use_native_host_theme = True
    panel._config = type("_Cfg", (), {"provider": type("_P", (), {"name": "n", "model": "m"})()})()
    return panel._build_context_bar_for_tests(), panel


@requires_real_qt
class TestContextRowChangesAction(unittest.TestCase):
    """G3: the context row ends with the Changes action/count."""

    def test_changes_button_is_the_mutations_action(self) -> None:
        from PySide6.QtCore import Qt

        bar, panel = _panel_with_context_row()
        self.assertIs(panel._mutations_btn, bar._changes_btn)
        self.assertEqual(bar._changes_btn.text(), "Changes")
        self.assertTrue(bar._changes_btn.toolTip())
        self.assertIn("mutation", bar._changes_btn.accessibleName().lower())
        self.assertEqual(bar._changes_btn.focusPolicy(), Qt.FocusPolicy.StrongFocus)

    def test_changes_survives_the_narrow_breakpoint(self) -> None:
        """G3 only lets *secondary metadata* hide at narrow, not actions."""
        bar, _panel = _panel_with_context_row()
        bar.set_narrow(True)
        # The row is not shown here, so read the widget flag (``isHidden`` is
        # only meaningful once the ancestor chain is visible).
        self.assertTrue(bar._model_label[0].isHidden(), "MODEL: hides at narrow")
        self.assertTrue(bar._tokens_label[0].isHidden(), "TOKENS: hides at narrow")
        self.assertFalse(bar._changes_btn.isHidden(), "Changes must stay reachable")
        bar.set_narrow(False)
        self.assertFalse(bar._model_label[0].isHidden(), "MODEL: returns when wide")

    def test_count_follows_recorded_mutations(self) -> None:
        bar, _panel = _panel_with_context_row()
        self.assertEqual(bar.mutation_count(), 0)
        bar.set_mutation_count(3)
        self.assertEqual(bar.mutation_count(), 3)
        self.assertEqual(bar._changes_btn.text(), "Changes · 3")
        self.assertIn("3 recorded", bar._changes_btn.accessibleName())
        bar.set_mutation_count(1)
        self.assertEqual(bar._changes_btn.text(), "Changes · 1")

    def test_changes_click_emits_the_request(self) -> None:
        bar, _panel = _panel_with_context_row()
        seen: list[int] = []
        bar.changes_requested.connect(lambda: seen.append(1))
        bar._changes_btn.click()
        self.assertEqual(seen, [1])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
