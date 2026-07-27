"""Integration tests for the tool registry with all built-in tools.

Tests that the default registry loads correctly, all tools have valid
schemas, and tools execute through the registry dispatch path.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

# Reload tool modules so they pick up real stub base classes (optinsn_t,
# Hexrays_Hooks, etc.) instead of MagicMock, which would leak fake
# _tool_definition attributes into the registry.
import importlib

import rikugan.ida.tools.database as _db_mod
import rikugan.ida.tools.microcode as _mc_mod
import rikugan.ida.tools.microcode_optim as _mco_mod

importlib.reload(_mco_mod)
importlib.reload(_mc_mod)
importlib.reload(_db_mod)

from rikugan.ida.tools.registry import create_default_registry
from rikugan.tools.registry import ToolRegistry


class TestDefaultRegistryCreation(unittest.TestCase):
    """Test that all built-in tool modules register successfully."""

    def setUp(self):
        self.registry = create_default_registry()

    def test_has_tools(self):
        tools = self.registry.list_names()
        self.assertTrue(len(tools) > 0)

    def test_minimum_tool_count(self):
        """Registry should have at least 20 tools across all modules."""
        tools = self.registry.list_names()
        self.assertGreaterEqual(len(tools), 20)

    def test_all_tools_have_descriptions(self):
        for defn in self.registry.list_tools():
            self.assertTrue(
                defn.description,
                f"Tool {defn.name} missing description",
            )

    def test_all_tools_have_handlers(self):
        for defn in self.registry.list_tools():
            self.assertIsNotNone(
                defn.handler,
                f"Tool {defn.name} missing handler",
            )

    def test_all_tools_have_valid_schemas(self):
        """Every tool must produce a valid JSON Schema dict."""
        for defn in self.registry.list_tools():
            schema = defn.to_json_schema()
            self.assertEqual(schema["type"], "object", f"{defn.name} schema type")
            self.assertIn("properties", schema, f"{defn.name} missing properties")

    def test_provider_format_all_tools(self):
        """Every tool must produce valid provider format for the LLM."""
        formats = self.registry.to_provider_format()
        fmt_names = {fmt["function"]["name"] for fmt in formats}
        all_names = set(self.registry.list_names())
        # Verify that the format includes a reasonable subset of registered tools.
        # Some internal/microcode/decompiler/web tools are intentionally excluded.
        self.assertGreater(len(all_names), len(fmt_names), "Should exclude some tools")
        self.assertGreater(len(all_names), 0)
        self.assertGreater(len(formats), 0)
        self.assertIn("list_functions", fmt_names, "basic tools must be present")
        self.assertIn("rename_function", fmt_names, "basic tools must be present")
        for fmt in formats:
            self.assertEqual(fmt["type"], "function")
            self.assertIn("name", fmt["function"])
            self.assertIn("description", fmt["function"])
            self.assertIn("parameters", fmt["function"])


class TestRegistryCategories(unittest.TestCase):
    """Test that expected tool categories are present."""

    def setUp(self):
        self.registry = create_default_registry()

    def test_navigation_tools(self):
        names = self.registry.list_names()
        self.assertIn("get_cursor_position", names)
        self.assertIn("get_current_function", names)

    def test_function_tools(self):
        names = self.registry.list_names()
        self.assertIn("list_functions", names)
        self.assertIn("search_functions", names)
        self.assertIn("get_function_info", names)

    def test_database_tools(self):
        names = self.registry.list_names()
        self.assertIn("get_binary_info", names)
        self.assertIn("list_segments", names)

    def test_string_tools(self):
        names = self.registry.list_names()
        self.assertIn("list_strings", names)
        self.assertIn("search_strings", names)

    def test_annotation_tools(self):
        names = self.registry.list_names()
        self.assertIn("rename_function", names)
        self.assertIn("set_comment", names)

    def test_xref_tools(self):
        names = self.registry.list_names()
        self.assertIn("xrefs_to", names)
        self.assertIn("xrefs_from", names)


class TestRegistryExecution(unittest.TestCase):
    """Test tool execution through the registry dispatch path."""

    def setUp(self):
        self.registry = create_default_registry()

    def test_execute_list_functions(self):
        result = self.registry.execute("list_functions", {"offset": 0, "limit": 10})
        self.assertIn("Functions", result)

    def test_execute_get_binary_info(self):
        result = self.registry.execute("get_binary_info", {})
        # File name is intentionally hidden from the LLM-facing output.
        self.assertNotIn("test_binary", result)
        self.assertIn("Processor:", result)

    def test_execute_search_functions(self):
        result = self.registry.execute("search_functions", {"query": "sub"})
        self.assertIn("sub_1000", result)

    def test_execute_unknown_tool_raises(self):
        from rikugan.core.errors import ToolNotFoundError
        with self.assertRaises(ToolNotFoundError):
            self.registry.execute("nonexistent_tool_xyz", {})

    def test_execute_wrong_args_raises(self):
        from rikugan.core.errors import ToolError
        with self.assertRaises(ToolError):
            # list_functions expects int for offset — @tool wraps TypeError as ToolError
            self.registry.execute("list_functions", {"offset": "not_an_int"})


class TestRegistryResultFormatting(unittest.TestCase):
    """Test result formatting and truncation."""

    def test_none_becomes_ok(self):
        self.assertEqual(ToolRegistry._format_result(None), "OK")

    def test_string_passthrough(self):
        self.assertEqual(ToolRegistry._format_result("hello"), "hello")

    def test_dict_becomes_json(self):
        result = ToolRegistry._format_result({"key": "val"})
        self.assertIn('"key"', result)
        self.assertIn('"val"', result)

    def test_dict_uses_compact_separators(self):
        """Phase 1.1 — tool results use compact JSON (no whitespace)."""
        result = ToolRegistry._format_result({"key": "val"})
        # Compact form: no leading/trailing whitespace, no spaces after
        # separators. Pretty-printed would contain ``: `` and ``\n``.
        self.assertNotIn(": ", result)
        self.assertNotIn("\n", result)
        self.assertIn('"key":"val"', result)

    def test_list_uses_compact_separators(self):
        result = ToolRegistry._format_result([1, 2, 3])
        self.assertNotIn("\n", result)
        self.assertEqual(result, "[1,2,3]")

    def test_list_becomes_json(self):
        result = ToolRegistry._format_result([1, 2, 3])
        self.assertIn("1", result)

    def test_other_types_become_str(self):
        result = ToolRegistry._format_result(42)
        self.assertEqual(result, "42")


class TestToolsCatalogCache(unittest.TestCase):
    """Phase 2.1 — the tools catalog is cached and invalidated correctly."""

    def setUp(self):
        from rikugan.tools.base import tool, ToolDefinition

        @tool(category="test")
        def example_tool(name: str = "x") -> str:
            """An example tool for testing the catalog cache."""
            return name

        self.registry = ToolRegistry()
        self.registry.register(example_tool._tool_definition)

    def test_first_call_builds(self):
        catalog = self.registry.tools_catalog()
        self.assertIn("example_tool", catalog)

    def test_second_call_returns_cached_string(self):
        first = self.registry.tools_catalog()
        second = self.registry.tools_catalog()
        # Same Python object (no rebuild)
        self.assertIs(first, second)

    def test_register_invalidates_cache(self):
        first = self.registry.tools_catalog()
        from rikugan.tools.base import tool

        @tool(category="test")
        def another_tool(value: int = 1) -> str:
            """Another tool to force catalog rebuild."""
            return str(value)

        self.registry.register(another_tool._tool_definition)
        rebuilt = self.registry.tools_catalog()
        self.assertIsNot(first, rebuilt)
        self.assertIn("another_tool", rebuilt)

    def test_set_capabilities_invalidates_cache(self):
        first = self.registry.tools_catalog()
        # Re-setting capabilities (even with no changes) should
        # invalidate the catalog cache since available tools may change.
        self.registry.set_capabilities({})
        rebuilt = self.registry.tools_catalog()
        self.assertIsNot(first, rebuilt)


class TestExecuteCoerced(unittest.TestCase):
    """Phase 2.3 — execute_coerced skips redundant argument coercion."""

    def setUp(self):
        from rikugan.tools.base import tool

        call_count = {"n": 0}

        @tool(category="test")
        def my_tool(count: int = 0) -> str:
            """Tool that records invocation count."""
            call_count["n"] += 1
            return f"called-{count}"

        self.registry = ToolRegistry()
        self.registry.register(my_tool._tool_definition)
        self.call_count = call_count

    def test_execute_coerced_runs_handler(self):
        result = self.registry.execute_coerced("my_tool", {"count": 5})
        self.assertEqual(result, "called-5")
        self.assertEqual(self.call_count["n"], 1)

    def test_execute_coerced_does_not_re_coerce(self):
        """The handler receives the args as-is; no second coercion pass."""
        # Pass already-coerced int — execute_coerced must NOT touch it
        # (no second coercion walk over parameter schema).
        self.registry.execute_coerced("my_tool", {"count": 7})
        self.assertEqual(self.call_count["n"], 1)

    def test_execute_coerced_unknown_tool_raises(self):
        from rikugan.core.errors import ToolNotFoundError
        with self.assertRaises(ToolNotFoundError):
            self.registry.execute_coerced("does_not_exist", {})

    def test_execute_coerced_caches_result(self):
        # ``my_tool`` is not in CACHEABLE_TOOLS, so cache.put is a no-op
        # by design. Verify the handler is still invoked once per call,
        # i.e. the cache miss path is not silently swallowing results.
        first = self.registry.execute_coerced("my_tool", {"count": 3})
        second = self.registry.execute_coerced("my_tool", {"count": 3})
        self.assertEqual(first, second)
        # Non-cacheable tool: handler runs every time.
        self.assertEqual(self.call_count["n"], 2)

    def test_execute_coerced_cacheable_tool_hits_cache(self):
        """When the tool is in CACHEABLE_TOOLS, repeated calls hit cache."""
        from rikugan.tools import cache as cache_mod

        # Force ``my_tool`` into the cacheable set for this test only.
        original = cache_mod.CACHEABLE_TOOLS
        cache_mod.CACHEABLE_TOOLS = original | {"my_tool"}
        try:
            first = self.registry.execute_coerced("my_tool", {"count": 9})
            second = self.registry.execute_coerced("my_tool", {"count": 9})
            self.assertEqual(first, second)
            self.assertEqual(self.call_count["n"], 1)
        finally:
            cache_mod.CACHEABLE_TOOLS = original


class TestCacheableToolsExpansion(unittest.TestCase):
    """Phase 2.2 — read-only tool result cache expanded safely."""

    def test_xrefs_and_function_info_are_cacheable(self):
        from rikugan.tools.cache import CACHEABLE_TOOLS

        self.assertIn("xrefs_to", CACHEABLE_TOOLS)
        self.assertIn("xrefs_from", CACHEABLE_TOOLS)
        self.assertIn("get_function_info", CACHEABLE_TOOLS)
        self.assertIn("get_function_name", CACHEABLE_TOOLS)

    def test_strings_tools_still_excluded(self):
        """Phase 2.2 must NOT add list_strings/search_strings (see cache.py)."""
        from rikugan.tools.cache import CACHEABLE_TOOLS

        self.assertNotIn("list_strings", CACHEABLE_TOOLS)
        self.assertNotIn("search_strings", CACHEABLE_TOOLS)


if __name__ == "__main__":
    unittest.main()
