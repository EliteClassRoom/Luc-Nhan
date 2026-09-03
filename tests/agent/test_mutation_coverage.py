"""Drift-proof tests for mutation tracking coverage.

CLAUDE.md / AGENTS.md mandate: every database-mutating tool must appear
in ``rikugan.agent.mutation._REVERSE_BUILDERS`` (with both a real
``build_reverse_record`` builder and a matching ``capture_pre_state``
branch) OR in the explicit ``_INTENTIONALLY_NON_REVERSIBLE`` frozenset
with a per-tool reason.

This module:

1. Walks the *real* ``ToolRegistry`` (the same factory the agent uses)
   and collects the names of every ``mutating=True`` tool.
2. Asserts the union of ``_REVERSE_BUILDERS`` and
   ``_INTENTIONALLY_NON_REVERSIBLE`` covers the entire set. Any new
   mutating tool that lands without an entry breaks this assertion —
   the drift-proof guard the brief asked for.
3. Exercises each real reverse builder with a small roundtrip:
   ``capture_pre_state`` populates ``pre_state`` from a stub executor,
   ``build_reverse_record`` produces a ``MutationRecord`` whose
   ``reverse_arguments`` undo the original ``arguments``.
"""

from __future__ import annotations

import os
import sys
import typing
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.agent import mutation  # noqa: E402
from rikugan.agent.mutation import (  # noqa: E402
    _REVERSE_BUILDERS,
    build_reverse_record,
    capture_pre_state,
)
from rikugan.ida.tools import microcode_optim as _opt  # noqa: E402
from rikugan.ida.tools.registry import (  # noqa: E402
    create_default_registry,
    register_advanced_tools,
)

# ---------------------------------------------------------------------------
# Registry factory — exactly the same path the agent uses at runtime so that
# adding a new mutating tool to ``register_advanced_tools`` (or any module
# wired into ``create_default_registry``) is what flips this test to red.
# ---------------------------------------------------------------------------


def _build_test_registry():  # pragma: no cover - exercised by every test
    """Build the same registry the agent uses, with both boot + advanced tools."""
    registry = create_default_registry(dispatch_wrapper=None)
    register_advanced_tools(registry)
    return registry


def _mutating_tool_names(registry=None) -> set[str]:
    registry = registry or _build_test_registry()
    return {td.name for td in registry.list_available_tools() if td.mutating}


# ---------------------------------------------------------------------------
# Drift-proof guard
# ---------------------------------------------------------------------------


class TestMutationCoverageDriftProof(unittest.TestCase):
    """The /undo invariant: every mutating tool must have an entry."""

    def test_intentionally_non_reversible_is_frozenset_with_reasons(self) -> None:
        """The non-reversible set must be a frozenset of (tool_name, reason) tuples.

        Each entry carries an inline reason so an audit reader can see *why*
        a mutating tool is excluded from undo without grepping through
        the source.
        """
        non_rev = getattr(mutation, "_INTENTIONALLY_NON_REVERSIBLE", None)
        self.assertIsNotNone(
            non_rev,
            "_INTENTIONALLY_NON_REVERSIBLE must exist on the mutation module",
        )
        self.assertIsInstance(non_rev, frozenset, "_INTENTIONALLY_NON_REVERSIBLE must be a frozenset")
        for entry in non_rev:
            self.assertIsInstance(
                entry,
                tuple,
                f"Each entry must be a (tool_name, reason) tuple; got {entry!r}",
            )
            self.assertEqual(len(entry), 2, f"Entry {entry!r} must have exactly (name, reason)")
            name, reason = entry
            self.assertIsInstance(name, str)
            self.assertIsInstance(reason, str)
            self.assertTrue(
                reason.strip(),
                f"reason for {name!r} must be a non-empty inline justification",
            )

    def test_no_duplicate_tool_name_between_real_and_non_reversible(self) -> None:
        """A tool cannot be both real and non-reversible — pick one."""
        non_rev = {name for (name, _reason) in getattr(mutation, "_INTENTIONALLY_NON_REVERSIBLE", frozenset())}
        overlap = set(_REVERSE_BUILDERS) & non_rev
        self.assertEqual(
            overlap,
            set(),
            f"Tools cannot appear in both _REVERSE_BUILDERS and _INTENTIONALLY_NON_REVERSIBLE: {sorted(overlap)}",
        )

    def test_every_mutating_tool_is_covered(self) -> None:
        """No mutating tool may be left without an entry.

        Drift-proof guard: add a new ``mutating=True`` tool anywhere in
        the registered modules and this test fails until the tool is
        wired into either ``_REVERSE_BUILDERS`` or
        ``_INTENTIONALLY_NON_REVERSIBLE``.
        """
        registry = _build_test_registry()
        mutating = _mutating_tool_names(registry)
        covered = set(_REVERSE_BUILDERS) | {
            name for (name, _reason) in getattr(mutation, "_INTENTIONALLY_NON_REVERSIBLE", frozenset())
        }
        uncovered = mutating - covered
        self.assertEqual(
            uncovered,
            set(),
            f"Mutating tools without an undo entry: {sorted(uncovered)}. "
            "Add either a real reverse builder in _REVERSE_BUILDERS or an entry "
            "in _INTENTIONALLY_NON_REVERSIBLE with an inline reason.",
        )


