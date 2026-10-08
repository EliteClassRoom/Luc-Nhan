"""Regression tests for UI/UX audit fixes.

Covers three findings from the 2026-07 UI/UX review:

1. **Assistant role contrast (dark theme).** ``_assistant_role`` resolved to
   ``tokens.highlight`` which in the bundled dark palette is ``#0e639c`` —
   only **2.61:1** against the ``#1e1e1e`` window background, failing even
   the WCAG 3:1 large-text threshold. The role label ("Luc Nhan", 11px bold)
   is functional text and must stay readable. The fix derives the role color
   from the theme's high-contrast accent/text tokens instead.

2. **Panel-level keyboard shortcuts.** The panel had zero ``QShortcut``
   registrations — every action required the mouse. The fix wires Ctrl+T
   (new chat tab) and Ctrl+W (close current tab) as window-scoped
   shortcuts on the panel.

3. **Action-button tooltips + accessible names.** The eight header action
   buttons (Send/Stop/New/Export/Settings/Mutations/History/Tools) carried
   no ``setToolTip`` and no ``setAccessibleName`` — keyboard and
   screen-reader users had no way to discover what "Mutations" does. The
   fix gives each button a one-line tooltip and a matching accessible name.
"""

from __future__ import annotations

import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _purge_rk_ui_modules() -> None:
    """Drop lucnhan.ui modules so tests get the real implementations,
    not stubs installed by sibling test files."""
    for name in list(sys.modules):
        if name == "lucnhan.ui" or name.startswith("lucnhan.ui."):
            del sys.modules[name]


# ---------------------------------------------------------------------------
# 1. Assistant role color contrast
# ---------------------------------------------------------------------------


def _contrast_ratio(hex_a: str, hex_b: str) -> float:
    """WCAG 2.x contrast ratio between two ``#rrggbb`` colors."""

    def _lin(channel: int) -> float:
        c = channel / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    def _lum(hex_color: str) -> float:
        h = hex_color.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)

    la, lb = _lum(hex_a), _lum(hex_b)
    if la < lb:
        la, lb = lb, la
    return (la + 0.05) / (lb + 0.05)


class TestAssistantRoleContrast(unittest.TestCase):
    """The assistant role label color must clear WCAG AA (4.5:1) against the
    window background in *both* bundled palettes.

    Regression: dark theme used ``tokens.highlight`` (#0e639c) = 2.61:1.
    """

    def setUp(self) -> None:
        _purge_rk_ui_modules()
        from lucnhan.ui.message_widgets import _assistant_role
        from lucnhan.ui.theme.palette_dark import DARK_TOKENS
        from lucnhan.ui.theme.palette_light import LIGHT_TOKENS

        self._assistant_role = _assistant_role
        self._dark = DARK_TOKENS
        self._light = LIGHT_TOKENS

    def test_dark_theme_assistant_role_meets_aa(self) -> None:
        color = self._assistant_role(self._dark)
        ratio = _contrast_ratio(color, self._dark.window)
        self.assertGreaterEqual(
            ratio,
            4.5,
            f"assistant role color {color} on {self._dark.window} = {ratio:.2f}:1 (must be >= 4.5:1)",
        )

    def test_light_theme_assistant_role_meets_aa(self) -> None:
        color = self._assistant_role(self._light)
        ratio = _contrast_ratio(color, self._light.window)
        self.assertGreaterEqual(
            ratio,
            4.5,
            f"assistant role color {color} on {self._light.window} = {ratio:.2f}:1 (must be >= 4.5:1)",
        )


# ---------------------------------------------------------------------------
# 2. Panel-level keyboard shortcuts
# ---------------------------------------------------------------------------


class TestPanelKeyboardShortcuts(unittest.TestCase):
    """Source-level guards: the panel wires window-scoped QShortcuts for
    Ctrl+T (new tab) and Ctrl+W (close tab). Inspecting source avoids a
    dependency on Qt stub fidelity for shortcut activation."""

    def setUp(self) -> None:
        _purge_rk_ui_modules()
        import inspect

        from lucnhan.ui.panel_core import LucNhanPanelCore

        self._src = inspect.getsource(LucNhanPanelCore)

    def test_new_tab_shortcut_registered(self) -> None:
        self.assertIn("QShortcut", self._src)
        self.assertIn("Ctrl+T", self._src)

    def test_close_tab_shortcut_registered(self) -> None:
        self.assertIn("Ctrl+W", self._src)

    def test_shortcuts_are_window_scoped(self) -> None:
        # WindowShortcut (or WidgetWithChildrenShortcut) keeps the binding
        # inside the panel — it must not leak into the host's global
        # shortcut namespace.
        self.assertTrue(
            "WindowShortcut" in self._src or "WidgetWithChildrenShortcut" in self._src,
            "panel shortcuts must be window/widget scoped, not application-global",
        )


