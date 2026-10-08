"""Tests for the IDAPython static validator.

Pure-Python tests — no IDA mocks required. Covers all three severity paths
(BLOCK, WARN, clean) plus edge cases like syntax errors, multi-line scripts,
deeply-nested attribute chains, and the original hallucination that
motivated this validator (``idaapi.get_operands()``).
"""

from __future__ import annotations

import unittest

from lucnhan.tools.validate_idapython import (
    BLOCKED_CALLS,
    BLOCKED_MODULES,
    WARNED_CALLS,
    validate_idapython,
)


class TestBlockedCalls(unittest.TestCase):
    """The actual bug from the user's bug report."""

    def test_idaapi_get_operands_is_blocked(self):
        """The motivating case: agent hallucinated idaapi.get_operands()."""
        source = (
            "import idautils\n"
            "import idaapi\n"
            "ops = idaapi.get_operands(0x401000)\n"  # the hallucinated call
        )
        result = validate_idapython(source)
        self.assertTrue(result.is_blocked)
        self.assertEqual(len(result.blocked_issues), 1)
        issue = result.blocked_issues[0]
        self.assertEqual(issue.call, "idaapi.get_operands")
        self.assertEqual(issue.line, 3)
        self.assertIn("decode_insn", issue.fix)

    def test_get_instruction_operands_blocked(self):
        source = "idaapi.get_instruction_operands(ea)"
        result = validate_idapython(source)
        self.assertTrue(result.is_blocked)
        self.assertEqual(result.blocked_issues[0].call, "idaapi.get_instruction_operands")

    def test_get_insn_operands_blocked(self):
        source = "idaapi.get_insn_operands(ea)"
        result = validate_idapython(source)
        self.assertTrue(result.is_blocked)

    def test_idautils_GetOperands_blocked(self):
        """PascalCase variant."""
        source = "ops = idautils.GetOperands(ea)"
        result = validate_idapython(source)
        self.assertTrue(result.is_blocked)
        self.assertEqual(result.blocked_issues[0].call, "idautils.GetOperands")


class TestBlockedModules(unittest.TestCase):
    """Modules removed in IDA 9.x."""

    def test_import_ida_struct_blocked(self):
        source = "import ida_struct\n"
        result = validate_idapython(source)
        self.assertTrue(result.is_blocked)
        self.assertIn("ida_struct", result.blocked_issues[0].call)

    def test_import_ida_enum_blocked(self):
        source = "import ida_enum\n"
        result = validate_idapython(source)
        self.assertTrue(result.is_blocked)

    def test_from_ida_struct_blocked(self):
        source = "from ida_struct import add_struc\n"
        result = validate_idapython(source)
        self.assertTrue(result.is_blocked)
        self.assertIn("ida_struct", result.blocked_issues[0].call)
        self.assertIn("add_struc", result.blocked_issues[0].call)


class TestWarnedCalls(unittest.TestCase):
    """Legacy APIs — should warn but NOT block."""

    def test_idc_GetOperandValue_warns_not_blocks(self):
        source = "val = idc.GetOperandValue(0x401000, 0)"
        result = validate_idapython(source)
        self.assertFalse(result.is_blocked)
        self.assertEqual(len(result.warnings), 1)
        self.assertEqual(result.warnings[0].call, "idc.GetOperandValue")

    def test_idc_GetOpnd_warns(self):
        source = "op = idc.GetOpnd(0x401000, 1)"
        result = validate_idapython(source)
        self.assertFalse(result.is_blocked)
        self.assertEqual(len(result.warnings), 1)

    def test_idc_ScreenEA_warns(self):
        source = "ea = idc.ScreenEA()"
        result = validate_idapython(source)
        self.assertFalse(result.is_blocked)


