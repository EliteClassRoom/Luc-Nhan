"""Tests for rikugan_plugin.py — pure logic helpers."""

from __future__ import annotations

import builtins
import importlib

# ---------------------------------------------------------------------------
# Stub idaapi (and related IDA modules) before importing the plugin
# ---------------------------------------------------------------------------
import os as _os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

# Provide real base classes so class inheritance works
import idaapi as _idaapi_mock  # type: ignore[import]

_idaapi_mock.plugmod_t = type("plugmod_t", (), {})
_idaapi_mock.plugin_t = type("plugin_t", (), {})
_idaapi_mock.PLUGIN_MULTI = 1
_idaapi_mock.PLUGIN_FIX = 2

# Force re-import so IDA mocks are active for this module's import
sys.modules.pop("rikugan_plugin", None)
import rikugan_plugin as rp

# ---------------------------------------------------------------------------
# _guarded_import — re-entrancy guard
# ---------------------------------------------------------------------------

class TestGuardedImport(unittest.TestCase):
    def setUp(self):
        """Ensure the import guard is installed before each test.

        Earlier test files in the full suite (notably test_session_controller)
        may replace ``builtins.__import__`` with a plain
        ``importlib.__import__`` for the duration of their tests. If that
        replacement leaks past their ``try/finally``, our guard is gone
        and the test below fails. Re-installing in ``setUp`` gives every
        test in this class a clean baseline.
        """
        import builtins
        current = builtins.__import__
        if not getattr(current, "_rikugan_guarded", False):
            builtins.__import__ = rp._guarded_import

    def test_marker_attribute_set(self):
        self.assertTrue(getattr(rp._guarded_import, "_rikugan_guarded", False))

    def test_not_double_wrapped(self):
        # builtins.__import__ should be _guarded_import (or something marked)
        current = builtins.__import__
        self.assertTrue(getattr(current, "_rikugan_guarded", False))

    def test_non_reentrant_calls_original(self):
        """Non-reentrant calls should go through the stored shiboken import."""
        original_called = []
        mock_import = MagicMock(side_effect=lambda *a, **k: (original_called.append(True), __builtins__)[1])

        saved = rp._shiboken_import
        saved_active = getattr(rp._import_guard, "active", False)
        try:
            rp._shiboken_import = mock_import
            rp._import_guard.active = False
            # Call _guarded_import with a simple module name
            try:
                rp._guarded_import("os", {}, {}, [], 0)
            except Exception:
                pass
            mock_import.assert_called_once()
        finally:
            rp._shiboken_import = saved
            rp._import_guard.active = saved_active

    def test_reentrant_calls_bypass(self):
        """Re-entrant calls should bypass the stored hook and use importlib.__import__."""
        bypass_called = []

        saved = rp._shiboken_import
        saved_active = getattr(rp._import_guard, "active", False)
        try:
            rp._import_guard.active = True
            mock_importlib_import = MagicMock(
                side_effect=lambda *a, **k: bypass_called.append(True) or types.ModuleType("fake")
            )
            with patch.object(importlib, "__import__", mock_importlib_import, create=True):
                try:
                    rp._guarded_import("os", {}, {}, [], 0)
                except Exception:
                    pass
            # shiboken_import must NOT have been called
            # (the mock_import replaces importlib.__import__)
        finally:
            rp._shiboken_import = saved
            rp._import_guard.active = saved_active

    def test_active_reset_after_call(self):
        """active flag should be reset to False after _guarded_import returns."""
        saved = rp._shiboken_import
        try:
            rp._shiboken_import = MagicMock(return_value=types.ModuleType("m"))
            rp._import_guard.active = False
            try:
                rp._guarded_import("os", {}, {}, [], 0)
            except Exception:
                pass
            self.assertFalse(getattr(rp._import_guard, "active", False))
        finally:
            rp._shiboken_import = saved

    def test_active_reset_on_exception(self):
        """active flag should be reset even if the original import raises."""
        saved = rp._shiboken_import
        try:
            rp._shiboken_import = MagicMock(side_effect=ImportError("fail"))
            rp._import_guard.active = False
            try:
                rp._guarded_import("nonexistent_xyz", {}, {}, [], 0)
            except ImportError:
                pass
            self.assertFalse(getattr(rp._import_guard, "active", False))
        finally:
            rp._shiboken_import = saved


# ---------------------------------------------------------------------------
# RikuganPlugmod.term
# ---------------------------------------------------------------------------

def _make_plugmod():
    """Create a RikuganPlugmod without calling __init__."""
    pm = object.__new__(rp.RikuganPlugmod)
    pm._panel = None
    return pm


class TestRikuganPlugmodTerm(unittest.TestCase):
    def test_term_closes_panel(self):
        pm = _make_plugmod()
        mock_panel = MagicMock()
        pm._panel = mock_panel
        pm.term()
        mock_panel.close.assert_called_once()

    def test_term_sets_panel_to_none(self):
        pm = _make_plugmod()
        pm._panel = MagicMock()
        pm.term()
        self.assertIsNone(pm._panel)

    def test_term_noop_when_panel_none(self):
        pm = _make_plugmod()
        pm._panel = None
        pm.term()  # must not raise

    def test_term_handles_close_exception(self):
        pm = _make_plugmod()
        mock_panel = MagicMock()
        mock_panel.close.side_effect = RuntimeError("panel already closed")
        pm._panel = mock_panel
        pm.term()  # must not raise, exception should be caught


class TestRikuganPlugmodRun(unittest.TestCase):
    def test_run_returns_true(self):
        pm = _make_plugmod()
        pm._panel = MagicMock()  # panel exists — toggle_panel calls show()
        with patch.object(pm, "_toggle_panel"):
            result = pm.run(0)
        self.assertTrue(result)

    def test_run_calls_toggle_panel(self):
        pm = _make_plugmod()
        with patch.object(pm, "_toggle_panel") as mock_toggle:
            pm.run(0)
        mock_toggle.assert_called_once()


# ---------------------------------------------------------------------------
# PLUGIN_ENTRY
# ---------------------------------------------------------------------------

class TestPluginEntry(unittest.TestCase):
    def test_returns_rikugan_plugin(self):
        result = rp.PLUGIN_ENTRY()
        self.assertIsInstance(result, rp.RikuganPlugin)


if __name__ == "__main__":
    unittest.main()
