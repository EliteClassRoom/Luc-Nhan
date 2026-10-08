"""Tests for lucnhan.core.dependencies optional-package detection."""

from __future__ import annotations

import importlib.util
import unittest
from unittest.mock import patch

from lucnhan.core.dependencies import (
    get_missing_dependency_warnings,
    get_optional_dependency_statuses,
)


class TestDependencies(unittest.TestCase):
    def test_statuses_include_known_optional_packages(self) -> None:
        statuses = get_optional_dependency_statuses()
        keys = {status.key for status in statuses}
        self.assertIn("anthropic", keys)
        self.assertIn("openai", keys)
        self.assertIn("gemini", keys)
        self.assertIn("mcp", keys)

    def test_missing_dependency_warnings_are_human_readable(self) -> None:
        def fake_find_spec(name: str):
            if name == "openai":
                return None
            return object()

        with patch.object(importlib.util, "find_spec", side_effect=fake_find_spec):
            warnings = get_missing_dependency_warnings()

        self.assertTrue(any("openai" in warning.lower() for warning in warnings))

    def test_dependency_status_has_warning_property(self) -> None:
        statuses = get_optional_dependency_statuses()
        for status in statuses:
            warning = status.warning
            self.assertIsInstance(warning, str)
            self.assertIn(status.package_name, warning)

    def test_module_available_handles_exceptions(self) -> None:
        """_module_available must not raise on import-time errors."""
        from lucnhan.core import dependencies

        # Should not raise even with a weird import error
        result = dependencies._module_available("definitely_not_a_real_module_xyz")
        self.assertIsInstance(result, bool)


if __name__ == "__main__":
    unittest.main()
