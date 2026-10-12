"""Tests for lucnhan.ui.context_bar — pure logic in set_tokens, set_function, _function_name_at."""

from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch

from tests.qt_stubs import ensure_pyside6_stubs

ensure_pyside6_stubs()

# Defensive: drop any ``_StubModule`` entries a sibling test file
# (e.g. ``tests/tools/test_panel_core.py``) left in ``sys.modules``
# before we import the real lucnhan modules.  Without this purge
# the ``ContextBar`` symbol here would be a ``MagicMock`` from a
# previous test's stub, and ``object.__new__(ContextBar)`` would
# raise ``TypeError``.
from tests import purge_lucnhan_stubs

purge_lucnhan_stubs()

# ``_StubModule`` instances created by sibling test files attach
# MagicMocks to ``sys.modules['lucnhan.ui.context_bar']``.  Force
# a re-import of the real module even if a previous import bound a
# stub to the same name.  We then re-bind the symbols the tests
# use from the freshly-imported module so a later
# ``patch("lucnhan.ui.context_bar.is_ida", ...)`` call targets the
# same module instance our functions were bound to.  Without this
# re-binding, ``patch`` would re-import context_bar (because the
# stub purge removed the real module too) and our local
# ``_function_name_at`` would still reference the *previous*
# module instance with its original ``is_ida`` — so the patch would
# silently no-op and the function would see the un-patched
# ``is_ida()`` (which may return True if a previous test left
# ``lucnhan.core.host._HOST = HOST_IDA``).
for _name in (
    "lucnhan.ui.context_bar",
    "lucnhan.ui.styles",
    "lucnhan.ui.theme.manager",
    "lucnhan.core.host",
):
    sys.modules.pop(_name, None)

import lucnhan.ui.context_bar as _context_bar_mod

ContextBar = _context_bar_mod.ContextBar
_function_name_at = _context_bar_mod._function_name_at

# Defensive: ``tests/core/test_host.py`` mutates
# ``lucnhan.core.host._HOST`` to ``HOST_IDA`` (or ``HOST_STANDALONE``)
# via ``setup_method`` and restores it in ``teardown_method``.  If a
# teardown is skipped (e.g. an assertion error before the teardown
# line, or a test that shares a class-scoped fixture), ``_HOST`` can
# leak as ``HOST_IDA`` and bias subsequent tests that call
# ``is_ida()``.  Force the standalone default here so the
# ``_function_name_at`` test sees the expected host kind.
import lucnhan.core.host as _host_mod

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
    bar._timer = MagicMock()
    return bar


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
# set_address
# ---------------------------------------------------------------------------


class TestSetAddress(unittest.TestCase):
    def test_set_address_updates_label(self):
        bar = _make_bar()
        bar.set_address("0x1000")
        bar._address_label[1].setText.assert_called_once_with("0x1000")


# ---------------------------------------------------------------------------
# _function_name_at — standalone host
# ---------------------------------------------------------------------------


class TestFunctionNameAt(unittest.TestCase):
    def test_returns_none_in_standalone_mode(self):
        with patch("lucnhan.ui.context_bar.is_ida", return_value=False):
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
