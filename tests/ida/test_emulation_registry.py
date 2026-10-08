"""Regression: the emulation tools must register via the advanced path
without importing the Unicorn SDK at module load and must always be
advertised in the schema even when the runtime dependency is missing.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()


class TestEmulationAdvancedRegistration(unittest.TestCase):
    def setUp(self) -> None:
        # Reset cached module states so ``register_advanced_tools`` runs
        # fresh on each invocation.
        if "lucnhan.ida.tools.emulation" in sys.modules:
            del sys.modules["lucnhan.ida.tools.emulation"]
        self.assertNotIn("lucnhan.ida.tools.emulation", sys.modules)
        # Force the advanced registry cache to reload every module name.
        from lucnhan.ida.tools import registry as ida_registry

        ida_registry.reset_failed_advanced_modules()

    def _fresh_registry(self):
        from lucnhan.ida.tools.registry import create_default_registry, register_advanced_tools

        registry = create_default_registry()
        register_advanced_tools(registry)
        return registry

    def test_both_tools_register_advanced(self) -> None:
        registry = self._fresh_registry()
        names = registry.list_names()
        self.assertIn("emulate_code", names)
        self.assertIn("resolve_emulated_string", names)

    def test_both_tools_are_non_mutating(self) -> None:
        registry = self._fresh_registry()
        for tool_name in ("emulate_code", "resolve_emulated_string"):
            defn = registry.get(tool_name)
            self.assertIsNotNone(defn, f"{tool_name} is registered")
            self.assertFalse(defn.mutating, f"{tool_name} must be mutating=False")
            self.assertEqual(defn.category, "emulation", f"{tool_name} category")
            self.assertEqual(defn.timeout, 30.0, f"{tool_name} timeout override")

    def test_schema_lists_tools_even_when_unicorn_is_hidden(self) -> None:
        """If Unicorn is missing the schema still advertises both tools."""
        from lucnhan.ida.tools.registry import create_default_registry, register_advanced_tools

        # Mock a missing Unicorn SDK.
        original_unicorn = sys.modules.pop("unicorn", None)
        sys.modules["unicorn"] = None  # forces ``import unicorn`` to fail
        try:
            registry = create_default_registry()
            register_advanced_tools(registry)
            names = registry.list_names()
            self.assertIn("emulate_code", names)
            self.assertIn("resolve_emulated_string", names)
        finally:
            if original_unicorn is not None:
                sys.modules["unicorn"] = original_unicorn
            else:
                sys.modules.pop("unicorn", None)
