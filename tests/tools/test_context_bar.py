"""Tests for rikugan.ui.context_bar — pure logic in set_tokens, set_function, _function_name_at."""

from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch

from tests.qt_stubs import ensure_pyside6_stubs

ensure_pyside6_stubs()

# Defensive: drop any ``_StubModule`` entries a sibling test file
# (e.g. ``tests/tools/test_panel_core.py``) left in ``sys.modules``
# before we import the real rikugan modules.  Without this purge
# the ``ContextBar`` symbol here would be a ``MagicMock`` from a
# previous test's stub, and ``object.__new__(ContextBar)`` would
# raise ``TypeError``.
from tests import purge_rikugan_stubs

purge_rikugan_stubs()

# ``_StubModule`` instances created by sibling test files attach
# MagicMocks to ``sys.modules['rikugan.ui.context_bar']``.  Force
# a re-import of the real module even if a previous import bound a
# stub to the same name.  We then re-bind the symbols the tests
# use from the freshly-imported module so a later
# ``patch("rikugan.ui.context_bar.is_ida", ...)`` call targets the
# same module instance our functions were bound to.  Without this
# re-binding, ``patch`` would re-import context_bar (because the
# stub purge removed the real module too) and our local
# ``_function_name_at`` would still reference the *previous*
# module instance with its original ``is_ida`` — so the patch would
# silently no-op and the function would see the un-patched
# ``is_ida()`` (which may return True if a previous test left
# ``rikugan.core.host._HOST = HOST_IDA``).
for _name in (
    "rikugan.ui.context_bar",
    "rikugan.ui.styles",
    "rikugan.ui.theme.manager",
    "rikugan.core.host",
):
    sys.modules.pop(_name, None)

import rikugan.ui.context_bar as _context_bar_mod

ContextBar = _context_bar_mod.ContextBar
_function_name_at = _context_bar_mod._function_name_at

# Defensive: ``tests/core/test_host.py`` mutates
# ``rikugan.core.host._HOST`` to ``HOST_IDA`` (or ``HOST_STANDALONE``)
# via ``setup_method`` and restores it in ``teardown_method``.  If a
# teardown is skipped (e.g. an assertion error before the teardown
# line, or a test that shares a class-scoped fixture), ``_HOST`` can
# leak as ``HOST_IDA`` and bias subsequent tests that call
# ``is_ida()``.  Force the standalone default here so the
# ``_function_name_at`` test sees the expected host kind.
import rikugan.core.host as _host_mod

_host_mod._HOST = _host_mod.HOST_STANDALONE

# ---------------------------------------------------------------------------
# Helper: create a ContextBar without calling __init__
# ---------------------------------------------------------------------------

def _make_bar() -> ContextBar:
    bar = object.__new__(ContextBar)
    bar._stopped = False
    # Mock label pairs
    bar._address_label = (MagicMock(), MagicMock())
    bar._function_label = (MagicMock(), MagicMock())
    bar._model_label = (MagicMock(), MagicMock())
    bar._tokens_label = (MagicMock(), MagicMock())
    bar._timer = MagicMock()
    return bar


# ---------------------------------------------------------------------------
# set_tokens
# ---------------------------------------------------------------------------

class TestSetTokens(unittest.TestCase):
    def test_small_count_shown_as_int(self):
        bar = _make_bar()
        bar.set_tokens(500)
        bar._tokens_label[1].setText.assert_called_once_with("500")

    def test_large_count_shown_in_k(self):
        bar = _make_bar()
        bar.set_tokens(2500)
        bar._tokens_label[1].setText.assert_called_once_with("2.5k")

    def test_exactly_1000_shown_in_k(self):
        bar = _make_bar()
        bar.set_tokens(1000)
        bar._tokens_label[1].setText.assert_called_once_with("1.0k")

    def test_context_window_shows_percentage(self):
        bar = _make_bar()
        bar.set_tokens(500, context_window=1000)
        call_arg = bar._tokens_label[1].setText.call_args[0][0]
        self.assertIn("500", call_arg)
        self.assertIn("50%", call_arg)

    def test_context_window_percentage_capped_at_100(self):
        bar = _make_bar()
        bar.set_tokens(2000, context_window=1000)
        call_arg = bar._tokens_label[1].setText.call_args[0][0]
        self.assertIn("100%", call_arg)

    def test_no_percentage_when_context_window_zero(self):
        bar = _make_bar()
        bar.set_tokens(100, context_window=0)
        call_arg = bar._tokens_label[1].setText.call_args[0][0]
        self.assertNotIn("%", call_arg)


# ---------------------------------------------------------------------------
# set_function
# ---------------------------------------------------------------------------

class TestSetFunction(unittest.TestCase):
    def test_short_name_passed_through(self):
        bar = _make_bar()
        bar.set_function("my_func")
        bar._function_label[1].setText.assert_called_once_with("my_func")

    def test_long_name_truncated(self):
        bar = _make_bar()
        long_name = "a" * 35
        bar.set_function(long_name)
        call_arg = bar._function_label[1].setText.call_args[0][0]
        self.assertTrue(call_arg.endswith("..."))
        self.assertLessEqual(len(call_arg), 30)

    def test_exactly_29_chars_not_truncated(self):
        bar = _make_bar()
        name = "b" * 29
        bar.set_function(name)
        bar._function_label[1].setText.assert_called_once_with(name)

    def test_exactly_30_chars_truncated(self):
        bar = _make_bar()
        name = "c" * 30
        bar.set_function(name)
        call_arg = bar._function_label[1].setText.call_args[0][0]
        self.assertTrue(call_arg.endswith("..."))


# ---------------------------------------------------------------------------
# set_address / set_model
# ---------------------------------------------------------------------------

class TestSetAddress(unittest.TestCase):
    def test_set_address_updates_label(self):
        bar = _make_bar()
        bar.set_address("0x1000")
        bar._address_label[1].setText.assert_called_once_with("0x1000")


class TestSetModel(unittest.TestCase):
    def test_set_model_updates_label(self):
        bar = _make_bar()
        bar.set_model("claude-opus-4")
        bar._model_label[1].setText.assert_called_once_with("claude-opus-4")


# ---------------------------------------------------------------------------
# _function_name_at — standalone host
# ---------------------------------------------------------------------------

class TestFunctionNameAt(unittest.TestCase):
    def test_returns_none_in_standalone_mode(self):
        with patch("rikugan.ui.context_bar.is_ida", return_value=False):
            result = _function_name_at(0x1000)
            self.assertIsNone(result)


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------

class TestContextBarStop(unittest.TestCase):
    def test_stop_sets_stopped_flag(self):
        bar = _make_bar()
        bar.stop()
        self.assertTrue(bar._stopped)

    def test_stop_calls_timer_stop(self):
        bar = _make_bar()
        bar.stop()
        bar._timer.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
