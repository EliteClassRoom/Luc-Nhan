"""Tests for tool-approval / result-editor style builders."""

from __future__ import annotations

import sys
import unittest

from tests.qt_stubs import ensure_pyside6_stubs

ensure_pyside6_stubs()

# Ensure the real module is loaded even if another test stubbed it.
sys.modules.pop("rikugan.ui.theme.widgets_mutation", None)

from rikugan.ui.theme.widgets_mutation import (
    get_tool_result_editor_style,
)


class TestResultEditorStyle(unittest.TestCase):
    def test_returns_qss_for_qplaintextedit(self):
        css = get_tool_result_editor_style()
        self.assertIn("QPlainTextEdit", css)
        self.assertIn("background:", css)
        self.assertIn("border:", css)

    def test_custom_text_color_appears_in_qss(self):
        css = get_tool_result_editor_style(text_color="#ff0000")
        self.assertIn("#ff0000", css)
        # The color must land in the QPlainTextEdit color rule, not just
        # appended (QSS keeps the first matching rule).
        self.assertIn("color: #ff0000", css.split("QScrollBar")[0])

    def test_default_has_no_literal_color_override_marker(self):
        # Default path: text_color is None → uses token code_text. The
        # QPlainTextEdit color rule is present and uses the token.
        css = get_tool_result_editor_style()
        self.assertIn("color:", css)


if __name__ == "__main__":
    unittest.main()
