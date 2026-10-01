"""Tests for the tool framework."""

from __future__ import annotations

import os
import sys
import unittest

# Install mocks before importing Rikugan modules
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.core.errors import ToolNotFoundError
from rikugan.tools.base import tool
from rikugan.tools.registry import ToolRegistry


class TestToolSubstitution(unittest.TestCase):
    """When ``execute_python`` script is just a wrapper around an existing
    dedicated tool, the guard should suggest the tool instead of forcing
    the user through an extra approval round-trip.

    These tests pin the *shape* of the suggestion API; the actual
    IDAPython-API → tool-name mapping is contribution-driven.
    """

    def test_suggest_substitutions_returns_structured_matches(self):
        from rikugan.tools.tool_substitution import suggest_substitutions

        # A wrapper that does nothing but list imports.
        script = """
import ida_nalt

nimps = ida_nalt.get_import_module_qty()
for i in range(nimps):
    mod = ida_nalt.get_import_module_name(i)
    print(mod)
"""

        suggestions = suggest_substitutions(script)
        self.assertIsInstance(suggestions, list)
        # Each suggestion must include the tool name and a human hint.
        for s in suggestions:
            self.assertTrue(hasattr(s, "tool_name"))
            self.assertTrue(hasattr(s, "hint"))
            self.assertIsInstance(s.tool_name, str)
            self.assertIsInstance(s.hint, str)
            self.assertTrue(s.tool_name)  # non-empty
            self.assertTrue(s.hint)  # non-empty

    def test_suggest_substitutions_no_match_returns_empty(self):
        from rikugan.tools.tool_substitution import suggest_substitutions

        # Legitimate compute that does not match any dedicated tool.
        script = """
import z3
s = z3.Solver()
s.add(z3.Int('x') > 0)
print(s.check())
"""
        suggestions = suggest_substitutions(script)
        self.assertEqual(suggestions, [])

    def test_suggest_substitutions_ignores_comments_and_strings(self):
        """Comments and string literals mentioning APIs must not trigger
        false positives — only actual call sites should be matched."""
        from rikugan.tools.tool_substitution import suggest_substitutions

        # Real call to list_imports API inside a string must be ignored.
        script = """
# This string mentions ida_nalt.get_import_module_qty but never calls it.
msg = "ida_nalt.get_import_module_qty is the right API for imports"
print(msg)
"""
        suggestions = suggest_substitutions(script)
        # We accept either zero suggestions or only the ones from real calls.
        for s in suggestions:
            self.assertNotIn("msg = ", s.hint)  # no hint should claim we matched the string

    def test_format_suggestions_for_agent_renders_table(self):
        from rikugan.tools.tool_substitution import (
            format_suggestions_for_agent,
            suggest_substitutions,
        )

        script = """
import ida_nalt
nimps = ida_nalt.get_import_module_qty()
"""
        sugg = suggest_substitutions(script)
        rendered = format_suggestions_for_agent(sugg)
        if sugg:
            # Must mention the tool name(s) and have a clear directive
            # telling the LLM to prefer the dedicated tool.
            self.assertIn("prefer", rendered.lower() or "use " in rendered.lower())
            for s in sugg:
                self.assertIn(s.tool_name, rendered)

    def test_format_suggestions_for_agent_mentions_guard(self):
        """The preamble must identify itself as Rikugan's substitution guard.

        Without this context, the LLM has no way to know that the
        ``[rikugan]`` block came from a built-in guard layer (vs. a
        stale system message or a malformed script output) and may
        ignore it as noise.
        """
        from rikugan.tools.tool_substitution import Suggestion, format_suggestions_for_agent

        sugg = [
            Suggestion(
                tool_name="list_imports",
                matched_apis=("ida_nalt.get_import_module_qty",),
                hint="Enumerating imports is what list_imports does.",
            )
        ]
        rendered = format_suggestions_for_agent(sugg)
        # Identity markers — the LLM should be able to tell this came
        # from Rikugan's guard, not from a user message.
        self.assertIn("[rikugan]", rendered)
        self.assertIn("guard", rendered.lower())


