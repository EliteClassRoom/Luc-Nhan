"""Tests for pure-logic helpers in rikugan.ui.tool_widgets.

Isolates the testable business-logic functions from Qt widget code
by installing PySide6 stubs before importing the module.
"""

from __future__ import annotations

import json
import sys
import unittest

from tests.qt_stubs import ensure_pyside6_stubs

ensure_pyside6_stubs()

# Ensure the real module is loaded even if another test stubbed it.
sys.modules.pop("rikugan.ui.tool_widgets", None)

from rikugan.ui.tool_widgets import (
    _DEFAULT_TOOL_COLOR,
    _build_approval_header,
    _format_tool_group_label,
    _format_tool_summary,
    _strip_mcp_prefix,
    _tool_color,
    _truncate_preview,
)

# ---------------------------------------------------------------------------
# _strip_mcp_prefix
# ---------------------------------------------------------------------------


class TestStripMcpPrefix(unittest.TestCase):
    def test_plain_name_unchanged(self):
        self.assertEqual(_strip_mcp_prefix("decompile_function"), "decompile_function")

    def test_strips_mcp_prefix(self):
        self.assertEqual(_strip_mcp_prefix("mcp__myserver__decompile_function"), "decompile_function")

    def test_strips_only_first_segment(self):
        self.assertEqual(_strip_mcp_prefix("mcp__a__b__c"), "b__c")

    def test_mcp_without_double_underscore(self):
        # "mcp__" present but no trailing "__" — should return as-is after "mcp__"
        result = _strip_mcp_prefix("mcp__notrailing")
        self.assertEqual(result, "mcp__notrailing")

    def test_empty_string(self):
        self.assertEqual(_strip_mcp_prefix(""), "")


# ---------------------------------------------------------------------------
# _tool_color
# ---------------------------------------------------------------------------


class TestToolColor(unittest.TestCase):
    def test_known_analysis_tool(self):
        color = _tool_color("decompile_function")
        self.assertEqual(color, "#4ec9b0")  # teal

    def test_known_mutation_tool(self):
        color = _tool_color("rename_function")
        self.assertEqual(color, "#c586c0")  # magenta

    def test_unknown_tool_gets_default(self):
        color = _tool_color("zzz_unknown_tool")
        self.assertEqual(color, _DEFAULT_TOOL_COLOR)

    def test_mcp_prefixed_tool(self):
        color = _tool_color("mcp__bn__decompile_function")
        self.assertEqual(color, "#4ec9b0")  # still teal after stripping prefix


# ---------------------------------------------------------------------------
# _format_tool_group_label
# ---------------------------------------------------------------------------


class TestFormatToolGroupLabel(unittest.TestCase):
    def test_empty_list(self):
        self.assertEqual(_format_tool_group_label([]), "0 tools called")

    def test_single_generic_tool(self):
        self.assertEqual(_format_tool_group_label(["some_tool"]), "1 tool called")

    def test_multiple_generic_tools(self):
        self.assertEqual(_format_tool_group_label(["a", "b", "c"]), "3 tools called")

    def test_single_known_tool_singular(self):
        result = _format_tool_group_label(["decompile_function"])
        self.assertEqual(result, "Decompiled 1 function")

    def test_single_known_tool_plural(self):
        result = _format_tool_group_label(["decompile_function", "decompile_function"])
        self.assertEqual(result, "Decompiled 2 functions")

    def test_mixed_tools_no_label(self):
        result = _format_tool_group_label(["rename_function", "decompile_function"])
        self.assertEqual(result, "2 tools called")

    def test_mcp_prefixed_known(self):
        result = _format_tool_group_label(["mcp__bn__decompile_function"])
        self.assertEqual(result, "Decompiled 1 function")


# ---------------------------------------------------------------------------
# _format_tool_summary
# ---------------------------------------------------------------------------


