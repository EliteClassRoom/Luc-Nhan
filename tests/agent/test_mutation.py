"""Tests for the mutation tracking module."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.agent.mutation import (
    MutationRecord,
    build_reverse_record,
    capture_pre_state,
)
from rikugan.tools.base import ParameterSchema, ToolDefinition
from rikugan.tools.coercion import coerce_bool


class TestBuildReverseRecord(unittest.TestCase):
    """Tests for build_reverse_record() generating undo operations."""

    def test_rename_function(self):
        rec = build_reverse_record(
            "rename_function",
            {"address": "0x401000", "new_name": "main"},
            pre_state={"old_name": "sub_401000"},
        )
        self.assertIsNotNone(rec)
        self.assertTrue(rec.reversible)
        self.assertEqual(rec.reverse_tool, "rename_function")
        self.assertEqual(rec.reverse_arguments, {"address": "0x401000", "new_name": "sub_401000"})

    def test_rename_variable(self):
        rec = build_reverse_record(
            "rename_variable",
            {"func_address": "0x401000", "old_name": "var_10", "new_name": "counter"},
        )
        self.assertIsNotNone(rec)
        self.assertTrue(rec.reversible)
        self.assertEqual(rec.reverse_tool, "rename_variable")
        self.assertEqual(
            rec.reverse_arguments,
            {"func_address": "0x401000", "old_name": "counter", "new_name": "var_10"},
        )

    def test_set_comment_with_existing(self):
        rec = build_reverse_record(
            "set_comment",
            {"address": "0x401000", "comment": "new comment", "repeatable": True},
            pre_state={"old_comment": "old comment"},
        )
        self.assertIsNotNone(rec)
        self.assertTrue(rec.reversible)
        self.assertEqual(rec.reverse_tool, "set_comment")
        self.assertEqual(
            rec.reverse_arguments,
            {"address": "0x401000", "comment": "old comment", "repeatable": True},
        )

        for repeatable in ("false", "0", "no", "n", "off", ""):
            rec = build_reverse_record(
                "set_comment",
                {"address": "0x401000", "comment": "new comment", "repeatable": repeatable},
                pre_state={"old_comment": "old comment"},
            )
            self.assertTrue(rec.reversible)
            self.assertEqual(
                rec.reverse_arguments,
                {"address": "0x401000", "comment": "old comment", "repeatable": False},
            )

    def test_set_comment_without_existing(self):
        rec = build_reverse_record(
            "set_comment",
            {"address": "0x401000", "comment": "new comment"},
            pre_state={"old_comment": ""},
        )
        self.assertIsNotNone(rec)
        self.assertTrue(rec.reversible)
        self.assertEqual(rec.reverse_tool, "set_comment")
        self.assertEqual(rec.reverse_arguments, {"address": "0x401000", "comment": "", "repeatable": False})

        rec = build_reverse_record(
            "set_comment",
            {"address": "0x401000", "comment": "new comment"},
        )
        self.assertFalse(rec.reversible)
        self.assertEqual(rec.reverse_tool, "")
        self.assertEqual(rec.reverse_arguments, {})

    def test_set_comment_old_none_not_reversible(self):
        """old_comment=None must be treated as non-reversible (getter failed)."""
        rec = build_reverse_record(
            "set_comment",
            {"address": "0x401000", "comment": "new comment"},
            pre_state={"old_comment": None},
        )
        self.assertFalse(rec.reversible)
        self.assertEqual(rec.reverse_tool, "")

    def test_set_comment_old_empty_is_reversible(self):
        """old_comment='' must remain reversible (was genuinely empty)."""
        rec = build_reverse_record(
            "set_comment",
            {"address": "0x401000", "comment": "new"},
            pre_state={"old_comment": ""},
        )
        self.assertTrue(rec.reversible)
        self.assertEqual(rec.reverse_arguments["comment"], "")

    def test_set_function_comment_reverses_to_tagged_only(self):
        """Plan §4.38 requires exact tagged-comment replacement:
        ``merge_evidence_line`` replaces a single tagged line while
        preserving other analyst text. The reverse record for the
        resulting ``set_function_comment`` call must still point at
        the original pre-state, proving the undo path remains sound.
        """
        from rikugan.ida.tools.annotations import merge_evidence_line

        original = "analyst note\n[Rikugan Evidence] old claim\nfollowup"
        merged = merge_evidence_line(original, "new claim")
        self.assertIn("analyst note", merged)
        self.assertIn("followup", merged)
        self.assertIn("[Rikugan Evidence] new claim", merged)
        self.assertNotIn("old claim", merged)
        rec = build_reverse_record(
            "set_function_comment",
            {
                "address": "0x401000",
                "comment": merged,
                "repeatable": True,
            },
            pre_state={"old_comment": original},
        )
        self.assertTrue(rec.reversible)
        self.assertEqual(rec.reverse_tool, "set_function_comment")
        self.assertEqual(
            rec.reverse_arguments,
            {
                "address": "0x401000",
                "comment": original,
                "repeatable": True,
            },
        )


    def test_set_function_comment_old_none_not_reversible(self):
        """old_comment=None must be treated as non-reversible."""
        rec = build_reverse_record(
            "set_function_comment",
            {"address": "0x401000", "comment": "new"},
            pre_state={"old_comment": None},
        )
        self.assertFalse(rec.reversible)
        self.assertEqual(rec.reverse_tool, "")

    def test_set_function_comment_old_empty_is_reversible(self):
        """old_comment='' must remain reversible."""
        rec = build_reverse_record(
            "set_function_comment",
            {"address": "0x401000", "comment": "new"},
            pre_state={"old_comment": ""},
        )
        self.assertTrue(rec.reversible)
        self.assertEqual(rec.reverse_arguments["comment"], "")

    def test_rename_address_with_old_name(self):
        rec = build_reverse_record(
            "rename_address",
            {"address": "0x600000", "new_name": "g_counter"},
            pre_state={"old_name": "data_600000"},
        )
        self.assertTrue(rec.reversible)
        self.assertEqual(rec.reverse_tool, "rename_address")
        self.assertEqual(rec.reverse_arguments, {"address": "0x600000", "new_name": "data_600000"})

    def test_rename_address_without_old_name(self):
        rec = build_reverse_record(
            "rename_address",
            {"address": "0x600000", "new_name": "g_counter"},
        )
        self.assertFalse(rec.reversible)
        self.assertEqual(rec.reverse_tool, "")
        self.assertEqual(rec.reverse_arguments, {})

    def test_set_function_prototype(self):
        rec = build_reverse_record(
            "set_function_prototype",
            {"address": "0x401000", "prototype": "int main(int argc, char **argv)"},
            pre_state={"old_prototype": "int sub_401000()"},
        )
        self.assertTrue(rec.reversible)
        self.assertEqual(rec.reverse_tool, "set_function_prototype")
        self.assertEqual(
            rec.reverse_arguments,
            {"address": "0x401000", "prototype": "int sub_401000()"},
        )

    def test_retype_variable(self):
        rec = build_reverse_record(
            "apply_type_to_variable",
            {"func_address": "0x401000", "var_name": "v1", "type_str": "int *"},
            pre_state={"old_type": "void *"},
        )
        self.assertTrue(rec.reversible)
        self.assertEqual(rec.reverse_tool, "apply_type_to_variable")
        self.assertEqual(
            rec.reverse_arguments,
            {"func_address": "0x401000", "var_name": "v1", "type_str": "void *"},
        )

    def test_unknown_tool_not_reversible(self):
        rec = build_reverse_record(
            "execute_python",
            {"code": "print('hello')"},
        )
        self.assertIsNotNone(rec)
        self.assertFalse(rec.reversible)
        self.assertEqual(rec.reverse_tool, "")
        self.assertEqual(rec.reverse_arguments, {})

        incomplete_records = [
            build_reverse_record("rename_function", {"new_name": "main"}, {"old_name": "sub_401000"}),
            build_reverse_record("set_comment", {"comment": "new"}, {"old_comment": "old"}),
            build_reverse_record("set_function_comment", {"comment": "new"}, {"old_comment": "old"}),
            build_reverse_record(
                "set_pseudocode_comment",
                {"func_address": "0x401000", "comment": "new"},
                {"old_comment": "old"},
            ),
            build_reverse_record(
                "set_pseudocode_comment",
                {"func_address": "0x401000", "target_address": "0x401010", "comment": "new"},
            ),
            build_reverse_record(
                "set_function_prototype",
                {"prototype": "int f(void)"},
                {"old_prototype": "int old(void)"},
            ),
            build_reverse_record(
                "apply_type_to_variable",
                {"func_address": "0x401000", "type_str": "int"},
                {"old_type": "char"},
            ),
        ]
        for incomplete in incomplete_records:
            self.assertFalse(incomplete.reversible)
            self.assertEqual(incomplete.reverse_tool, "")
            self.assertEqual(incomplete.reverse_arguments, {})

    def test_pseudocode_comment_old_none_not_reversible(self):
        """old_comment=None from failed decompile must not be reversible."""
        rec = build_reverse_record(
            "set_pseudocode_comment",
            {"func_address": "0x401000", "target_address": "0x401010", "comment": "new"},
            pre_state={"old_comment": None},
        )
        self.assertFalse(rec.reversible)
        self.assertEqual(rec.reverse_tool, "")

    def test_pseudocode_comment_old_empty_is_reversible(self):
        """old_comment='' with ok=true must remain reversible."""
        rec = build_reverse_record(
            "set_pseudocode_comment",
            {"func_address": "0x401000", "target_address": "0x401010", "comment": "new"},
            pre_state={"old_comment": ""},
        )
        self.assertTrue(rec.reversible)
        self.assertEqual(rec.reverse_tool, "set_pseudocode_comment")
        self.assertEqual(rec.reverse_arguments["comment"], "")

    def test_pseudocode_comment_non_string_not_reversible(self):
        """Non-string old_comment must not be reversible."""
        rec = build_reverse_record(
            "set_pseudocode_comment",
            {"func_address": "0x401000", "target_address": "0x401010", "comment": "new"},
            pre_state={"old_comment": 42},
        )
        self.assertFalse(rec.reversible)

    def test_boolean_consistency_registry_and_mutation(self):
        """coerce_bool() must agree with ToolRegistry coercion for both truthy and falsy repeatable values."""
        from rikugan.tools.registry import ToolRegistry

        bool_defn = ToolDefinition(
            name="test_bool",
            description="test",
            parameters=[ParameterSchema(name="repeatable", type="boolean")],
        )
        # --- truthy values ---
        for val in ("true", "1", "yes", "y", "on", "TRUE", " YES ", "\tyes\t"):
            reg = ToolRegistry._coerce_arguments(bool_defn, {"repeatable": val})
            mut = coerce_bool(val)
            self.assertEqual(reg["repeatable"], mut, f"Truthy mismatch for {val!r}")
            self.assertTrue(reg["repeatable"], f"Expected {val!r} → True")

        # --- falsy values ---
        for val in ("false", "0", "no", "n", "off", "", " false ", " off "):
            reg = ToolRegistry._coerce_arguments(bool_defn, {"repeatable": val})
            mut = coerce_bool(val)
            self.assertEqual(reg["repeatable"], mut, f"Falsy mismatch for {val!r}")
            self.assertFalse(reg["repeatable"], f"Expected {val!r} → False")

    def test_description_populated(self):
        rec = build_reverse_record(
            "rename_function",
            {"address": "0x401000", "new_name": "main"},
            pre_state={"old_name": "sub_401000"},
        )
        self.assertIn("sub_401000", rec.description)
        self.assertIn("main", rec.description)


class TestCapturePreState(unittest.TestCase):
    """Tests for capture_pre_state() fetching current state before mutation."""

    def test_set_comment_captures_old(self):
        calls = []

        def mock_executor(name, args):
            calls.append((name, args))
            if name == "get_comment":
                return "existing comment"
            return ""

        pre = capture_pre_state("set_comment", {"address": "0x1000", "repeatable": True}, mock_executor)
        self.assertEqual(pre["old_comment"], "existing comment")
        self.assertEqual(calls, [("get_comment", {"address": "0x1000", "repeatable": True})])

        for repeatable in ("false", "0", "no", "n", "off", ""):
            calls.clear()
            pre = capture_pre_state("set_comment", {"address": "0x1000", "repeatable": repeatable}, mock_executor)
            self.assertEqual(pre["old_comment"], "existing comment")
            self.assertEqual(calls, [("get_comment", {"address": "0x1000", "repeatable": False})])

    def test_set_function_comment_captures_old(self):
        calls = []

        def mock_executor(name, args):
            calls.append((name, args))
            if name == "get_function_comment":
                return "func comment"
            return ""

        pre = capture_pre_state(
            "set_function_comment",
            {"address": "0x401000", "repeatable": "false"},
            mock_executor,
        )
        self.assertEqual(pre["old_comment"], "func comment")
        self.assertEqual(calls, [("get_function_comment", {"address": "0x401000", "repeatable": False})])

    def test_name_capture_uses_raw_getter_tools(self):
        calls = []

        def mock_executor(name, args):
            calls.append((name, args))
            if name == "get_function_name":
                return "sub_401000"
            if name == "get_address_name":
                return "data_500000"
            return ""

        pre = capture_pre_state("rename_function", {"address": "0x401000", "new_name": "b"}, mock_executor)
        self.assertEqual(pre, {"old_name": "sub_401000"})

        pre = capture_pre_state("rename_address", {"address": "0x500000", "new_name": "g_count"}, mock_executor)
        self.assertEqual(pre, {"old_name": "data_500000"})
        self.assertEqual(
            calls,
            [
                ("get_function_name", {"address": "0x401000"}),
                ("get_address_name", {"address": "0x500000"}),
            ],
        )

    def test_executor_failure_graceful(self):
        def mock_executor(name, args):
            raise RuntimeError("tool not available")

        # Should not raise, just return empty pre_state
        pre = capture_pre_state("set_comment", {"address": "0x1000"}, mock_executor)
        self.assertEqual(pre, {})

    def test_pseudocode_comment_state_ok_true(self):
        """ok=true with a real comment should set old_comment."""
        calls = []

        def mock_executor(name, args):
            calls.append((name, args))
            if name == "get_pseudocode_comment_state":
                return '{"ok": true, "comment": "old pseudocode note"}'
            return ""

        pre = capture_pre_state(
            "set_pseudocode_comment",
            {"func_address": "0x401000", "target_address": "0x401010"},
            mock_executor,
        )
        self.assertEqual(pre["old_comment"], "old pseudocode note")
        self.assertEqual(len(calls), 1)

    def test_pseudocode_comment_state_ok_true_empty(self):
        """ok=true with empty comment must capture '' as reversible pre-state."""
        calls = []

        def mock_executor(name, args):
            calls.append((name, args))
            if name == "get_pseudocode_comment_state":
                return '{"ok": true, "comment": ""}'
            return ""

        pre = capture_pre_state(
            "set_pseudocode_comment",
            {"func_address": "0x401000", "target_address": "0x401010"},
            mock_executor,
        )
        self.assertEqual(pre["old_comment"], "")

    def test_pseudocode_comment_failure_and_malformed_states(self):
        """Verify that capture_pre_state returns old_comment=None for all failure modes.

        Covers: ok=false, malformed JSON, non-dict decoded values, and
        ok=true with a non-string comment field.
        """

        def _capture(result):
            calls = []

            def mock_executor(name, args):
                calls.append((name, args))
                if name == "get_pseudocode_comment_state":
                    return result
                return ""

            return capture_pre_state(
                "set_pseudocode_comment",
                {"func_address": "0x401000", "target_address": "0x401010"},
                mock_executor,
            )

        # ok=false → None
        pre = _capture('{"ok": false, "comment": ""}')
        self.assertIsNone(pre["old_comment"])
        self.assertIn("old_comment", pre)

        # malformed JSON → None
        pre = _capture("not json {{{")
        self.assertIsNone(pre["old_comment"])
        self.assertIn("old_comment", pre)

        # decoded non-dict (JSON list) → None
        pre = _capture('["a", "b"]')
        self.assertIsNone(pre["old_comment"])
        self.assertIn("old_comment", pre)

        # ok=true with non-string comment (int) → None
        pre = _capture('{"ok": true, "comment": 42}')
        self.assertIsNone(pre["old_comment"])
        self.assertIn("old_comment", pre)

    def test_comment_exact_whitespace_preserved(self):
        """Comment pre-state must preserve exact leading/trailing whitespace."""
        calls = []

        def mock_executor(name, args):
            calls.append((name, args))
            if name == "get_comment":
                return " hello "  # leading + trailing space
            return ""

        pre = capture_pre_state("set_comment", {"address": "0x1000"}, mock_executor)
        self.assertEqual(pre["old_comment"], " hello ")

        # Verify build_reverse_record preserves the exact whitespace
        rec = build_reverse_record(
            "set_comment",
            {"address": "0x1000", "comment": "new"},
            pre_state={"old_comment": " hello "},
        )
        self.assertTrue(rec.reversible)
        self.assertEqual(rec.reverse_arguments["comment"], " hello ")


class TestMutationRecord(unittest.TestCase):
    """Tests for MutationRecord dataclass."""

    def test_defaults(self):
        rec = MutationRecord(
            tool_name="test",
            arguments={},
            reverse_tool="test_reverse",
            reverse_arguments={},
        )
        self.assertTrue(rec.reversible)
        self.assertGreater(rec.timestamp, 0)
        self.assertEqual(rec.description, "")

    def test_non_reversible(self):
        rec = MutationRecord(
            tool_name="execute_python",
            arguments={"code": "x=1"},
            reverse_tool="",
            reverse_arguments={},
            reversible=False,
        )
        self.assertFalse(rec.reversible)


class TestMutationContractConsistency(unittest.TestCase):
    """Every mutating tool must have BOTH a reverse builder and a capture_pre_state branch.

    This is the /undo correctness contract mandated by CLAUDE.md: a mutating tool
    that has one handler but not the other breaks undo replay.
    """

    @classmethod
    def setUpClass(cls):
        # Inspect the module internals to enumerate both handler sets.
        import inspect

        from rikugan.agent import mutation

        cls._reverse_tools = set(mutation._REVERSE_BUILDERS.keys())

        # Extract tool names from capture_pre_state's if/elif branches by
        # parsing the source — the function is a flat dispatch.
        source = inspect.getsource(mutation.capture_pre_state)
        cls._capture_tools = {line.split('"')[1] for line in source.splitlines() if 'tool_name == "' in line}

    def test_reverse_builder_tools_have_capture_branch(self):
        """Every tool with a reverse builder must capture pre-state.

        Exception: tools whose reverse record can be built entirely from the
        call arguments (e.g. rename_variable carries old_name as an explicit
        parameter) do not need a pre-state branch.
        """
        self_sufficient = {"rename_variable"}
        missing = (self._reverse_tools - self._capture_tools) - self_sufficient
        self.assertEqual(
            missing,
            set(),
            f"Tools with reverse builders but no capture_pre_state branch: {missing}. "
            "These tools cannot replay undo because their pre-state is never captured.",
        )

    def test_capture_tools_have_reverse_builder(self):
        """Every tool that captures pre-state must have a reverse builder."""
        missing = self._capture_tools - self._reverse_tools
        self.assertEqual(
            missing,
            set(),
            f"Tools with capture_pre_state branches but no reverse builder: {missing}. "
            "These tools capture state that is never used for undo.",
        )

    def test_unknown_tool_is_non_reversible(self):
        """Unknown mutating tools fall back to a non-reversible record, never crash."""
        rec = build_reverse_record("unknown_tool_xyz", {"address": "0x1000"}, {})
        self.assertFalse(rec.reversible)
        self.assertEqual(rec.tool_name, "unknown_tool_xyz")

    def test_capture_pre_state_handles_unknown_tool_gracefully(self):
        """capture_pre_state returns empty dict for unknown tools (no crash)."""
        executor = lambda name, args: {"unused": True}  # noqa: E731
        pre = capture_pre_state("unknown_tool_xyz", {"address": "0x1000"}, executor)
        self.assertEqual(pre, {})

    def test_capture_pre_state_swallows_executor_failures(self):
        """capture_pre_state logs and returns empty dict if a getter fails (best-effort)."""
        executor = lambda name, args: (_ for _ in ()).throw(RuntimeError("boom"))  # noqa: E731
        pre = capture_pre_state("rename_function", {"address": "0x1000"}, executor)
        self.assertEqual(pre, {})


if __name__ == "__main__":
    unittest.main()