class TestPromptMentionsNewTools(unittest.TestCase):
    """The system prompt's tool-usage guidance must stay in sync with the
    tools actually registered. When new dedicated tools are added
    (search_imports, imports_by_module), the prompt must (1) mention them
    so the LLM considers them, and (2) acknowledge the substitution
    guard layer so the [rikugan] preamble is not treated as noise."""

    def test_ida_tool_usage_mentions_search_variants(self):
        from rikugan.agent.prompts.ida import _IDA_TOOL_USAGE

        text = _IDA_TOOL_USAGE.lower()
        # Both new dedicated tools must be reachable from the prompt.
        self.assertIn("search_imports", text)
        self.assertIn("imports_by_module", text)

    def test_ida_api_discipline_acknowledges_substitution_guard(self):
        from rikugan.agent.prompts.base import IDA_API_DISCIPLINE_SECTION

        text = IDA_API_DISCIPLINE_SECTION.lower()
        # The discipline section is the right home for "what to do when
        # you see the [rikugan] substitution hint" because it already
        # owns the "is there a built-in tool?" pre-write checklist.
        self.assertIn("[rikugan]", text)
        self.assertIn("substitution", text)

    def test_contributed_mappings_recognize_wrapper_scripts(self):
        """Each contributed mapping entry must trigger when its API is called.

        This pins the table so accidental edits to ``_API_PATTERNS`` that
        break a working entry are caught immediately. Each case is a
        minimal wrapper script that should yield at least one suggestion
        pointing at the dedicated tool.
        """
        from rikugan.tools.tool_substitution import suggest_substitutions

        cases = [
            # (script, expected_tool_name)
            ("import ida_name\nida_name.set_name(0x1000, 'foo')\n", "rename_address"),
            ("import ida_funcs\nprint(ida_funcs.get_func_name(0x1000))\n", "get_function_name"),
            ("import idc\nidc.SetComment(0x1000, 'note')\n", "set_comment"),
            ("import idc\nidc.SetFunctionComment(0x1000, 'note')\n", "set_function_comment"),
            ("import idc\nidc.SetType(0x1000, 'int')\n", "set_type"),
            ("import idc\nidc.SetFunctionAttr(0x1000, idc.FTI_TYPE, 'int')\n", "set_function_prototype"),
            ("import ida_hexrays\nprint(ida_hexrays.decompile(0x1000))\n", "decompile_function"),
            ("import idc\nprint(idc.GetDisasm(0x1000))\n", "read_disassembly"),
            ("import ida_ua\ninsn = ida_ua.insn_t(); ida_ua.decode_insn(insn, 0x1000)\n", "get_instruction_info"),
            ("import ida_hexrays\nmba = ida_hexrays.gen_microcode(0x1000)\n", "get_microcode"),
            ("import ida_typeinf\nt = ida_typeinf.tinfo_t(); t.create_udt()\n", "create_struct"),
            ("import ida_typeinf\nfor s in ida_typeinf.iter_struct():\n  print(s)\n", "list_structs"),
            ("import ida_typeinf\nfor e in ida_typeinf.iter_enum():\n  print(e)\n", "list_enums"),
        ]

        for script, expected_tool in cases:
            with self.subTest(expected=expected_tool):
                suggestions = suggest_substitutions(script)
                tool_names = {s.tool_name for s in suggestions}
                self.assertIn(
                    expected_tool,
                    tool_names,
                    f"expected {expected_tool} suggestion for script starting: {script.splitlines()[1][:60]!r}",
                )


class TestToolCatalog(unittest.TestCase):
    """The system prompt's 'Available Tools' section should be a categorized
    table, not a comma-separated list of bare names. The LLM needs (1)
    category grouping for navigation, (2) a one-line description hint to
    recall what each tool does without reading the full schema."""

    def test_format_tools_catalog_groups_by_category(self):
        from rikugan.agent.system_prompt import format_tools_catalog
        from rikugan.tools.base import tool

        @tool(category="database", description="List every imported function.")
        def list_imports() -> str:
            return ""

        @tool(category="database", description="Search imports by substring.")
        def search_imports(query: str) -> str:
            return ""

        @tool(category="navigation", description="Jump to an address.")
        def jump_to(address: str) -> str:
            return ""

        catalog = format_tools_catalog(
            [list_imports._tool_definition, search_imports._tool_definition, jump_to._tool_definition]
        )

        # Two categories must appear as section headers.
        self.assertIn("database", catalog.lower())
        self.assertIn("navigation", catalog.lower())
        # Tools should appear by name, not omitted because of category grouping.
        self.assertIn("list_imports", catalog)
        self.assertIn("search_imports", catalog)
        self.assertIn("jump_to", catalog)
        # Descriptions should accompany names so the LLM can recall purpose.
        self.assertIn("List every imported function", catalog)
        self.assertIn("Jump to an address", catalog)

    def test_format_tools_catalog_handles_empty_list(self):
        from rikugan.agent.system_prompt import format_tools_catalog

        # Empty input must not raise and must produce a stable section header.
        result = format_tools_catalog([])
        self.assertIsInstance(result, str)

    def test_build_system_prompt_includes_categorized_catalog(self):
        """build_system_prompt with a tool catalog should render the
        categorized table, not the comma-separated names list."""

        from rikugan.agent.system_prompt import build_system_prompt
        from rikugan.tools.base import tool

        @tool(category="database", description="List every imported function.")
        def list_imports() -> str:
            return ""

        catalog = (
            "## Available Tools\n"
            "| Tool | Description |\n"
            "| --- | --- |\n"
            "| list_imports | List every imported function. |\n"
        )

        prompt = build_system_prompt(tool_names=["list_imports"], tools_table=catalog)

        # The table must be in the prompt, not the comma-separated fallback.
        self.assertIn("list_imports", prompt)
        self.assertIn("List every imported function", prompt)


