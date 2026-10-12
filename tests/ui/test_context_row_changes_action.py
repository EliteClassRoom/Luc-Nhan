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

from lucnhan.ui.qt_compat import QWidget
from tests.qt_real import requires_real_qt


class _StubInputArea(QWidget):
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


def _panel_with_context_row(*, with_workspace_buttons: bool = False):
    """A panel carrying the context row's ``Changes`` action.

    ``with_workspace_buttons`` additionally builds the header and the
    composer, which is what ``_apply_action_button_styles`` /
    ``_apply_action_button_tooltips`` iterate over — needed to prove the
    context row's button survives those sweeps.
    """
    import lucnhan.ui.panel_core as panel_core
    from lucnhan.ui.panel_core import LucNhanPanelCore

    _ensure_qapplication()

    from lucnhan.ui.qt_compat import QWidget

    panel = LucNhanPanelCore.__new__(LucNhanPanelCore)
    QWidget.__init__(panel)
    panel._config = type("_Cfg", (), {"provider": type("_P", (), {"name": "n", "model": "m"})()})()
    if not with_workspace_buttons:
        return panel._build_context_bar_for_tests(), panel

    panel._ctrl = type("_Ctrl", (), {"skill_slugs": []})()
    panel._skills_refresh_timer = None
    panel._build_header()
    original = panel_core.InputArea
    panel_core.InputArea = _StubInputArea
    try:
        panel._build_input_section()
    finally:
        panel_core.InputArea = original
    panel._build_context_bar_for_tests()
    return panel._context_bar, panel


@requires_real_qt
class TestContextRowChangesAction(unittest.TestCase):
    """G3: the context row ends with the Changes action/count."""

    def test_changes_button_is_the_mutations_action(self) -> None:
        from PySide6.QtCore import Qt

        bar, panel = _panel_with_context_row()
        self.assertIs(panel._mutations_btn, bar.changes_button())
        self.assertEqual(bar.changes_button().text(), "Changes")
        self.assertTrue(bar.changes_button().toolTip())
        self.assertIn("mutation", bar.changes_button().accessibleName().lower())
        self.assertEqual(bar.changes_button().focusPolicy(), Qt.FocusPolicy.StrongFocus)

    def test_row_does_not_repeat_the_footer_readouts(self) -> None:
        """Model and token usage live in the footer only; the row must not
        carry a second copy (it showed the same numbers twice)."""
        from PySide6.QtWidgets import QLabel

        bar, _panel = _panel_with_context_row()
        texts = " ".join(label.text() for label in bar.findChildren(QLabel))
        self.assertNotIn("TOKENS", texts)
        self.assertNotIn("MODEL", texts)
        self.assertFalse(bar.changes_button().isHidden(), "Changes must stay reachable")

    def test_count_follows_recorded_mutations(self) -> None:
        bar, _panel = _panel_with_context_row()
        self.assertEqual(bar.mutation_count(), 0)
        bar.set_mutation_count(3)
        self.assertEqual(bar.mutation_count(), 3)
        self.assertEqual(bar.changes_button().text(), "Changes · 3")
        self.assertIn("3 recorded", bar.changes_button().accessibleName())
        bar.set_mutation_count(1)
        self.assertEqual(bar.changes_button().text(), "Changes · 1")

    def test_changes_style_survives_the_workspace_sweeps(self) -> None:
        """MANDATORY-1: the context row owns this button's chrome.

        ``_apply_action_button_styles`` / ``_apply_action_button_tooltips``
        used to sweep ``_mutations_btn`` too.  A widget's own stylesheet wins
        over the frame-level ``QFrame#context_bar QPushButton#...`` rules, so
        the flat 11 px style was overridden with push-button chrome (which
        wants ~30 px inside a fixed 27 px row), and the accessible name was
        doubled to ``Changes - Changes - ...``.
        """
        from lucnhan.ui.context_bar import ContextBar

        bar, panel = _panel_with_context_row(with_workspace_buttons=True)
        # Run both workspace sweeps exactly as a theme switch would.
        panel._apply_action_button_styles()
        panel._apply_action_button_tooltips()
        bar.apply_theme()

        self.assertEqual(
            bar.changes_button().styleSheet(),
            "",
            "no widget-level stylesheet may override the context row",
        )
        self.assertIn("QPushButton#context_changes_btn", bar.styleSheet())
        self.assertIn("font-size: 11px", bar.styleSheet())

        # Label / description must be the ContextBar's own, exactly once.
        self.assertEqual(bar.changes_button().text(), "Changes")
        name = bar.changes_button().accessibleName()
        self.assertEqual(name, "Changes \u2014 show the mutation log (0 recorded)")
        self.assertEqual(bar.changes_button().toolTip(), "Show the mutation log (IDA database edits made by the agent)")

        # And the rendered button must fit the fixed-height row (G2: 27 px).
        bar.resize(900, ContextBar._ROW_HEIGHT)
        bar.changes_button().ensurePolished()
        self.assertLessEqual(
            bar.changes_button().sizeHint().height(),
            ContextBar._ROW_HEIGHT,
            "the Changes action must not overflow the 27 px context row",
        )

    def test_changes_click_is_the_panel_wiring_point(self) -> None:
        """The button's own ``clicked`` is the contract; the panel connects it."""
        bar, panel = _panel_with_context_row()
        seen: list[str] = []
        panel._mutations_btn.clicked.connect(lambda: seen.append("clicked"))
        bar.changes_button().click()
        self.assertEqual(seen, ["clicked"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