class TestCleanScripts(unittest.TestCase):
    """Scripts using the correct APIs should pass through cleanly."""

    def test_modern_insn_t_decode_insn_pattern_clean(self):
        source = (
            "import ida_ua\n"
            "insn = ida_ua.insn_t()\n"
            "if ida_ua.decode_insn(insn, 0x401000):\n"
            "    for op in insn.ops:\n"
            "        if op.type == ida_ua.o_imm:\n"
            "            print(f'{op.value:#x}')\n"
        )
        result = validate_idapython(source)
        self.assertFalse(result.is_blocked, msg=f"Got: {result.format_for_agent()}")
        self.assertEqual(len(result.warnings), 0)

    def test_idautils_Functions_clean(self):
        source = "import idautils\nfor ea in idautils.Functions():\n    print(hex(ea))\n"
        result = validate_idapython(source)
        self.assertFalse(result.is_blocked)
        self.assertEqual(len(result.warnings), 0)

    def test_idc_StillWorks(self):
        """Plain idc calls that aren't in the warned list should not trigger."""
        source = "print(idc.MAXSTR)\n"
        result = validate_idapython(source)
        self.assertFalse(result.is_blocked)
        self.assertEqual(len(result.warnings), 0)

    def test_empty_source_clean(self):
        result = validate_idapython("")
        self.assertFalse(result.is_blocked)
        self.assertEqual(len(result.issues), 0)

    def test_comments_only_clean(self):
        source = "# this is just a comment\n# another\n"
        result = validate_idapython(source)
        self.assertEqual(len(result.issues), 0)


class TestSyntaxErrors(unittest.TestCase):
    """SyntaxError should not be classified as a hallucination."""

    def test_syntax_error_captured_not_blocked(self):
        source = "this is not valid python !!!"
        result = validate_idapython(source)
        self.assertIsNotNone(result.syntax_error)
        self.assertFalse(result.is_blocked)
        self.assertEqual(len(result.issues), 0)

    def test_format_for_agent_handles_syntax_error(self):
        source = "def foo(:\n"
        result = validate_idapython(source)
        formatted = result.format_for_agent()
        self.assertIn("SYNTAX ERROR", formatted)


class TestMultipleIssues(unittest.TestCase):
    """Scripts with multiple problems should report all of them."""

    def test_mixed_block_and_warn_in_one_script(self):
        source = (
            "import ida_struct\n"  # BLOCK (module removed)
            "val = idc.GetOperandValue(0x401000, 0)\n"  # WARN
            "ops = idaapi.get_operands(0x401000)\n"  # BLOCK
        )
        result = validate_idapython(source)
        self.assertTrue(result.is_blocked)
        self.assertEqual(len(result.blocked_issues), 2)
        self.assertEqual(len(result.warnings), 1)

    def test_format_for_agent_includes_all(self):
        source = "import ida_struct\nops = idaapi.get_operands(0x401000)\n"
        result = validate_idapython(source)
        formatted = result.format_for_agent()
        self.assertIn("BLOCK", formatted)
        self.assertIn("ida_struct", formatted)
        self.assertIn("get_operands", formatted)


class TestEdgeCases(unittest.TestCase):
    """Corner cases the AST walker must handle gracefully."""

    def test_attribute_chain_resolution(self):
        """Multi-level attribute chain resolves fully."""
        # (Will be classified as a generic Attribute call — but the walker
        # must not crash. It's not in our tables, so no issue.)
        source = "val = idaapi.foo.bar.baz(0x401000)"
        result = validate_idapython(source)
        self.assertEqual(len(result.issues), 0)

    def test_nested_call_in_expression(self):
        """Hallucinated call nested inside an expression still caught."""
        source = "result = [op for op in idaapi.get_operands(ea)]\n"
        result = validate_idapython(source)
        self.assertTrue(result.is_blocked)

    def test_call_in_lambda(self):
        """Call inside a lambda body is still caught."""
        source = "f = lambda ea: idaapi.get_operands(ea)\n"
        result = validate_idapython(source)
        self.assertTrue(result.is_blocked)

    def test_multiline_source_correct_line_numbers(self):
        source = (
            "\n"  # line 1
            "\n"  # line 2
            "idaapi.get_operands(0x401000)\n"  # line 3
        )
        result = validate_idapython(source)
        self.assertEqual(result.blocked_issues[0].line, 3)


class TestApiTablesAreWired(unittest.TestCase):
    """Sanity check: the BLOCK/WARN tables are not empty and contain
    the canonical example."""

    def test_blocked_calls_nonempty(self):
        self.assertGreater(len(BLOCKED_CALLS), 5)

    def test_warned_calls_nonempty(self):
        self.assertGreater(len(WARNED_CALLS), 2)

    def test_blocked_modules_nonempty(self):
        self.assertIn("ida_struct", BLOCKED_MODULES)
        self.assertIn("ida_enum", BLOCKED_MODULES)

    def test_canonical_get_operands_in_blocked(self):
        self.assertIn("idaapi.get_operands", BLOCKED_CALLS)


if __name__ == "__main__":
    unittest.main()
