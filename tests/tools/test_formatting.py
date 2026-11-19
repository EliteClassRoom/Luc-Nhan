"""Tests for the shared caller/callee formatters."""

from __future__ import annotations

import unittest

from lucnhan.tools.formatting import (
    _MAX_INLINE_NAMES,
    _MAX_LISTED_NAMES,
    format_callers_callees,
    format_function_summary,
)


class TestFormatFunctionSummary(unittest.TestCase):
    def test_short_lists_are_complete(self):
        text = format_function_summary("f", 0x10, 0x20, 16, 1, 3, ["a", "b"], ["c"])
        self.assertIn("Callers (2): a, b", text)
        self.assertIn("Callees (1): c", text)
        self.assertNotIn("more", text)

    def test_long_list_is_summarised(self):
        callers = [f"caller_{i}" for i in range(_MAX_INLINE_NAMES + 5)]
        text = format_function_summary("f", 0x10, 0x20, 16, 1, 3, callers, [])
        self.assertIn(f"Callers ({len(callers)})", text)
        self.assertIn("... and 5 more", text)
        self.assertNotIn(f"caller_{_MAX_INLINE_NAMES}", text)


class TestFormatCallersCallees(unittest.TestCase):
    def test_lists_are_sorted_and_indented(self):
        text = format_callers_callees("f", 0x10, ["b", "a"], ["d", "c"])
        self.assertIn("\nCallers (2):\n  a\n  b", text)
        self.assertIn("\nCallees (2):\n  c\n  d", text)

    def test_long_list_is_truncated_with_a_tail(self):
        callers = [f"c{i:04d}" for i in range(_MAX_LISTED_NAMES + 7)]
        text = format_callers_callees("f", 0x10, callers, [])
        self.assertIn(f"Callers ({len(callers)})", text)
        self.assertIn("  ... and 7 more", text)
        self.assertEqual(text.count("\n  c"), _MAX_LISTED_NAMES)

    def test_empty_sides_still_render_headings(self):
        text = format_callers_callees("f", 0x10, [], [])
        self.assertIn("Callers (0):", text)
        self.assertIn("Callees (0):", text)
        self.assertNotIn("more", text)