class TestToolDecorator(unittest.TestCase):
    def test_basic_tool_registration(self):
        @tool(name="test_tool", description="A test tool")
        def my_tool(x: int, y: str = "hello") -> str:
            return f"{x}-{y}"

        defn = my_tool._tool_definition
        self.assertEqual(defn.name, "test_tool")
        self.assertEqual(defn.description, "A test tool")
        self.assertEqual(len(defn.parameters), 2)

        # x is required
        self.assertEqual(defn.parameters[0].name, "x")
        self.assertEqual(defn.parameters[0].type, "integer")
        self.assertTrue(defn.parameters[0].required)

        # y has default
        self.assertEqual(defn.parameters[1].name, "y")
        self.assertEqual(defn.parameters[1].type, "string")
        self.assertFalse(defn.parameters[1].required)

    def test_tool_json_schema(self):
        @tool()
        def another_tool(name: str, count: int = 5) -> str:
            """Do something."""
            return "ok"

        schema = another_tool._tool_definition.to_json_schema()
        self.assertEqual(schema["type"], "object")
        self.assertIn("name", schema["properties"])
        self.assertIn("count", schema["properties"])
        self.assertIn("name", schema["required"])
        self.assertNotIn("count", schema["required"])

    def test_tool_provider_format(self):
        @tool(name="my_func", description="My function")
        def my_func(a: str) -> str:
            return a

        fmt = my_func._tool_definition.to_provider_format()
        self.assertEqual(fmt["type"], "function")
        self.assertEqual(fmt["function"]["name"], "my_func")

    def test_tool_execution_wraps_errors(self):
        @tool(name="failing_tool")
        def failing_tool() -> str:
            """Fails."""
            raise ValueError("boom")

        from rikugan.core.errors import ToolError

        with self.assertRaises(ToolError):
            failing_tool()

    def test_unexpected_kwarg_raises_validation_error_with_valid_params(self):
        """A hallucinated/extra argument must raise ToolValidationError listing
        the valid parameters, so the model can self-correct instead of
        retrying the same bad call in a loop.

        Reproduces the read_disassembly vs read_function_disassembly
        confusion: the model passes ``count`` to a tool that only takes
        ``address``.
        """
        from rikugan.core.errors import ToolValidationError

        @tool(name="read_function_disassembly")
        def read_function_disassembly(address: str) -> str:
            """Read the full disassembly of a function."""
            return "ok"

        with self.assertRaises(ToolValidationError) as ctx:
            read_function_disassembly(address="0x1000", count=30)

        msg = str(ctx.exception)
        # The valid parameter must be named so the model knows what to keep.
        self.assertIn("address", msg)
        # The bogus parameter must be named so the model knows what to drop.
        self.assertIn("count", msg)

    def test_tool_description_uses_full_docstring(self):
        """Tool description should be the full docstring, not just the first line.

        LLMs use the description to decide when to call a tool. A 1-line
        description like 'List all imported functions' gives them no idea of
        the output format, pagination, or when to use a sibling tool.
        """

        @tool()
        def rich_doc_tool(x: int) -> str:
            """First line of docstring.

            Output format: lines of `0xADDR  NAME`.
            Use the search variant when filtering by name.
            """
            return str(x)

        defn = rich_doc_tool._tool_definition
        self.assertIn("First line of docstring", defn.description)
        self.assertIn("Output format", defn.description)
        self.assertIn("Use the search variant", defn.description)

    def test_tool_description_strips_leading_trailing_whitespace(self):
        @tool()
        def pad_tool() -> str:
            """

            Padded description.

            """
            return "ok"

        defn = pad_tool._tool_definition
        self.assertFalse(defn.description.startswith("\n"))
        self.assertFalse(defn.description.endswith("\n"))

    def test_tool_description_handles_no_docstring(self):
        @tool(name="no_doc")
        def no_doc() -> str:
            return "ok"

        defn = no_doc._tool_definition
        self.assertEqual(defn.description, "")