# ---------------------------------------------------------------------------
# Per-builder roundtrip tests
# ---------------------------------------------------------------------------


class _BuilderRoundtripBase(unittest.TestCase):
    """Mixin: stubs the capture executor and isolates global optimizer state."""

    def setUp(self) -> None:
        _opt.installed_optimizers.clear()

    def _capture(self, tool_name: str, arguments: dict, executor) -> dict:
        return capture_pre_state(tool_name, arguments, executor)


class TestSetTypeRoundtrip(_BuilderRoundtripBase):
    def test_reverse_restores_old_type(self) -> None:
        pre = self._capture(
            "set_type",
            {"address": "0x401000", "type_string": "int"},
            lambda name, args: "unsigned char" if name == "get_function_prototype" else "",
        )
        rec = build_reverse_record(
            "set_type",
            {"address": "0x401000", "type_string": "int"},
            pre_state=pre,
        )
        self.assertTrue(rec.reversible)
        self.assertEqual(rec.reverse_tool, "set_type")
        self.assertEqual(
            rec.reverse_arguments,
            {"address": "0x401000", "type_string": "unsigned char"},
        )

    def test_reverse_with_no_prior_type_not_reversible(self) -> None:
        pre = self._capture(
            "set_type",
            {"address": "0x401000", "type_string": "int"},
            lambda name, args: "",
        )
        rec = build_reverse_record(
            "set_type",
            {"address": "0x401000", "type_string": "int"},
            pre_state=pre,
        )
        self.assertFalse(rec.reversible)
        # Description must mention the tool name so /undo displays it.
        self.assertIn("type", rec.description.lower())

    def test_capture_uses_get_function_prototype(self) -> None:
        calls = []

        def exe(name, args):
            calls.append((name, args))
            if name == "get_function_prototype":
                return "int sub()"
            return ""

        pre = self._capture("set_type", {"address": "0x401000", "type_string": "void"}, exe)
        self.assertEqual(pre.get("old_type"), "int sub()")
        self.assertEqual(calls, [("get_function_prototype", {"address": "0x401000"})])


class TestNopMicrocodeRoundtrip(_BuilderRoundtripBase):
    def test_reverse_removes_installed_optimizer(self) -> None:
        # Simulate the tool having installed an optimizer named 'my_nop'.
        _opt.installed_optimizers["my_nop"] = object()  # placeholder

        pre = self._capture(
            "nop_microcode",
            {
                "func_address": "0x401000",
                "instruction_addresses": "0x401004,0x401008",
                "optimizer_name": "my_nop",
            },
            lambda name, args: "",
        )
        rec = build_reverse_record(
            "nop_microcode",
            {
                "func_address": "0x401000",
                "instruction_addresses": "0x401004,0x401008",
                "optimizer_name": "my_nop",
            },
            pre_state=pre,
        )
        self.assertTrue(rec.reversible)
        self.assertEqual(rec.reverse_tool, "remove_microcode_optimizer")
        self.assertEqual(rec.reverse_arguments, {"name": "my_nop"})
        # Roundtrip: applying the reverse actually removes the optimizer.
        _opt.remove_optimizer("my_nop")
        self.assertNotIn("my_nop", _opt.installed_optimizers)

    def test_capture_does_not_call_executor(self) -> None:
        """nop_microcode capture is self-contained: target EAs come from args."""
        calls = []

        def exe(name, args):
            calls.append(name)
            return ""

        pre = self._capture(
            "nop_microcode",
            {
                "func_address": "0x401000",
                "instruction_addresses": "0x401004",
                "optimizer_name": "x",
            },
            exe,
        )
        # capture should populate from arguments alone, not from a getter
        self.assertEqual(calls, [])
        self.assertIn("optimizer_name", pre)