class TestFormatToolSummary(unittest.TestCase):
    def test_decompile_with_address(self):
        args = json.dumps({"address": "0x1000"})
        result = _format_tool_summary("decompile_function", args)
        self.assertEqual(result, "0x1000")

    def test_rename_function(self):
        args = json.dumps({"old_name": "sub_1000", "new_name": "process_data"})
        result = _format_tool_summary("rename_function", args)
        self.assertEqual(result, "sub_1000 → process_data")

    def test_rename_variable_summary(self):
        args = json.dumps({"variable_name": "var_0", "new_name": "count", "function_name": "main"})
        result = _format_tool_summary("rename_variable", args)
        self.assertEqual(result, "main: var_0 → count")

    def test_set_comment_truncation(self):
        long_comment = "A" * 60
        args = json.dumps({"address": "0x1000", "comment": long_comment})
        result = _format_tool_summary("set_comment", args)
        self.assertIn("...", result)
        self.assertLessEqual(len(result), 120)

    def test_search_strings(self):
        args = json.dumps({"pattern": "hello"})
        result = _format_tool_summary("search_strings", args)
        self.assertEqual(result, '"hello"')

    def test_execute_python_first_line(self):
        code = "print('hello')\nprint('world')"
        args = json.dumps({"code": code})
        result = _format_tool_summary("execute_python", args)
        self.assertEqual(result, "print('hello')")

    def test_invalid_json_returns_empty(self):
        result = _format_tool_summary("decompile_function", "not json")
        self.assertEqual(result, "")

    def test_empty_args(self):
        result = _format_tool_summary("decompile_function", "")
        self.assertEqual(result, "")

    def test_summary_truncated_at_80(self):
        args = json.dumps({"address": "x" * 100})
        result = _format_tool_summary("decompile_function", args)
        self.assertLessEqual(len(result), 80)
        self.assertTrue(result.endswith("..."))

    def test_mcp_prefixed_tool(self):
        args = json.dumps({"address": "0x400"})
        result = _format_tool_summary("mcp__bn__decompile_function", args)
        self.assertEqual(result, "0x400")

    def test_phase_transition(self):
        args = json.dumps({"to_phase": "execute", "reason": "all targets identified"})
        result = _format_tool_summary("phase_transition", args)
        self.assertIn("execute", result)

    def test_generic_fallback_address(self):
        args = json.dumps({"address": "0xdeadbeef"})
        result = _format_tool_summary("unknown_tool_xyz", args)
        self.assertEqual(result, "0xdeadbeef")


# ---------------------------------------------------------------------------
# _truncate_preview
# ---------------------------------------------------------------------------


class TestTruncatePreview(unittest.TestCase):
    def test_short_text_unchanged(self):
        text = "line1\nline2\nline3"
        self.assertEqual(_truncate_preview(text, max_lines=3), text)

    def test_single_line_unchanged(self):
        self.assertEqual(_truncate_preview("hello", max_lines=3), "hello")

    def test_truncated_shows_count(self):
        text = "\n".join(f"line{i}" for i in range(10))
        result = _truncate_preview(text, max_lines=3)
        self.assertIn("… +7 lines", result)
        self.assertIn("line0", result)
        self.assertIn("line2", result)
        self.assertNotIn("line3", result)

    def test_exactly_at_limit_unchanged(self):
        text = "a\nb\nc"
        self.assertEqual(_truncate_preview(text, max_lines=3), text)

    def test_custom_max_lines(self):
        text = "a\nb\nc\nd\ne"
        result = _truncate_preview(text, max_lines=2)
        self.assertIn("… +3 lines", result)


# ---------------------------------------------------------------------------
# _build_approval_header
# ---------------------------------------------------------------------------


class TestBuildApprovalHeader(unittest.TestCase):
    """The approval-gate header must name the actual tool being approved.

    Regression: ``ToolApprovalWidget`` hard-coded ``"Approve
    execute_python?"`` even though the same widget is reused for every
    mutating tool when ``config.approve_mutations`` is set (rename,
    set_type, nop_microcode, create_struct, ...). A user approving a
    mutation saw a header that named an unrelated script tool, so they
    could not tell what they were authorising. ``_build_approval_header``
    is the pure helper that derives the correct label from the tool name.
    """

    def test_execute_python_keeps_expected_label(self):
        # The motivating case the header was originally written for —
        # keep it so the fix does not regress script approvals.
        self.assertEqual(_build_approval_header("execute_python"), "Approve execute_python?")

    def test_mutating_tool_named_in_header(self):
        # The bug: a mutation approval must name the mutating tool, not
        # fall back to "execute_python".
        self.assertEqual(_build_approval_header("rename_function"), "Approve rename_function?")
        self.assertEqual(_build_approval_header("nop_microcode"), "Approve nop_microcode?")

    def test_mcp_prefix_stripped(self):
        # MCP-bridged tools carry a ``mcp__<server>__`` prefix that is
        # noise in a user-facing label.
        self.assertEqual(
            _build_approval_header("mcp__myserver__custom_tool"),
            "Approve custom_tool?",
        )

    def test_generic_tool_named(self):
        self.assertEqual(_build_approval_header("search_strings"), "Approve search_strings?")


if __name__ == "__main__":
    unittest.main()
