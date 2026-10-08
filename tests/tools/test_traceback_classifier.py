"""Tests for traceback classification — pure function, no IDA deps."""

from __future__ import annotations

import dataclasses
import unittest

from lucnhan.tools.traceback_classifier import (
    TracebackClassification,
    classify_traceback,
)


class TestClassifyTraceback(unittest.TestCase):
    def test_attribute_error_is_api_shaped(self):
        tb = (
            "Traceback (most recent call last):\n"
            '  File "<string>", line 1, in <module>\n'
            "AttributeError: module 'idaapi' has no attribute 'get_operands'\n"
        )
        result = classify_traceback(tb)
        self.assertTrue(result.is_api_shaped)
        self.assertEqual(result.exception_type, "AttributeError")
        self.assertIn("get_operands", result.exception_message)

    def test_import_error_is_api_shaped(self):
        tb = (
            "Traceback (most recent call last):\n"
            '  File "<string>", line 1, in <module>\n'
            "ImportError: No module named ida_struct\n"
        )
        result = classify_traceback(tb)
        self.assertTrue(result.is_api_shaped)
        self.assertEqual(result.exception_type, "ImportError")

    def test_module_not_found_error_is_api_shaped(self):
        tb = (
            "Traceback (most recent call last):\n"
            '  File "<string>", line 1, in <module>\n'
            "ModuleNotFoundError: No module named 'ida_nonexistent'\n"
        )
        result = classify_traceback(tb)
        self.assertTrue(result.is_api_shaped)
        self.assertEqual(result.exception_type, "ModuleNotFoundError")

    def test_name_error_is_api_shaped(self):
        tb = (
            "Traceback (most recent call last):\n"
            '  File "<string>", line 1, in <module>\n'
            "NameError: name 'BADADDR' is not defined\n"
        )
        result = classify_traceback(tb)
        self.assertTrue(result.is_api_shaped)
        self.assertEqual(result.exception_type, "NameError")

    def test_value_error_is_not_api_shaped(self):
        tb = (
            "Traceback (most recent call last):\n"
            '  File "<string>", line 1, in <module>\n'
            "ValueError: invalid literal for int() with base 16: 'xyz'\n"
        )
        result = classify_traceback(tb)
        self.assertFalse(result.is_api_shaped)

    def test_type_error_is_not_api_shaped(self):
        tb = (
            "Traceback (most recent call last):\n"
            '  File "<string>", line 1, in <module>\n'
            "TypeError: unsupported operand type(s) for +: 'int' and 'str'\n"
        )
        result = classify_traceback(tb)
        self.assertFalse(result.is_api_shaped)

    def test_empty_traceback_returns_not_api_shaped(self):
        result = classify_traceback("")
        self.assertFalse(result.is_api_shaped)
        self.assertEqual(result.exception_type, "")

    def test_extract_modules_from_imports(self):
        code = "import ida_bytes\nimport ida_funcs\nprint(1)\n"
        result = classify_traceback("NameError: x", code)
        self.assertIn("ida_bytes", result.modules_referenced)
        self.assertIn("ida_funcs", result.modules_referenced)

    def test_extract_modules_from_from_imports(self):
        code = "from ida_hexrays import decompile\nfrom idautils import Functions\n"
        result = classify_traceback("NameError: x", code)
        self.assertIn("ida_hexrays", result.modules_referenced)
        self.assertIn("idautils", result.modules_referenced)

    def test_extract_modules_bare_idautils_idc_idaapi(self):
        code = "import idautils\nimport idc\nimport idaapi\n"
        result = classify_traceback("NameError: x", code)
        self.assertIn("idautils", result.modules_referenced)
        self.assertIn("idc", result.modules_referenced)
        self.assertIn("idaapi", result.modules_referenced)

    def test_extract_modules_no_ida_modules(self):
        code = "import json\nimport struct\nprint(1)\n"
        result = classify_traceback("NameError: x", code)
        self.assertEqual(result.modules_referenced, ())

    def test_extract_modules_syntax_error_returns_empty(self):
        code = "def broken(:\n"
        result = classify_traceback("NameError: x", code)
        self.assertEqual(result.modules_referenced, ())

    def test_exception_message_extracted(self):
        tb = "AttributeError: module 'idaapi' has no attribute 'foo'\n"
        result = classify_traceback(tb)
        self.assertEqual(
            result.exception_message,
            "module 'idaapi' has no attribute 'foo'",
        )

    def test_no_code_returns_empty_modules(self):
        result = classify_traceback("NameError: x")
        self.assertEqual(result.modules_referenced, ())

    def test_returns_frozen_dataclass(self):
        result = classify_traceback("NameError: x")
        self.assertIsInstance(result, TracebackClassification)
        # Frozen dataclass — mutation should raise
        with self.assertRaises((AttributeError, dataclasses.FrozenInstanceError)):
            result.is_api_shaped = True  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