class TestModifyStructRoundtrip(_BuilderRoundtripBase):
    def test_rename_field_reversible_via_args(self) -> None:
        """rename_field carries old + new name → reverses purely from args."""
        pre = self._capture(
            "modify_struct",
            {
                "name": "Foo",
                "action": "rename_field",
                "field_name": "old_field",
                "new_name": "new_field",
            },
            lambda name, args: "",
        )
        rec = build_reverse_record(
            "modify_struct",
            {
                "name": "Foo",
                "action": "rename_field",
                "field_name": "old_field",
                "new_name": "new_field",
            },
            pre_state=pre,
        )
        self.assertTrue(rec.reversible)
        self.assertEqual(rec.reverse_tool, "modify_struct")
        self.assertEqual(rec.reverse_arguments["action"], "rename_field")
        self.assertEqual(rec.reverse_arguments["field_name"], "new_field")
        self.assertEqual(rec.reverse_arguments["new_name"], "old_field")

    def test_remove_field_reverses_to_add(self) -> None:
        """Removing a field requires its pre-state (offset/type) for the add."""
        pre = self._capture(
            "modify_struct",
            {
                "name": "Foo",
                "action": "remove_field",
                "field_name": "x",
            },
            lambda name, args: (
                # get_struct_info returns the pre-state layout using IDA's
                # tinfo_t iter_struct() output format.
                "Struct: Foo\n"
                "Size: 16 (0x10)\n"
                "Members: 1\n"
                "\n"
                "  +0x0008  int                     x                        (4 bytes)\n"
                if name == "get_struct_info"
                else ""
            ),
        )
        rec = build_reverse_record(
            "modify_struct",
            {
                "name": "Foo",
                "action": "remove_field",
                "field_name": "x",
            },
            pre_state=pre,
        )
        self.assertTrue(rec.reversible)
        self.assertEqual(rec.reverse_arguments["action"], "add_field")
        self.assertEqual(rec.reverse_arguments["field_name"], "x")
        self.assertEqual(rec.reverse_arguments["field_type"], "int")
        self.assertEqual(rec.reverse_arguments["offset"], 8)

    def test_resize_reverses_to_old_size(self) -> None:
        pre = self._capture(
            "modify_struct",
            {
                "name": "Foo",
                "action": "resize",
                "new_size": 256,
            },
            lambda name, args: (
                # get_struct_info header carries the old size
                "Struct: Foo\nSize: 128 (0x80)\nMembers: 0\n" if name == "get_struct_info" else ""
            ),
        )
        rec = build_reverse_record(
            "modify_struct",
            {
                "name": "Foo",
                "action": "resize",
                "new_size": 256,
            },
            pre_state=pre,
        )
        self.assertTrue(rec.reversible)
        self.assertEqual(rec.reverse_arguments["action"], "resize")
        self.assertEqual(rec.reverse_arguments["new_size"], 128)

    def test_capture_uses_get_struct_info(self) -> None:
        calls = []

        def exe(name, args):
            calls.append((name, args))
            if name == "get_struct_info":
                return "Struct: Foo\nSize: 8 (0x8)\nMembers: 1\n\n  +0x0000  int x (4 bytes)\n"
            return ""

        pre = self._capture(
            "modify_struct",
            {"name": "Foo", "action": "resize", "new_size": 16},
            exe,
        )
        self.assertEqual(calls, [("get_struct_info", {"name": "Foo"})])
        self.assertIn("old_struct_info_parsed", pre)
        self.assertEqual(pre["old_struct_info_parsed"]["size"], 8)


