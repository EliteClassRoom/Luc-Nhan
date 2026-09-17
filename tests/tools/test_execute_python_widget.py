"""Tests for ExecutePythonWidget (always-visible, no collapse/expand)."""

from __future__ import annotations

import json
import sys
import unittest

from tests.qt_stubs import ensure_pyside6_stubs

ensure_pyside6_stubs()

# Ensure the real module is loaded even if another test stubbed it.
sys.modules.pop("rikugan.ui.tool_widgets", None)

from rikugan.ui.tool_widgets import ExecutePythonWidget


class TestExecutePythonWidgetInit(unittest.TestCase):
    def test_init_idle_no_buttons(self):
        w = ExecutePythonWidget("tc1")
        # No code set yet.
        self.assertEqual(w._code, "")
        # Buttons should not be shown until show_approval_buttons().
        self.assertFalse(w._buttons_visible)


class TestSetArguments(unittest.TestCase):
    def test_set_arguments_extracts_code_from_json(self):
        w = ExecutePythonWidget("tc1")
        w.set_arguments(json.dumps({"code": "print(1)\nprint(2)\n"}))
        self.assertEqual(w._code, "print(1)\nprint(2)\n")

    def test_set_arguments_extracts_script_field(self):
        w = ExecutePythonWidget("tc1")
        w.set_arguments(json.dumps({"script": "x = 1"}))
        self.assertEqual(w._code, "x = 1")

    def test_set_arguments_fallback_raw_on_bad_json(self):
        w = ExecutePythonWidget("tc1")
        w.set_arguments("not valid json")
        self.assertEqual(w._code, "not valid json")


class TestDocsGateStatus(unittest.TestCase):
    def test_running_sets_status_text(self):
        w = ExecutePythonWidget("tc1")
        w.set_docs_gate_status("running", reasons=("2 IDA modules",))
        self.assertIn("Reviewing", w._status_text)
        self.assertIn("2 IDA modules", w._status_text)
        self.assertTrue(w._status_visible)

    def test_approved_sets_status_text(self):
        w = ExecutePythonWidget("tc1")
        w.set_docs_gate_status("approved")
        self.assertIn("Docs review passed", w._status_text)
        self.assertTrue(w._status_visible)

    def test_blocked_hides_buttons(self):
        w = ExecutePythonWidget("tc1")
        w.show_approval_buttons()
        self.assertTrue(w._buttons_visible)
        w.set_docs_gate_status("blocked", summary="bad API")
        self.assertFalse(w._buttons_visible)
        self.assertIn("Docs review blocked", w._status_text)

    def test_blocked_status_detail_visible_by_default(self):
        """A blocked review shows the full reviewer summary immediately —
        there is no collapse toggle to click open."""
        w = ExecutePythonWidget("tc1")
        w.set_docs_gate_status("blocked", summary="ida_bytes.patch_qword is not a real API" * 5)
        self.assertTrue(w._status_visible)
        self.assertTrue(w._status_detail.isVisible())

    def test_blocked_result_does_not_dup(self):
        """When the docs gate blocks, the loop emits TOOL_RESULT with the
        reviewer summary as an error. The widget already shows that summary
        in the status detail, so set_result must NOT render a result block."""
        w = ExecutePythonWidget("tc1")
        w.set_docs_gate_status("blocked", summary="rewrite guidance")
        w.set_result("rewrite guidance", is_error=True)
        self.assertFalse(w._result_block.isVisible())

    def test_failed_shows_buttons(self):
        """FAILED (reviewer crash) still lets the user approve."""
        w = ExecutePythonWidget("tc1")
        w.show_approval_buttons()
        w.set_docs_gate_status("failed", summary="boom")
        self.assertTrue(w._buttons_visible)
        self.assertIn("review manually", w._status_text.lower())

    def test_no_status_hidden_by_default(self):
        w = ExecutePythonWidget("tc1")
        self.assertFalse(w._status_visible)


