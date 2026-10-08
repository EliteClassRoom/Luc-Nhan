"""Tests for pure-logic helpers in lucnhan.ui.tool_widgets.

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
sys.modules.pop("lucnhan.ui.tool_widgets", None)

from lucnhan.ui.tool_widgets import (
    _build_approval_header,
    _format_tool_group_label,
    _format_tool_summary,
    _row_target_text,
    _status_style,
    _strip_mcp_prefix,
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
# _status_style
# ---------------------------------------------------------------------------


class TestStatusStyle(unittest.TestCase):
    """Tool rows carry state as a glyph AND a word, never colour alone
    (GUI contract G9). The QSS only chooses the tone; the non-colour
    carrier lives in the widget's icon/label text."""

    def setUp(self):
        from lucnhan.ui.styles import get_tool_colors

        self.colors = get_tool_colors()

    def test_each_state_maps_to_its_own_role(self):
        success = _status_style(self.colors, "success")
        error = _status_style(self.colors, "error")
        running = _status_style(self.colors, "running")
        self.assertIn(self.colors["status_success"], success)
        self.assertIn(self.colors["status_error"], error)
        self.assertIn(self.colors["status_spinner"], running)

    def test_discarded_is_neutral_not_error(self):
        discarded = _status_style(self.colors, "discarded")
        self.assertIn(self.colors["preview"], discarded)
        self.assertNotIn(self.colors["status_error"], discarded)


# ---------------------------------------------------------------------------
# _row_target_text
# ---------------------------------------------------------------------------


class TestRowTargetText(unittest.TestCase):
    """The target column is one line, elided, and keeps the real value
    in the tooltip (GUI contract G4)."""

    def test_multiline_summary_becomes_one_line(self):
        self.assertEqual(_row_target_text("line one\nline two"), "line one line two")

    def test_long_summary_is_elided(self):
        text = _row_target_text("a" * 500)
        self.assertLessEqual(len(text), 120)
        self.assertTrue(text.endswith("…"))


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


# ---------------------------------------------------------------------------
# Tool row regressions (review round 1)
# ---------------------------------------------------------------------------


class TestToolRowDetailRendering(unittest.TestCase):
    """Args/result must be rendered the FIRST time a row is expanded.

    Regression: ``_toggle`` set ``_details_built`` *after* the builder ran,
    and the builder's ``_render_detail_text`` call no-ops while that flag
    is unset — so expanding a call whose args/result had already arrived
    showed an empty inset.
    """

    def _widget(self):
        from lucnhan.ui.tool_widgets import ToolCallWidget

        return ToolCallWidget("decompile_function", "tc1")

    def test_expand_renders_args_that_arrived_earlier(self):
        widget = self._widget()
        widget.set_arguments('{"address": "0x1400188b0"}')
        self.assertFalse(hasattr(widget, "_args_label"))
        widget._toggle()
        self.assertIn("0x1400188b0", widget._args_label.text())

    def test_expand_renders_result_that_arrived_earlier(self):
        widget = self._widget()
        widget.set_arguments('{"address": "0x1400188b0"}')
        widget.set_result("some output", is_error=False)
        widget._toggle()
        self.assertIn("some output", widget._result_label.text())

    def test_result_arriving_while_expanded_renders(self):
        widget = self._widget()
        widget.set_arguments('{"address": "0x1400188b0"}')
        widget._toggle()
        widget.set_result("late output", is_error=True)
        self.assertIn("late output", widget._result_label.text())


class TestToolRowTooltip(unittest.TestCase):
    """G4: the row tooltip carries the FULL argument, not the 80-char
    summary slice."""

    def test_long_argument_kept_in_full(self):
        from lucnhan.ui.tool_widgets import ToolCallWidget

        args = json.dumps({"code": "print(" + "a" * 300 + ")"})
        widget = ToolCallWidget("execute_python", "tc1")
        widget.set_arguments(args)
        self.assertEqual(widget._summary_label.toolTip(), args)
        self.assertGreater(len(widget._summary_label.toolTip()), 200)

    def test_summary_column_stays_elided(self):
        from lucnhan.ui.tool_widgets import ToolCallWidget

        widget = ToolCallWidget("execute_python", "tc1")
        widget.set_arguments(json.dumps({"code": "x" * 400}))
        self.assertLessEqual(len(widget._summary_label.text()), 120)


class TestToolBatchEntries(unittest.TestCase):
    """Each expanded batch entry must name its own target."""

    def test_entries_render_per_call_args(self):
        from lucnhan.ui.tool_widgets import ToolBatchWidget

        batch = ToolBatchWidget("list_strings")
        batch.add_call("b1", '{"pattern": "alpha"}')
        batch.add_call("b2", '{"pattern": "beta"}')
        batch._toggle()
        texts = [e.text() for e in batch._entry_labels]
        self.assertEqual(len(texts), 2)
        self.assertTrue(texts[0].endswith("alpha"), texts)
        self.assertTrue(texts[1].endswith("beta"), texts)

    def test_add_call_while_expanded_appends_entry(self):
        from lucnhan.ui.tool_widgets import ToolBatchWidget

        batch = ToolBatchWidget("list_strings")
        batch.add_call("b1", '{"pattern": "alpha"}')
        batch._toggle()
        batch.add_call("b2", '{"pattern": "beta"}')
        self.assertEqual(len(batch._entry_labels), 2)

    def test_set_args_for_call_updates_collapsed_batch(self):
        from lucnhan.ui.tool_widgets import ToolBatchWidget

        batch = ToolBatchWidget("list_strings")
        batch.add_call("b1", "")
        batch.add_call("b2", "")
        batch.set_args_for_call("b2", '{"pattern": "beta"}')
        batch._toggle()
        self.assertTrue(batch._entry_labels[1].text().endswith("beta"), batch._entry_labels[1].text())


class TestApprovalHeaderText(unittest.TestCase):
    """G4 pins the literal ``Approval required`` in the approval header."""

    def test_header_emits_approval_required(self):
        from lucnhan.ui.tool_widgets import ToolApprovalWidget

        widget = ToolApprovalWidget("a1", "execute_python", '{"code": "print(1)"}', "desc")
        self.assertEqual(widget._required_label.text(), "Approval required")
        self.assertIn("execute_python", widget._header.text())

    def test_state_chip_reads_paused(self):
        from lucnhan.ui.tool_widgets import ToolApprovalWidget

        widget = ToolApprovalWidget("a1", "execute_python", '{"code": "print(1)"}', "desc")
        self.assertEqual(widget._state_label.text(), "⏸ Paused")


if __name__ == "__main__":
    unittest.main()
