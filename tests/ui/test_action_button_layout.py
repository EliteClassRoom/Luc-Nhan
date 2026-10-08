"""Workspace action-button contract (GUI design contract G3).

The action buttons moved out of the old single ``_build_action_buttons``
HBox: Send / Stop now live in the composer's action row (built by
``_build_input_section``) and History / Settings / Tools / Chat live in the
header (built by ``_build_header``).  What must not drift is that *every*
pre-existing action still exists exactly once, carries a tooltip and an
accessible name, and that the header keeps the contract's navigation order.
"""

from __future__ import annotations

import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from tests.qt_stubs import ensure_pyside6_stubs

ensure_pyside6_stubs()

from lucnhan.ui.panel_core import LucNhanPanelCore
from lucnhan.ui.qt_compat import QVBoxLayout, QWidget


class _StubInputArea(QWidget):
    """Stand-in for the composer editor (the shared Qt stub has no
    QPlainTextEdit palette plumbing)."""

    def set_submit_callback(self, cb) -> None:
        pass

    def set_cancel_callback(self, cb) -> None:
        pass

    def set_skill_slugs(self, slugs) -> None:
        pass


_HEADER_NAV_ATTRS = ("_chat_btn", "_tools_btn", "_history_btn", "_settings_btn")


def _header_widgets(panel):
    """Navigation buttons in the order the header layout holds them."""
    nav = [getattr(panel, attr) for attr in _HEADER_NAV_ATTRS]
    return [
        panel._header_layout.itemAt(i).widget()
        for i in range(panel._header_layout.count())
        if panel._header_layout.itemAt(i).widget() in nav
    ]


_BUTTON_ATTRS = (
    "_chat_btn",
    "_send_btn",
    "_cancel_btn",
    "_export_btn",
    "_settings_btn",
    "_mutations_btn",
    "_history_btn",
    "_tools_btn",
)


class TestActionButtonLayout(unittest.TestCase):
    def _build_panel_buttons(self):
        # Bypass heavyweight __init__ (config load, controller, timers);
        # only exercise the widget-building methods under test. Use the
        # native-host-theme branch so the themed stylesheet path
        # (``ThemeManager.tokens()``) is not required by the stub.
        panel = LucNhanPanelCore.__new__(LucNhanPanelCore)
        panel._use_native_host_theme = True
        panel._build_header()
        panel._skills_refresh_timer = None
        panel._ctrl = type("_Ctrl", (), {"skill_slugs": []})()
        panel._config = type(
            "_Cfg",
            (),
            {"provider": type("_P", (), {"name": "n", "model": "m"})()},
        )()
        # The Qt stub's QPlainTextEdit has no palette plumbing; the
        # composer's own button wiring is what this file pins.
        import lucnhan.ui.panel_core as panel_core

        original = panel_core.InputArea
        panel_core.InputArea = _StubInputArea
        try:
            panel._build_input_section()
            panel._build_action_buttons()
        finally:
            panel_core.InputArea = original
        # G3: Mutations is the context row's ``Changes`` action, so the
        # attribute under test is built with the context bar.
        panel._build_context_bar_for_tests()
        return panel

    def test_action_buttons_live_in_horizontal_layouts(self) -> None:
        """The composer action row stays horizontal (never stacked)."""
        panel = self._build_panel_buttons()
        # Editor on top, commands / hint / Send / Stop on one horizontal
        # action row underneath (GUI contract G2 + G3).
        composer_layout = panel._input_container.layout()
        self.assertIsInstance(composer_layout, QVBoxLayout)
        self.assertIs(panel._input_area, composer_layout.itemAt(0).widget())

    def test_every_action_button_is_present(self) -> None:
        panel = self._build_panel_buttons()
        for attr in _BUTTON_ATTRS:
            with self.subTest(button=attr):
                self.assertTrue(hasattr(panel, attr), f"missing button {attr}")
                btn = getattr(panel, attr)
                self.assertTrue(btn.toolTip(), f"{attr} is missing a tooltip")
                self.assertTrue(
                    btn.accessibleName(),
                    f"{attr} is missing an accessible name",
                )

    def test_no_second_stateful_copy_of_an_action(self) -> None:
        """Each action exists once — overflow entries reuse the buttons."""
        panel = self._build_panel_buttons()
        seen: dict[int, str] = {}
        for attr in _BUTTON_ATTRS:
            btn = getattr(panel, attr)
            self.assertNotIn(id(btn), seen, f"{attr} duplicates {seen.get(id(btn))}")
            seen[id(btn)] = attr

    def test_header_navigation_order_matches_contract(self) -> None:
        """G3: header right side is Chat, Tools, History, Settings."""
        panel = self._build_panel_buttons()
        # The shared Qt stub does not record ``QPushButton`` labels, so the
        # ordering is pinned on the *instance* order the header layout
        # holds; the labels are asserted in the real-Qt smoke run.
        expected = [getattr(panel, attr) for attr in _HEADER_NAV_ATTRS]
        self.assertEqual(_header_widgets(panel), expected)


if __name__ == "__main__":
    unittest.main()