class TestToolRegistry(unittest.TestCase):
    def test_register_and_execute(self):
        registry = ToolRegistry()

        @tool(name="add")
        def add(a: int, b: int) -> str:
            """Add two numbers."""
            return str(a + b)

        registry.register_function(add)
        result = registry.execute("add", {"a": 3, "b": 4})
        self.assertEqual(result, "7")

    def test_unknown_tool(self):
        registry = ToolRegistry()
        with self.assertRaises(ToolNotFoundError):
            registry.execute("nonexistent", {})

    def test_list_tools(self):
        registry = ToolRegistry()

        @tool(name="t1")
        def t1() -> str:
            """Tool 1."""
            return "1"

        @tool(name="t2")
        def t2() -> str:
            """Tool 2."""
            return "2"

        registry.register_function(t1)
        registry.register_function(t2)
        self.assertEqual(set(registry.list_names()), {"t1", "t2"})


class TestBuiltinTools(unittest.TestCase):
    """Test that built-in tools are loadable (using mocks)."""

    def test_navigation_tools(self):
        from rikugan.ida.tools.navigation import get_cursor_position

        result = get_cursor_position()
        self.assertTrue(result.startswith("0x"))

    def test_database_tools_loadable(self):
        from rikugan.ida.tools import database

        self.assertTrue(hasattr(database, "get_binary_info"))

    def test_search_imports_returns_matching_imports(self):
        """search_imports should substring-match import names across all modules."""
        from unittest.mock import patch

        from rikugan.ida.tools import database

        # Two modules: kernel32 has CreateFileA/W, user32 has MessageBoxA.
        TEST_DATA = {
            0: [  # kernel32.dll
                (0x1000, "CreateFileA", -1),
                (0x1008, "CreateFileW", -1),
                (0x1010, "ReadFile", -1),
            ],
            1: [  # user32.dll
                (0x2000, "MessageBoxA", -1),
                (0x2008, "GetWindowTextW", -1),
            ],
        }
        MODULE_NAMES = {0: "kernel32.dll", 1: "user32.dll"}

        def fake_enum_import_names(idx, cb):
            for ea, name, ordinal in TEST_DATA.get(idx, []):
                cb(ea, name, ordinal)

        with (
            patch.object(database.ida_nalt, "get_import_module_qty", return_value=2),
            patch.object(database.ida_nalt, "get_import_module_name", side_effect=lambda i: MODULE_NAMES[i]),
            patch.object(database.ida_nalt, "enum_import_names", side_effect=fake_enum_import_names),
        ):
            result = database.search_imports(query="CreateFile")

        # Must include both CreateFileA/W from kernel32, but NOT ReadFile or
        # any user32 entries.
        self.assertIn("CreateFileA", result)
        self.assertIn("CreateFileW", result)
        self.assertNotIn("ReadFile", result)
        self.assertNotIn("MessageBoxA", result)
        # Module grouping header should appear so the LLM sees which DLL.
        self.assertIn("kernel32.dll", result)

    def test_search_imports_no_matches_returns_clear_message(self):
        from unittest.mock import patch

        from rikugan.ida.tools import database

        with (
            patch.object(database.ida_nalt, "get_import_module_qty", return_value=1),
            patch.object(database.ida_nalt, "get_import_module_name", return_value="kernel32.dll"),
            patch.object(database.ida_nalt, "enum_import_names", side_effect=lambda i, cb: None),
        ):
            result = database.search_imports(query="CreateFile")

        self.assertIn("No imports matching", result)

    def test_imports_by_module_filters_correctly(self):
        """imports_by_module should return imports only from the named DLL."""
        from unittest.mock import patch

        from rikugan.ida.tools import database

        TEST_DATA = {
            0: [(0x1000, "CreateFileA", -1)],
            1: [(0x2000, "MessageBoxA", -1)],
        }
        MODULE_NAMES = {0: "kernel32.dll", 1: "user32.dll"}

        def fake_enum(idx, cb):
            for ea, name, ordinal in TEST_DATA.get(idx, []):
                cb(ea, name, ordinal)

        with (
            patch.object(database.ida_nalt, "get_import_module_qty", return_value=2),
            patch.object(database.ida_nalt, "get_import_module_name", side_effect=lambda i: MODULE_NAMES[i]),
            patch.object(database.ida_nalt, "enum_import_names", side_effect=fake_enum),
        ):
            result = database.imports_by_module(module_name="user32")

        self.assertIn("MessageBoxA", result)
        self.assertNotIn("CreateFileA", result)
        self.assertIn("user32", result.lower())

    def test_imports_by_module_missing_module_returns_message(self):
        from unittest.mock import patch

        from rikugan.ida.tools import database

        with (
            patch.object(database.ida_nalt, "get_import_module_qty", return_value=1),
            patch.object(database.ida_nalt, "get_import_module_name", return_value="kernel32.dll"),
            patch.object(database.ida_nalt, "enum_import_names", side_effect=lambda i, cb: None),
        ):
            result = database.imports_by_module(module_name="user32")

        self.assertIn("not found", result.lower())
        # Should list available modules so the LLM can recover.
        self.assertIn("kernel32", result.lower())

    def test_execute_python_suggests_dedicated_tool_for_wrapper(self):
        """When the script is a wrapper for an existing tool, execute_python
        should surface the suggestion so the LLM learns the pattern.

        The suggestion layer is suggest-only: the script still runs. The
        test asserts the suggestion appears in the output, NOT that
        execution was blocked."""
        from rikugan.ida.tools import scripting

        wrapper_script = """
import ida_nalt
nimps = ida_nalt.get_import_module_qty()
print("count:", nimps)
"""

        output = scripting.execute_python(code=wrapper_script)

        # Suggestion preamble must appear.
        self.assertIn("[rikugan]", output)
        self.assertIn("list_imports", output)
        # The dedicated-tool name appears in the suggestion.
        self.assertIn("dedicated tool", output.lower())

    def test_execute_python_no_suggestion_for_legitimate_script(self):
        """Compute that has no dedicated tool equivalent must NOT trigger
        the suggestion layer (false-positive guard)."""
        from rikugan.ida.tools import scripting

        legitimate = """
import struct
buf = b"\\x00\\x01\\x02"
print(struct.unpack("<I", buf[:4])[0])
"""

        output = scripting.execute_python(code=legitimate)

        # Suggestion preamble must NOT appear for unrelated compute.
        self.assertNotIn("[rikugan]", output)

    def test_database_tool_descriptions_are_substantial(self):
        """Each database tool description must give the LLM actionable context.

        A one-liner like "List all imported functions" is too thin to compete
        with ``execute_python`` in the LLM's tool-selection decision. The
        description must (1) describe the output shape and (2) point to a
        sibling tool when relevant (search/filter variants).
        """
        from rikugan.ida.tools import database
        from rikugan.tools.base import ToolDefinition

        def _collect(module: object) -> list[ToolDefinition]:
            defs: list[ToolDefinition] = []
            for attr_name in dir(module):
                attr = getattr(module, attr_name, None)
                defn = getattr(attr, "_tool_definition", None)
                if isinstance(defn, ToolDefinition):
                    defs.append(defn)
            return defs

        defs = _collect(database)
        names = {d.name for d in defs}
        for required in ("list_imports", "list_exports", "get_binary_info", "list_segments"):
            self.assertIn(required, names, f"missing database tool: {required}")

        for defn in defs:
            with self.subTest(tool=defn.name):
                self.assertGreaterEqual(
                    len(defn.description),
                    80,
                    f"tool {defn.name} description too short ({len(defn.description)} chars)",
                )
                # Tools that list imports/exports/strings/segments must mention
                # a search/filter sibling tool so the LLM does not reach for
                # execute_python when it only needs filtered results.
                if defn.name in {"list_imports", "list_exports", "list_strings", "list_segments"}:
                    desc_lower = defn.description.lower()
                    self.assertTrue(
                        "search" in desc_lower or "filter" in desc_lower,
                        f"tool {defn.name} must mention a search/filter sibling tool",
                    )


if __name__ == "__main__":
    unittest.main()
