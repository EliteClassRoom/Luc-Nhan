"""Tests for the IDA function index (Phase 5).

These tests do not require IDA — they exercise the index against
synthetic ``FunctionEntry`` data so the cache logic can be unit-tested
in CI without a real IDA binary.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.ida.tools import function_index
from rikugan.ida.tools.function_index import (
    FunctionEntry,
    find_containing_function,
    function_count,
    get_function_index,
    invalidate_function_index,
    list_function_entries,
    search_function_names,
)


def _seed_index(entries: list[FunctionEntry]) -> None:
    """Forcefully install *entries* into the module-level index.

    Used by tests to bypass IDA's ``idautils.Functions()`` walk.
    """
    by_start = {e.start_ea: e for e in entries}
    name_lower = [(e.name.lower(), e) for e in entries]
    ranges = sorted(
        ((e.start_ea, e.end_ea, e) for e in entries),
        key=lambda r: r[0],
    )
    function_index._INDEX = function_index._FunctionIndex(  # type: ignore[attr-defined]
        entries=entries,
        by_start=by_start,
        name_lower=name_lower,
        ranges=ranges,
    )


class TestFunctionIndexSearch(unittest.TestCase):
    def setUp(self):
        invalidate_function_index()
        _seed_index(
            [
                FunctionEntry(0x1000, 0x1010, "sub_1000", False, 16),
                FunctionEntry(0x2000, 0x2030, "process_data", False, 48),
                FunctionEntry(0x3000, 0x3020, "DECRYPT_BUFFER", False, 32),
                FunctionEntry(0x4000, 0x4010, "printf", True, 16),  # import
            ]
        )

    def test_substring_search_case_insensitive(self):
        results = search_function_names("DATA", limit=10)
        names = {e.name for e in results}
        self.assertIn("process_data", names)

    def test_substring_search_matches_uppercase(self):
        """A lowercase query should match names stored in uppercase."""
        results = search_function_names("decrypt", limit=10)
        names = {e.name for e in results}
        self.assertIn("DECRYPT_BUFFER", names)

    def test_search_limit(self):
        results = search_function_names("sub", limit=1)
        self.assertEqual(len(results), 1)

    def test_search_no_match(self):
        results = search_function_names("zzz_no_match_zzz", limit=10)
        self.assertEqual(results, [])

    def test_search_empty_query(self):
        results = search_function_names("", limit=10)
        self.assertEqual(results, [])


class TestFunctionIndexFindContaining(unittest.TestCase):
    def setUp(self):
        invalidate_function_index()
        _seed_index(
            [
                FunctionEntry(0x1000, 0x1010, "sub_1000", False, 16),
                FunctionEntry(0x2000, 0x2050, "process", False, 80),
            ]
        )

    def test_find_within_function(self):
        entry = find_containing_function(0x2025)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.name, "process")

    def test_find_at_start(self):
        entry = find_containing_function(0x2000)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.name, "process")

    def test_find_outside_any_function(self):
        entry = find_containing_function(0x5000)
        self.assertIsNone(entry)

    def test_find_below_all_ranges(self):
        entry = find_containing_function(0x500)
        self.assertIsNone(entry)


class TestFunctionIndexList(unittest.TestCase):
    def setUp(self):
        invalidate_function_index()
        _seed_index(
            [FunctionEntry(0x1000 + i * 0x10, 0x1008 + i * 0x10, f"f{i}", False, 8) for i in range(20)]
        )

    def test_list_paginated(self):
        entries, total = list_function_entries(5, 10)
        self.assertEqual(total, 20)
        self.assertEqual(len(entries), 10)
        self.assertEqual(entries[0].start_ea, 0x1050)

    def test_list_offset_only(self):
        entries, total = list_function_entries(15, 0)
        self.assertEqual(total, 20)
        self.assertEqual(len(entries), 5)
        self.assertEqual(entries[0].start_ea, 0x10F0)

    def test_function_count(self):
        self.assertEqual(function_count(), 20)


class TestFunctionIndexInvalidate(unittest.TestCase):
    def test_invalidate_clears_cache(self):
        _seed_index([FunctionEntry(0x1000, 0x1010, "x", False, 16)])
        self.assertEqual(function_count(), 1)
        invalidate_function_index()
        # After invalidation, the next access rebuilds — and since IDA is
        # mocked out, the rebuilt index is empty. The contract is just
        # that the cache is no longer the same object.
        first = function_index._INDEX  # type: ignore[attr-defined]
        invalidate_function_index()
        get_function_index()  # triggers rebuild
        second = function_index._INDEX  # type: ignore[attr-defined]
        self.assertIsNot(first, second)


if __name__ == "__main__":
    unittest.main()