class TestModifyEnumRoundtrip(_BuilderRoundtripBase):
    def test_rename_member_reversible_via_args(self) -> None:
        """rename_member carries old + new name → reverses purely from args."""
        pre = self._capture(
            "modify_enum",
            {
                "name": "MyEnum",
                "action": "rename_member",
                "member_name": "OLD",
                "new_name": "NEW",
            },
            lambda name, args: "",
        )
        rec = build_reverse_record(
            "modify_enum",
            {
                "name": "MyEnum",
                "action": "rename_member",
                "member_name": "OLD",
                "new_name": "NEW",
            },
            pre_state=pre,
        )
        self.assertTrue(rec.reversible)
        self.assertEqual(rec.reverse_tool, "modify_enum")
        self.assertEqual(rec.reverse_arguments["action"], "rename_member")
        self.assertEqual(rec.reverse_arguments["member_name"], "NEW")
        self.assertEqual(rec.reverse_arguments["new_name"], "OLD")

    def test_remove_member_reverses_to_add(self) -> None:
        pre = self._capture(
            "modify_enum",
            {
                "name": "MyEnum",
                "action": "remove_member",
                "member_name": "X",
            },
            lambda name, args: (
                # get_enum_info returns the pre-state layout using IDA's
                # enum iteration output format.
                "Enum: MyEnum\n\n  X                                       = 0x5 (5)\n"
                if name == "get_enum_info"
                else ""
            ),
        )
        rec = build_reverse_record(
            "modify_enum",
            {
                "name": "MyEnum",
                "action": "remove_member",
                "member_name": "X",
            },
            pre_state=pre,
        )
        self.assertTrue(rec.reversible)
        self.assertEqual(rec.reverse_arguments["action"], "add_member")
        self.assertEqual(rec.reverse_arguments["member_name"], "X")
        self.assertEqual(rec.reverse_arguments["value"], 5)

    def test_capture_uses_get_enum_info(self) -> None:
        calls = []

        def exe(name, args):
            calls.append((name, args))
            if name == "get_enum_info":
                return "Enum: MyEnum\n  X = 0x5\n"
            return ""

        pre = self._capture(
            "modify_enum",
            {"name": "MyEnum", "action": "remove_member", "member_name": "X"},
            exe,
        )
        self.assertEqual(calls, [("get_enum_info", {"name": "MyEnum"})])
        self.assertIn("old_enum_info_parsed", pre)
        self.assertEqual(pre["old_enum_info_parsed"]["members"][0]["value"], 5)


# ---------------------------------------------------------------------------
# Non-reversible cases — every member of the explicit frozenset must produce
# a non-reversible MutationRecord regardless of pre-state.
# ---------------------------------------------------------------------------


class TestIntentionallyNonReversible(unittest.TestCase):
    """Each listed member returns a non-reversible MutationRecord with a useful description."""

    CASES: typing.ClassVar = [
        ("execute_python", {"code": "pass"}),
        ("create_struct", {"name": "Foo", "fields": "[]"}),
        ("create_enum", {"name": "E", "members": "[]"}),
        ("create_typedef", {"name": "T", "base_type": "int"}),
        ("import_c_header", {"c_code": "struct A{int a;};"}),
        ("import_type_from_library", {"til_name": "vcreft", "type_name": "HRESULT"}),
        ("apply_struct_to_address", {"struct_name": "Foo", "address": "0x401000"}),
        ("propagate_type", {"struct_name": "Foo"}),
        (
            "install_microcode_optimizer",
            {"name": "x", "description": "", "optimizer_type": "instruction", "python_code": ""},
        ),
        ("remove_microcode_optimizer", {"name": "x"}),
    ]

    def test_each_member_is_non_reversible(self) -> None:
        for tool_name, args in self.CASES:
            rec = build_reverse_record(tool_name, args, pre_state={})
            self.assertFalse(
                rec.reversible,
                f"{tool_name} is in _INTENTIONALLY_NON_REVERSIBLE but returned reversible={rec.reversible}",
            )
            self.assertEqual(
                rec.reverse_tool,
                "",
                f"{tool_name} is intentionally non-reversible; reverse_tool must be empty",
            )
            self.assertEqual(
                rec.tool_name,
                tool_name,
                f"{tool_name} record must carry its own name for /undo display",
            )

    def test_guard_fails_when_a_new_mutating_tool_is_added(self) -> None:
        """Adding a new mutating=True tool to the registry must flip
        the drift-proof guard red until the tool is wired in.

        Self-test for the guard: register a fake mutating tool on a
        clone of the registry and assert the same uncovered-check
        that the previous test runs against the real registry now
        flags it.
        """
        from rikugan.tools.base import ParameterSchema, ToolDefinition

        registry = _build_test_registry()
        registry.register(
            ToolDefinition(
                name="__fake_unguarded_mutator__",
                description="self-test sentinel: should fail the drift guard",
                parameters=[ParameterSchema(name="x", type="string")],
                mutating=True,
            )
        )
        mutating = _mutating_tool_names(registry)
        covered = set(_REVERSE_BUILDERS) | {
            name for (name, _reason) in getattr(mutation, "_INTENTIONALLY_NON_REVERSIBLE", frozenset())
        }
        uncovered = mutating - covered
        self.assertIn(
            "__fake_unguarded_mutator__",
            uncovered,
            "drift-proof guard must surface a freshly-registered mutating tool "
            "with no entry in _REVERSE_BUILDERS or _INTENTIONALLY_NON_REVERSIBLE",
        )


if __name__ == "__main__":
    unittest.main()