class TestApprovalButtons(unittest.TestCase):
    def test_show_approval_buttons_makes_visible(self):
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)")
        w.show_approval_buttons()
        self.assertTrue(w._buttons_visible)

    def test_allow_emits_signal(self):
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)")
        w.show_approval_buttons()
        captured = []
        w.approved.connect(lambda tid, decision: captured.append((tid, decision)))
        w._on_allow()
        self.assertEqual(captured, [("tc1", "allow")])

    def test_always_allow_emits_allow_all(self):
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)")
        w.show_approval_buttons()
        captured = []
        w.approved.connect(lambda tid, decision: captured.append((tid, decision)))
        w._on_always_allow()
        self.assertEqual(captured, [("tc1", "allow_all")])

    def test_deny_emits_deny(self):
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)")
        w.show_approval_buttons()
        captured = []
        w.approved.connect(lambda tid, decision: captured.append((tid, decision)))
        w._on_deny()
        self.assertEqual(captured, [("tc1", "deny")])


class TestSetResult(unittest.TestCase):
    def test_set_result_success_shows_result_block(self):
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)")
        w.set_result("42", is_error=False)
        self.assertTrue(w._result_block.isVisible())
        self.assertFalse(w._is_error)

    def test_set_result_shows_output_in_editor(self):
        """Output must be visible immediately — no toggle required."""
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)")
        w.set_result("the answer is 42", is_error=False)
        self.assertEqual(w._result_edit.toPlainText(), "the answer is 42")
        self.assertTrue(w._result_block.isVisible())

    def test_set_result_error_marks_error_and_colors(self):
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)")
        w.set_result("NameError: x", is_error=True)
        self.assertTrue(w._result_block.isVisible())
        self.assertTrue(w._is_error)
        self.assertEqual(w._status_icon.text(), "✗")

    def test_result_short_output_compact(self):
        """A short output renders at its natural line count (no cap, no
        scroll)."""
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)")
        w.set_result("line one\nline two", is_error=False)
        lines = 2
        line_height = w._result_edit.fontMetrics().lineSpacing()
        self.assertEqual(w._result_edit.height(), line_height * lines + 16)

    def test_result_long_output_capped_and_scrollable(self):
        """A long output caps the editor height at _RESULT_MAX_LINES; the
        full text is still present in the document (scrollable)."""
        from rikugan.ui.tool_widgets import _RESULT_MAX_LINES

        long_output = "\n".join(f"line {i}" for i in range(50))
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)")
        w.set_result(long_output, is_error=False)
        line_height = w._result_edit.fontMetrics().lineSpacing()
        self.assertEqual(w._result_edit.height(), line_height * _RESULT_MAX_LINES + 16)
        # Full content preserved for scrolling.
        self.assertIn("line 49", w._result_edit.toPlainText())


class TestMarkDone(unittest.TestCase):
    def test_mark_done_is_safe_to_call(self):
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)")
        # mark_done must not raise whether or not result is set.
        w.mark_done()
        w.set_result("ok", is_error=False)
        w.mark_done()


class TestHidePreview(unittest.TestCase):
    def test_hide_preview_is_noop(self):
        """hide_preview is retained for ChatView grouping compat but is a
        no-op — the widget has no collapse state."""
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)\nprint(2)\n")
        # Should not raise and should not hide code.
        w.hide_preview()
        self.assertTrue(w._code_section().isVisible())


class TestCodeDisplayedOnce(unittest.TestCase):
    def test_no_redundant_description_label(self):
        """The widget must not carry a redundant 'Run Python code: ...'
        description — code is shown once in the code editor."""
        w = ExecutePythonWidget("tc1")
        w.set_arguments(json.dumps({"code": "import idautils\nprint(1)\n"}))
        # There should be no _description_label attribute holding a
        # duplicate of the first code line.
        self.assertFalse(getattr(w, "_description_label", None))


class TestAlwaysVisible(unittest.TestCase):
    def test_no_toggle_button_in_header(self):
        """The collapse toggle (QToolButton) is gone from the header."""
        w = ExecutePythonWidget("tc1")
        self.assertFalse(getattr(w, "_toggle_btn", None))

    def test_set_code_always_visible(self):
        """After set_code, the code section is visible without toggling."""
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)")
        self.assertTrue(w._code_section().isVisible())
        self.assertTrue(w._code_edit.isVisible())


if __name__ == "__main__":
    unittest.main()
