"""Tests for tool argument coercion in ToolRegistry."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.tools.base import ParameterSchema, ToolDefinition  # noqa: E402
from rikugan.tools.coercion import coerce_bool  # noqa: E402
from rikugan.tools.registry import ToolRegistry  # noqa: E402


def _make_defn(params: list[ParameterSchema]) -> ToolDefinition:
    """Helper to create a ToolDefinition with given params."""
    return ToolDefinition(
        name="test_tool",
        description="test",
        parameters=params,
        handler=lambda **kw: str(kw),
    )


class TestCoerceArguments(unittest.TestCase):
    """Tests for ToolRegistry._coerce_arguments()."""

    def test_string_to_int(self):
        defn = _make_defn([ParameterSchema(name="count", type="integer")])
        result = ToolRegistry._coerce_arguments(defn, {"count": "30"})
        self.assertEqual(result["count"], 30)
        self.assertIsInstance(result["count"], int)

    def test_float_string_to_int(self):
        defn = _make_defn([ParameterSchema(name="count", type="integer")])
        result = ToolRegistry._coerce_arguments(defn, {"count": "30.0"})
        self.assertEqual(result["count"], 30)

    def test_hex_string_to_int(self):
        defn = _make_defn([ParameterSchema(name="count", type="integer")])
        result = ToolRegistry._coerce_arguments(defn, {"count": "0x47"})
        self.assertEqual(result["count"], 71)

    def test_bool_true_to_int(self):
        """bool is a subclass of int — should be coerced to plain int."""
        defn = _make_defn([ParameterSchema(name="count", type="integer")])
        result = ToolRegistry._coerce_arguments(defn, {"count": True})
        self.assertEqual(result["count"], 1)
        # Verify it's a plain int, not a bool
        self.assertIs(type(result["count"]), int)

    def test_bool_false_to_int(self):
        defn = _make_defn([ParameterSchema(name="count", type="integer")])
        result = ToolRegistry._coerce_arguments(defn, {"count": False})
        self.assertEqual(result["count"], 0)
        self.assertIs(type(result["count"]), int)

    def test_int_to_bool(self):
        """Integers should coerce to bool via coerce_bool helper."""
        defn = _make_defn([ParameterSchema(name="flag", type="boolean")])
        for val, expected in ((0, False), (1, True), (42, True), (-1, True)):
            with self.subTest(value=val):
                result = ToolRegistry._coerce_arguments(defn, {"flag": val})
                if expected:
                    self.assertTrue(result["flag"], f"Expected {val!r} → True")
                else:
                    self.assertFalse(result["flag"], f"Expected {val!r} → False")
                self.assertIsInstance(result["flag"], bool)

    def test_string_truthy_to_bool(self):
        """Truthy string values should coerce to True for boolean params."""
        defn = _make_defn([ParameterSchema(name="flag", type="boolean")])
        for val in ("true", "yes", "y", "on", "1"):
            with self.subTest(value=val):
                result = ToolRegistry._coerce_arguments(defn, {"flag": val})
                self.assertTrue(result["flag"], f"Expected {val!r} → True")

    def test_string_falsy_to_bool(self):
        """Falsy string values should coerce to False for boolean params."""
        defn = _make_defn([ParameterSchema(name="flag", type="boolean")])
        for val in ("false", "no", "n", "off", "0", ""):
            with self.subTest(value=val):
                result = ToolRegistry._coerce_arguments(defn, {"flag": val})
                self.assertFalse(result["flag"], f"Expected {val!r} → False")

    def test_string_whitespace_variants_to_bool(self):
        """Whitespace-surrounded truthy/falsy strings should coerce correctly."""
        defn = _make_defn([ParameterSchema(name="flag", type="boolean")])
        for truthy in (" true ", " yes ", " on ", " TRUE ", " YES "):
            result = ToolRegistry._coerce_arguments(defn, {"flag": truthy})
            self.assertTrue(result["flag"], f"Expected {truthy!r} → True")
        for falsy in (" false ", " off ", " FALSE ", " no"):
            result = ToolRegistry._coerce_arguments(defn, {"flag": falsy})
            self.assertFalse(result["flag"], f"Expected {falsy!r} → False")

    def test_int_to_string(self):
        defn = _make_defn([ParameterSchema(name="name", type="string")])
        result = ToolRegistry._coerce_arguments(defn, {"name": 42})
        self.assertEqual(result["name"], "42")

    def test_string_to_number(self):
        defn = _make_defn([ParameterSchema(name="ratio", type="number")])
        result = ToolRegistry._coerce_arguments(defn, {"ratio": "3.14"})
        self.assertAlmostEqual(result["ratio"], 3.14)

    def test_native_types_unchanged(self):
        """Values already matching their schema type should pass through."""
        defn = _make_defn(
            [
                ParameterSchema(name="count", type="integer"),
                ParameterSchema(name="flag", type="boolean"),
                ParameterSchema(name="name", type="string"),
            ]
        )
        result = ToolRegistry._coerce_arguments(
            defn,
            {
                "count": 42,
                "flag": True,
                "name": "hello",
            },
        )
        self.assertEqual(result["count"], 42)
        self.assertTrue(result["flag"])
        self.assertEqual(result["name"], "hello")

    def test_unknown_param_ignored(self):
        defn = _make_defn([ParameterSchema(name="count", type="integer")])
        result = ToolRegistry._coerce_arguments(defn, {"count": 5, "extra": "ignored"})
        self.assertEqual(result["count"], 5)
        self.assertEqual(result["extra"], "ignored")

    def test_empty_arguments(self):
        defn = _make_defn([ParameterSchema(name="x", type="integer")])
        result = ToolRegistry._coerce_arguments(defn, {})
        self.assertEqual(result, {})

    def test_invalid_value_passes_through(self):
        """Unparseable values should pass through for the handler to reject."""
        defn = _make_defn([ParameterSchema(name="count", type="integer")])
        result = ToolRegistry._coerce_arguments(defn, {"count": "not_a_number"})
        self.assertEqual(result["count"], "not_a_number")

    def test_coerce_arguments_returns_fresh_dict(self):
        """_coerce_arguments and coerce_arguments_for must always return a
        dict distinct from the input, even for empty arguments or tools with
        no parameters.
        """
        defn = _make_defn([ParameterSchema(name="x", type="integer")])
        for label, args in (
            ("empty", {}),
            ("non-empty", {"x": "5"}),
            ("unknown params", {"y": "z", "x": "3"}),
        ):
            with self.subTest(case=label):
                result = ToolRegistry._coerce_arguments(defn, args)
                self.assertIsNot(result, args, f"Must return a fresh dict for {label}")

        # coerce_arguments_for() with empty arguments
        registry = ToolRegistry()
        registry.register(defn)
        for label, args in (("empty", {}), ("non-empty", {"x": "5"})):
            with self.subTest(case=f"coerce_arguments_for {label}"):
                result = registry.coerce_arguments_for("test_tool", args)
                self.assertIsNot(result, args, f"coerce_arguments_for must return a fresh dict for {label}")

        # Tool with no parameters returning a fresh dict for empty arguments
        no_param_defn = _make_defn([])
        self.assertIsNot(
            ToolRegistry._coerce_arguments(no_param_defn, {}),
            {},
            "Must return a fresh dict for no-parameter tool with empty args",
        )

        # Registered no-parameter tool: coerce_arguments_for() must return a
        # fresh dict distinct from the original input.
        no_param_args: dict[str, str] = {}
        registry.register(
            ToolDefinition(
                name="no_param_tool",
                description="A tool with no parameters",
                parameters=[],
                handler=lambda: "ok",
            )
        )
        no_param_result = registry.coerce_arguments_for("no_param_tool", no_param_args)
        self.assertEqual(no_param_result, {})
        self.assertIsNot(
            no_param_result,
            no_param_args,
            "coerce_arguments_for must return a fresh dict for no-parameter registered tool",
        )


class TestCoerceBool(unittest.TestCase):
    """Consolidated tests for the shared coerce_bool() helper."""

    def test_coerce_bool_helper_matches_registry_cases(self):
        """Exercise all coerce_bool categories through sub-tests."""
        # --- truthy strings ---
        truthy_strings = ("true", "1", "yes", "y", "on")
        for val in truthy_strings:
            with self.subTest(category="truthy string", value=val):
                self.assertTrue(coerce_bool(val))

        # --- truthy whitespace ---
        for val in (" true ", " yes ", " on ", "\tyes\t", "\n1\n"):
            with self.subTest(category="truthy whitespace", value=val):
                self.assertTrue(coerce_bool(val))

        # --- falsy strings ---
        falsy_strings = ("false", "0", "no", "n", "off", "")
        for val in falsy_strings:
            with self.subTest(category="falsy string", value=val):
                self.assertFalse(coerce_bool(val))

        # --- falsy whitespace ---
        for val in (" false ", " off ", "\tno\t", "\n0\n"):
            with self.subTest(category="falsy whitespace", value=val):
                self.assertFalse(coerce_bool(val))

        # --- numeric & None ---
        for val, default, expected in (
            (0, False, False),
            (1, False, True),
            (-1, False, True),
            (42, False, True),
            (None, False, False),
            (None, True, True),
        ):
            with self.subTest(category="numeric/none", value=val, default=default):
                self.assertEqual(coerce_bool(val, default=default), expected)

        # --- bool passthrough ---
        for val, expected in ((True, True), (False, False)):
            with self.subTest(category="bool passthrough", value=val):
                self.assertEqual(coerce_bool(val), expected)

        # --- unknown types ---
        for val, default, expected in (
            ([1, 2, 3], False, False),
            ({"a": 1}, False, False),
            ({"a": 1}, True, True),
        ):
            with self.subTest(category="unknown type", value=repr(val), default=default):
                self.assertEqual(coerce_bool(val, default=default), expected)


if __name__ == "__main__":
    unittest.main()
