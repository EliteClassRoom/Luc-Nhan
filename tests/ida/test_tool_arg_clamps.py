"""Tool arguments that are counts or limits must survive nonsense values.

Negative ``limit`` used to slice the tail off (``rows[:-1]``), a zero or
negative disassembly ``count`` returned an empty listing, and an unclamped
``count`` kept issuing IDA calls after the output was already truncated.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from lucnhan.ida.tools import database, disassembly


class TestImportLimit(unittest.TestCase):
    def _run_search(self, limit: int) -> str:
        def fake_enum(index, cb):
            for i, name in enumerate(("CreateFileW", "CreateProcessW", "CreateThread")):
                cb(0x1000 + i * 0x10, name, 0)

        with (
            patch.object(database.ida_nalt, "get_import_module_qty", return_value=1),
            patch.object(database.ida_nalt, "get_import_module_name", return_value="kernel32"),
            patch.object(database.ida_nalt, "enum_import_names", side_effect=fake_enum),
        ):
            return database.search_imports("Create", limit=limit)

    def test_negative_limit_falls_back_to_one_row_not_the_tail_slice(self):
        out = self._run_search(-1)
        self.assertIn("CreateFileW", out)
        self.assertNotIn("CreateProcessW", out)
        self.assertIn("2 more", out)

    def test_zero_limit_still_returns_one_row(self):
        self.assertIn("CreateFileW", self._run_search(0))


class TestDisassemblyCount(unittest.TestCase):
    def _run(self, count: int) -> list[str]:
        with (
            patch.object(disassembly.idc, "print_insn_mnem", return_value="nop"),
            patch.object(disassembly.idc, "print_operand", return_value=""),
            patch.object(disassembly.idc, "get_cmt", return_value=None),
            patch.object(disassembly.idc, "next_head", side_effect=lambda ea, _mx: ea + 1),
        ):
            return disassembly.read_disassembly("0x401000", count=count).splitlines()

    def test_zero_and_negative_counts_return_one_instruction(self):
        self.assertEqual(len(self._run(0)), 1)
        self.assertEqual(len(self._run(-4)), 1)

    def test_count_is_capped(self):
        self.assertEqual(len(self._run(10**6)), disassembly._MAX_DISASSEMBLY_INSNS)


class TestReadBytesSize(unittest.TestCase):
    def _calls(self, size: int) -> int:
        with patch.object(database.idc, "get_wide_byte", return_value=0x90) as getter:
            database.read_bytes("0x401000", size=size)
        return getter.call_count

    def test_zero_reads_one_byte(self):
        self.assertEqual(self._calls(0), 1)

    def test_oversize_is_capped(self):
        self.assertEqual(self._calls(10**6), 1024)


if __name__ == "__main__":
    unittest.main()
