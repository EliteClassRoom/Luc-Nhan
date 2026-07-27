"""Regression test for the horizontal action-button layout.

Pins the contract that ``RikuganPanelCore._build_action_buttons`` returns a
``QHBoxLayout`` and that all eight action buttons remain present with their
tooltips/accessible names. The existing a11y tests already cover the
tooltips; this file is the layout-shape contract.
"""

from __future__ import annotations

import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from tests.qt_stubs import ensure_pyside6_stubs  # noqa: E402

ensure_pyside6_stubs()

from rikugan.ui.panel_core import RikuganPanelCore  # noqa: E402
from rikugan.ui.qt_compat import QHBoxLayout, QVBoxLayout  # noqa: E402


_BUTTON_ATTRS = (
    "_send_btn",
    "_cancel_btn",
    "_new_btn",
    "_export_btn",
    "_settings_btn",
    "_mutations_btn",
    "_history_btn",
    "_tools_btn",
)


class TestActionButtonLayout(unittest.TestCase):
    def _build_panel_buttons(self):
        # Bypass heavyweight __init__ (config load, controller, timers);
        # only exercise the widget-building method under test. Use the
        # native-host-theme branch so the themed stylesheet path
        # (``ThemeManager.tokens()``) is not required by the stub.
        panel = RikuganPanelCore.__new__(RikuganPanelCore)
        panel._use_native_host_theme = True
        panel._build_action_buttons()
        return panel

    def test_returns_horizontal_layout(self) -> None:
        panel = self._build_panel_buttons()
        layout = panel._build_action_buttons()
        self.assertIsInstance(layout, QHBoxLayout)
        # Not a vertical layout under any name.
        self.assertNotIsInstance(layout, QVBoxLayout)

    def test_all_eight_buttons_present(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
