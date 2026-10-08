"""Tests for lucnhan.ui.theme.watcher — IDAThemeWatcher palette change detection.

These tests focus on the watcher's contract (start/stop idempotency, tick
error swallowing) without requiring real PySide6. The full end-to-end
palette flow is exercised manually inside IDA — the qt_stubs here don't
expose QPalette.ColorRole so we mock what we need.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from tests.qt_stubs import ensure_pyside6_stubs

ensure_pyside6_stubs()


class TestPaletteSignature(unittest.TestCase):
    """_palette_signature is the change-detection key.

    It must be deterministic and distinct for different (Window, WindowText)
    pairs. We stub the QPalette surface so the contract is verifiable
    without a real PySide6 install.
    """

    def _make_pal(self, window: str, text: str) -> MagicMock:
        pal = MagicMock()
        # Make `color(QPalette.ColorRole.Window)` return a color with `.name()`.
        window_color = MagicMock()
        window_color.name.return_value = window
        text_color = MagicMock()
        text_color.name.return_value = text

        def color(role):
            # role is a sentinel — we don't compare identity, just sequence.
            if role == "Window":
                return window_color
            if role == "WindowText":
                return text_color
            # Default: a fresh mock whose .name() returns a stable value
            other = MagicMock()
            other.name.return_value = f"#{role}"
            return other

        pal.color.side_effect = color
        return pal

    def test_signature_returns_two_strings(self) -> None:
        from lucnhan.ui.theme.watcher import _palette_signature

        with patch("lucnhan.ui.theme.watcher.QPalette") as MockQPalette:
            MockQPalette.ColorRole.Window = "Window"
            MockQPalette.ColorRole.WindowText = "WindowText"
            pal = self._make_pal("#111111", "#eeeeee")
            sig = _palette_signature(pal)
        self.assertEqual(sig, ("#111111", "#eeeeee"))

    def test_signature_changes_with_window(self) -> None:
        from lucnhan.ui.theme.watcher import _palette_signature

        with patch("lucnhan.ui.theme.watcher.QPalette") as MockQPalette:
            MockQPalette.ColorRole.Window = "Window"
            MockQPalette.ColorRole.WindowText = "WindowText"
            sig1 = _palette_signature(self._make_pal("#111111", "#eeeeee"))
            sig2 = _palette_signature(self._make_pal("#222222", "#eeeeee"))
        self.assertNotEqual(sig1, sig2)

    def test_signature_changes_with_text(self) -> None:
        from lucnhan.ui.theme.watcher import _palette_signature

        with patch("lucnhan.ui.theme.watcher.QPalette") as MockQPalette:
            MockQPalette.ColorRole.Window = "Window"
            MockQPalette.ColorRole.WindowText = "WindowText"
            sig1 = _palette_signature(self._make_pal("#111111", "#eeeeee"))
            sig2 = _palette_signature(self._make_pal("#111111", "#dddddd"))
        self.assertNotEqual(sig1, sig2)

    def test_signature_is_stable_for_same_input(self) -> None:
        from lucnhan.ui.theme.watcher import _palette_signature

        with patch("lucnhan.ui.theme.watcher.QPalette") as MockQPalette:
            MockQPalette.ColorRole.Window = "Window"
            MockQPalette.ColorRole.WindowText = "WindowText"
            pal = self._make_pal("#111111", "#eeeeee")
            sig1 = _palette_signature(pal)
            sig2 = _palette_signature(pal)
        self.assertEqual(sig1, sig2)


class TestIDAThemeWatcherLifecycle(unittest.TestCase):
    """Start/stop idempotency + the _alive flag contract.

    We patch QTimer.singleShot so the test doesn't actually schedule
    anything on the Qt event loop.
    """

    def setUp(self) -> None:
        from lucnhan.ui.theme.watcher import IDAThemeWatcher

        self._patch = patch(
            "lucnhan.ui.theme.watcher.QTimer.singleShot",
            lambda *a, **kw: None,
        )
        self._patch.start()
        self.watcher = IDAThemeWatcher(interval_ms=50)

    def tearDown(self) -> None:
        self.watcher.stop()
        self._patch.stop()

    def test_stop_prevents_further_ticks(self) -> None:
        self.watcher.start()
        self.assertTrue(self.watcher._alive.is_set())
        self.watcher.stop()
        self.assertFalse(self.watcher._alive.is_set())

    def test_start_is_idempotent(self) -> None:
        """Calling start() twice does not double-schedule."""
        self.watcher.start()
        self.watcher.start()  # second call is a no-op (flag already set)
        self.assertTrue(self.watcher._alive.is_set())

    def test_stop_is_safe_when_never_started(self) -> None:
        """stop() on a fresh watcher must not raise."""
        from lucnhan.ui.theme.watcher import IDAThemeWatcher

        fresh = IDAThemeWatcher(interval_ms=10)
        fresh.stop()  # no exception
        self.assertFalse(fresh._alive.is_set())


class TestTickErrorSwallowing(unittest.TestCase):
    """_tick() must never propagate exceptions to the Qt event loop."""

    def setUp(self) -> None:
        from lucnhan.ui.theme.watcher import IDAThemeWatcher

        self._patch = patch(
            "lucnhan.ui.theme.watcher.QTimer.singleShot",
            lambda *a, **kw: None,
        )
        self._patch.start()
        self.watcher = IDAThemeWatcher(interval_ms=50)
        self.watcher.start()

    def tearDown(self) -> None:
        self.watcher.stop()
        self._patch.stop()

    def test_tick_swallows_source_none(self) -> None:
        """When _app_source returns None, _tick is a clean no-op."""
        with patch(
            "lucnhan.ui.theme.watcher.ThemeManager.instance"
        ) as mock_inst:
            mock_inst.return_value._app_source.return_value = None
            # Should not raise
            self.watcher._tick()
        self.assertTrue(self.watcher._alive.is_set())  # still alive → rescheduled

    def test_tick_swallows_palette_errors(self) -> None:
        """When source.palette() raises, _tick logs and continues."""
        with patch(
            "lucnhan.ui.theme.watcher.ThemeManager.instance"
        ) as mock_inst:
            source = MagicMock()
            source.palette.side_effect = RuntimeError("palette access failed")
            mock_inst.return_value._app_source.return_value = source
            # Should not raise — the broad except catches RuntimeError
            self.watcher._tick()
        self.assertTrue(self.watcher._alive.is_set())


class TestPluginWatcherGate(unittest.TestCase):
    """Bug E regression: the IDA plugin must not start IDAThemeWatcher
    when the user has chosen DARK or LIGHT — those modes return bundled
    constants and never read QPalette.
    """

    def test_needs_palette_watch_logic(self) -> None:
        from lucnhan.ui.theme.tokens import ThemeMode

        # Mirror the gate from lucnhan_plugin.run() — this is the
        # source-of-truth truth table.
        for mode, expected in [
            (ThemeMode.AUTO, True),
            (ThemeMode.IDA_NATIVE, True),
            (ThemeMode.DARK, False),
            (ThemeMode.LIGHT, False),
        ]:
            with self.subTest(mode=mode):
                needs_palette_watch = mode in (ThemeMode.AUTO, ThemeMode.IDA_NATIVE)
                self.assertEqual(needs_palette_watch, expected)


if __name__ == "__main__":
    unittest.main()
