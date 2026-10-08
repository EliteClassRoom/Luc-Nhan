"""Tests for ``lucnhan.core.host.resolve_symbol``.

Covers the unknown-name and non-IDA-host paths, which decide whether a bad
tool argument raises an actionable ``ValueError`` or gets silently mapped to
some address.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

import lucnhan.core.host as host_mod
from lucnhan.core.host import HOST_STANDALONE, resolve_symbol


class TestResolveSymbol(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_host = host_mod._HOST

    def tearDown(self) -> None:
        host_mod._HOST = self._orig_host

    def test_unknown_name_returns_none_under_ida_host(self) -> None:
        host_mod._HOST = host_mod.HOST_IDA
        ida_name = sys.modules["ida_name"]
        idc = sys.modules["idc"]
        original = ida_name.get_name_ea
        ida_name.get_name_ea.side_effect = lambda bad, name: idc.BADADDR
        try:
            self.assertIsNone(resolve_symbol("no_such_symbol"))
        finally:
            ida_name.get_name_ea = original

    def test_known_name_returns_address_under_ida_host(self) -> None:
        host_mod._HOST = host_mod.HOST_IDA
        ida_name = sys.modules["ida_name"]
        idc = sys.modules["idc"]
        original = ida_name.get_name_ea
        ida_name.get_name_ea.side_effect = lambda bad, name: 0x140001000 if name == "main" else idc.BADADDR
        try:
            self.assertEqual(resolve_symbol("main"), 0x140001000)
        finally:
            ida_name.get_name_ea = original

    def test_non_ida_host_returns_none(self) -> None:
        host_mod._HOST = HOST_STANDALONE
        self.assertIsNone(resolve_symbol("main"))


if __name__ == "__main__":
    unittest.main()
