"""Tests for microcode_format pure-Python helpers."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from lucnhan.core.errors import ToolError
from lucnhan.ida.tools.microcode_format import (
    _MATURITY_LEVELS,
    _MATURITY_NAMES,
    maturity_label,
    parse_maturity,
    require_hexrays,
)


class TestMaturityNames(unittest.TestCase):
    def test_all_levels_defined(self):
        assert len(_MATURITY_NAMES) == 8
        for i in range(8):
            assert i in _MATURITY_NAMES

    def test_levels_reverse_maps_names(self):
        for name, level in _MATURITY_LEVELS.items():
            assert _MATURITY_NAMES[level] == name


class TestParseMaturity(unittest.TestCase):
    def test_parse_by_name(self):
        assert parse_maturity("MMAT_GENERATED") == 0
        assert parse_maturity("MMAT_LOCOPT") == 2
        assert parse_maturity("MMAT_LVARS") == 7

    def test_parse_case_insensitive(self):
        assert parse_maturity("mmat_calls") == 3
        assert parse_maturity("Mmat_Glbopt1") == 4

    def test_parse_by_number_string(self):
        assert parse_maturity("0") == 0
        assert parse_maturity("7") == 7
        assert parse_maturity("3") == 3

    def test_parse_with_whitespace(self):
        assert parse_maturity("  MMAT_PREOPTIMIZED  ") == 1

    def test_parse_invalid_name_raises(self):
        with self.assertRaises(ToolError):
            parse_maturity("NOT_A_LEVEL")

    def test_parse_invalid_number_raises(self):
        with self.assertRaises(ToolError):
            parse_maturity("99")

    def test_parse_negative_number_raises(self):
        with self.assertRaises(ToolError):
            parse_maturity("-1")


class TestMaturityLabel(unittest.TestCase):
    def test_known_levels(self):
        assert maturity_label(0) == "MMAT_GENERATED"
        assert maturity_label(7) == "MMAT_LVARS"

    def test_unknown_level_fallback(self):
        label = maturity_label(99)
        assert "99" in label


class TestRequireHexrays(unittest.TestCase):
    def test_raises_when_no_hexrays(self):
        import lucnhan.ida.tools.microcode_format as mod
        orig = mod._HAS_HEXRAYS
        try:
            mod._HAS_HEXRAYS = False
            with self.assertRaises(ToolError) as ctx:
                require_hexrays()
            assert "decompiler" in str(ctx.exception).lower()
        finally:
            mod._HAS_HEXRAYS = orig


if __name__ == "__main__":
    unittest.main()
