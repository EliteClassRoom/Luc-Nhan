"""Tests for rikugan.ida.ui.panel — IDA PluginForm wrapper."""

from __future__ import annotations

import os
import sys
import types
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from tests.qt_stubs import _qt_class, ensure_pyside6_stubs

ensure_pyside6_stubs()

# Stub rikugan.ui.panel_core
_panel_core_mod = types.ModuleType("rikugan.ui.panel_core")
_panel_core_mod.RikuganPanelCore = _qt_class("RikuganPanelCore")
sys.modules.setdefault("rikugan.ui.panel_core", _panel_core_mod)

# Stub session/actions modules
_session_mod = types.ModuleType("rikugan.ida.ui.session_controller")
_session_mod.IdaSessionController = MagicMock()
_session_mod.SessionController = _session_mod.IdaSessionController  # backwards-compat alias
sys.modules["rikugan.ida.ui.session_controller"] = _session_mod

_actions_mod = types.ModuleType("rikugan.ida.ui.actions")
_actions_mod.RikuganUIHooks = MagicMock()
sys.modules["rikugan.ida.ui.actions"] = _actions_mod

from rikugan.ida.ui.panel import RikuganPanel

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_panel() -> RikuganPanel:
    """Create RikuganPanel bypassing __init__, injecting mock _core."""
    panel = object.__new__(RikuganPanel)
    panel._form_widget = None
    panel._root = None
    panel._core = None
    return panel


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------

class TestRikuganIdaPanelShutdown(unittest.TestCase):
    def test_shutdown_calls_core_shutdown(self):
        panel = _make_panel()
        mock_core = MagicMock()
        panel._core = mock_core
        panel.shutdown()
        mock_core.shutdown.assert_called_once()

    def test_shutdown_sets_core_to_none(self):
        panel = _make_panel()
        panel._core = MagicMock()
        panel.shutdown()
        self.assertIsNone(panel._core)

    def test_shutdown_noop_when_core_is_none(self):
        panel = _make_panel()
        panel._core = None
        panel.shutdown()  # must not raise


# ---------------------------------------------------------------------------
# prefill_input
# ---------------------------------------------------------------------------

class TestRikuganIdaPanelPrefillInput(unittest.TestCase):
    def test_prefill_delegates_when_core_available(self):
        panel = _make_panel()
        panel._core = MagicMock()
        panel.prefill_input("hello", auto_submit=True)
        panel._core.prefill_input.assert_called_once_with("hello", auto_submit=True)

    def test_prefill_noop_when_core_is_none(self):
        panel = _make_panel()
        panel._core = None
        panel.prefill_input("text")  # must not raise

    def test_prefill_default_auto_submit_false(self):
        panel = _make_panel()
        panel._core = MagicMock()
        panel.prefill_input("text")
        panel._core.prefill_input.assert_called_once_with("text", auto_submit=False)


# ---------------------------------------------------------------------------
# __getattr__
# ---------------------------------------------------------------------------

class TestRikuganIdaPanelGetattr(unittest.TestCase):
    def test_getattr_delegates_to_core(self):
        panel = _make_panel()
        panel._core = MagicMock()
        panel._core.session = "current_session"
        result = panel.session
        self.assertEqual(result, "current_session")

    def test_getattr_raises_when_not_on_core(self):
        panel = _make_panel()
        panel._core = MagicMock(spec=[])
        with self.assertRaises(AttributeError):
            _ = panel.nonexistent_attr

    def test_getattr_raises_when_core_is_none(self):
        panel = _make_panel()
        panel._core = None
        with self.assertRaises(AttributeError):
            _ = panel.any_attr


# ---------------------------------------------------------------------------
# OnClose
# ---------------------------------------------------------------------------

class TestRikuganIdaPanelOnClose(unittest.TestCase):
    def test_on_close_calls_shutdown(self):
        panel = _make_panel()
        panel._core = MagicMock()
        panel._root = MagicMock()
        panel.OnClose(form=None)
        self.assertIsNone(panel._core)

    def test_on_close_clears_root(self):
        panel = _make_panel()
        panel._core = None
        mock_root = MagicMock()
        panel._root = mock_root
        panel.OnClose(form=None)
        mock_root.setParent.assert_called_once_with(None)
        self.assertIsNone(panel._root)

    def test_on_close_noop_root_when_already_none(self):
        panel = _make_panel()
        panel._core = None
        panel._root = None
        panel.OnClose(form=None)  # must not raise


if __name__ == "__main__":
    unittest.main()
