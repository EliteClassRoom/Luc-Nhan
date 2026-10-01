"""Regression tests for ``parse_addr`` address-or-name resolution.

The model routinely passes a function name where a tool documents an
address (e.g. ``get_function_info("init_config_and_beacon")``). That used
to raise ``ValueError: invalid literal for int() with base 0`` from
``int(value, 0)`` and fail the whole tool call, and a bare decimal
address string ("4096") was rejected by the same base-0 parse.

The symbol lookup goes through ``sys.modules["ida_name"]`` at call time
(resolved lazily by ``core.host.resolve_symbol``), so the stub is fetched
in ``setUp`` — a sibling test module's ``install_ida_mocks()`` replaces
that entry during a full-suite run.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.tools.base import parse_addr

_SYMBOL = "init_config_and_beacon"
_SYMBOL_EA = 0x140001000


class TestParseAddr(unittest.TestCase):
    def setUp(self) -> None:
        ida_name = sys.modules["ida_name"]
        self._get_name_ea = ida_name.get_name_ea
        ida_name.get_name_ea.side_effect = lambda bad, name: (
            _SYMBOL_EA if name == _SYMBOL else sys.modules["idc"].BADADDR
        )

    def tearDown(self) -> None:
        ida_name = sys.modules["ida_name"]
        ida_name.get_name_ea = self._get_name_ea

    def test_int_passthrough(self):
        self.assertEqual(parse_addr(0x401000), 0x401000)

    def test_hex_string(self):
        self.assertEqual(parse_addr("0x401000"), 0x401000)

    def test_decimal_string_has_no_base_prefix(self):
        self.assertEqual(parse_addr("4096"), 4096)

    def test_symbol_name_resolves_to_address(self):
        self.assertEqual(parse_addr(_SYMBOL), _SYMBOL_EA)

    def test_unknown_name_raises_actionable_error(self):
        with self.assertRaises(ValueError) as ctx:
            parse_addr("no_such_symbol")
        self.assertIn("no_such_symbol", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