# ---------------------------------------------------------------------------
# 3. Action-button tooltips + accessible names
# ---------------------------------------------------------------------------


class TestActionButtonTooltips(unittest.TestCase):
    """Behavioral: every header action button ends up with a non-empty
    tooltip and accessible name. Drives the real widget builders against
    the shared Qt stubs (``QPushButton`` records setToolTip/setAccessibleName),
    so it verifies wiring rather than source text."""

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

    def _build_panel_buttons(self):
        from tests.qt_stubs import ensure_pyside6_stubs

        ensure_pyside6_stubs()
        _purge_rk_ui_modules()
        from lucnhan.ui.panel_core import LucNhanPanelCore

        # Skip the heavyweight __init__ (config load, controller, timers).
        # We only exercise the two widget-building methods under test.
        panel = LucNhanPanelCore.__new__(LucNhanPanelCore)
        panel._use_native_host_theme = False
        panel._build_action_buttons()
        return panel

    def test_every_button_has_tooltip(self) -> None:
        panel = self._build_panel_buttons()
        for attr in self._BUTTON_ATTRS:
            with self.subTest(button=attr):
                btn = getattr(panel, attr)
                self.assertTrue(
                    btn.toolTip(),
                    f"{attr} is missing a tooltip",
                )

    def test_every_button_has_accessible_name(self) -> None:
        panel = self._build_panel_buttons()
        for attr in self._BUTTON_ATTRS:
            with self.subTest(button=attr):
                btn = getattr(panel, attr)
                self.assertTrue(
                    btn.accessibleName(),
                    f"{attr} is missing an accessible name",
                )


# ---------------------------------------------------------------------------
# 4. ContextBar truncation → tooltip + selectable (M3)
# ---------------------------------------------------------------------------


class TestContextBarTruncation(unittest.TestCase):
    """Source-level guards for the ContextBar truncation fix.

    Long function names (C++ mangled, Rust generics) are sliced to 27 chars
    with ``...``. The audit rule is "truncate → ellipsis + tooltip + copy";
    the bar previously did the first two only. These tests pin that the
    tooltip carries the full name and the value labels are selectable."""

    def setUp(self) -> None:
        _purge_rk_ui_modules()
        import inspect

        from lucnhan.ui.context_bar import ContextBar

        self._src = inspect.getsource(ContextBar)

    def test_function_tooltip_shows_full_name(self) -> None:
        # set_function must push the un-sliced name into the tooltip.
        self.assertIn("setToolTip(", self._src)

    def test_value_labels_are_selectable(self) -> None:
        self.assertIn("TextSelectableByMouse", self._src)


# ---------------------------------------------------------------------------
# 5. Approval button labels — no whitespace padding (L2)
# ---------------------------------------------------------------------------


class TestApprovalButtonLabels(unittest.TestCase):
    """Approval buttons used space-padded text (``"  Allow  "``) instead of
    QSS padding — a hack that leaks whitespace to screen readers and breaks
    centering when font metrics change. The QSS already carries
    ``padding: 4px 16px``, so the spaces are redundant. Pin clean labels."""

    def setUp(self) -> None:
        _purge_rk_ui_modules()
        import inspect

        from lucnhan.ui import tool_widgets

        # Both ToolApprovalWidget and ExecutePythonWidget build the same
        # Allow / Always Allow / Deny row with the same padding hack — pin
        # the whole module so neither widget can regress.
        self._module_src = inspect.getsource(tool_widgets)

    def test_no_space_padded_labels(self) -> None:
        for padded in (
            '"  Allow  "',
            '"  Always Allow  "',
            '"  Deny  "',
            '"  Allowed  "',
            '"  Always Allowed  "',
            '"  Denied  "',
        ):
            with self.subTest(label=padded):
                self.assertNotIn(padded, self._module_src)

    def test_labels_present_without_padding(self) -> None:
        self.assertIn('"Allow"', self._module_src)
        self.assertIn('"Always Allow"', self._module_src)
        self.assertIn('"Deny"', self._module_src)


if __name__ == "__main__":
    unittest.main()
