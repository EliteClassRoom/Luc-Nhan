"""Tests for lucnhan.ida.tools.annotations — argument validation (finding F2)."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from lucnhan.core.errors import ToolValidationError
from lucnhan.ida.tools import annotations as ann


class TestAnnotationValidation(unittest.TestCase):
    """LLM-supplied names/comments must be non-empty at the tool boundary."""

    def _expect_validation_error(self, callable, **kwargs):
        with self.assertRaises(ToolValidationError) as ctx:
            callable(**kwargs)
        return str(ctx.exception)

    def test_rename_function_rejects_empty_new_name(self):
        msg = self._expect_validation_error(ann.rename_function, address="0x1000", new_name="")
        self.assertIn("new_name", msg)

    def test_rename_function_rejects_blank_new_name(self):
        msg = self._expect_validation_error(ann.rename_function, address="0x1000", new_name="   ")
        self.assertIn("new_name", msg)

    def test_rename_address_rejects_empty_new_name(self):
        msg = self._expect_validation_error(ann.rename_address, address="0x1000", new_name="")
        self.assertIn("new_name", msg)

    def test_set_comment_rejects_empty_comment(self):
        msg = self._expect_validation_error(ann.set_comment, address="0x1000", comment="")
        self.assertIn("comment", msg)

    def test_set_function_comment_rejects_empty_comment(self):
        msg = self._expect_validation_error(ann.set_function_comment, address="0x1000", comment=" ")
        self.assertIn("comment", msg)

    def test_set_type_rejects_empty_type_string(self):
        msg = self._expect_validation_error(ann.set_type, address="0x1000", type_string="")
        self.assertIn("type_string", msg)

    def test_rename_variable_rejects_empty_new_name(self):
        msg = self._expect_validation_error(
            ann.rename_variable,
            func_address="0x1000",
            old_name="v",
            new_name="",
        )
        self.assertIn("new_name", msg)


if __name__ == "__main__":
    unittest.main()
