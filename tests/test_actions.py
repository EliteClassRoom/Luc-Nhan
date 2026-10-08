"""Tests for UI action handler functions."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

# The _handle_* functions are defined inside `if _HAS_IDA:` block,
# so they exist in the module namespace after import with IDA mocks.
import lucnhan.ida.ui.actions as actions_mod


class TestActionHandlers(unittest.TestCase):
    """Test that action handler functions produce correct prompts."""

    def _ctx(self, **overrides):
        """Build a context dict with defaults."""
        ctx = {
            "ea": 0x401000,
            "func_ea": 0x401000,
            "func_name": "process_data",
            "selected_text": "",
        }
        ctx.update(overrides)
        return ctx

    def test_send_to_with_selection(self):
        handler = getattr(actions_mod, "_handle_send_to", None)
        if handler is None:
            self.skipTest("_handle_send_to not available (no IDA mock)")
        result = handler(self._ctx(selected_text="mov rax, rbx"))
        self.assertEqual(result, "mov rax, rbx")

    def test_send_to_with_function(self):
        handler = getattr(actions_mod, "_handle_send_to", None)
        if handler is None:
            self.skipTest("_handle_send_to not available")
        result = handler(self._ctx())
        self.assertIn("process_data", result)
        self.assertIn("0x401000", result)

    def test_send_to_without_function(self):
        handler = getattr(actions_mod, "_handle_send_to", None)
        if handler is None:
            self.skipTest("_handle_send_to not available")
        result = handler(self._ctx(func_name=None))
        self.assertIn("0x401000", result)
        self.assertNotIn("None", result)

    def test_explain(self):
        handler = getattr(actions_mod, "_handle_explain", None)
        if handler is None:
            self.skipTest("_handle_explain not available")
        result = handler(self._ctx())
        self.assertIn("Explain", result)
        self.assertIn("process_data", result)
        self.assertIn("0x401000", result)

    def test_rename(self):
        handler = getattr(actions_mod, "_handle_rename", None)
        if handler is None:
            self.skipTest("_handle_rename not available")
        result = handler(self._ctx())
        self.assertIn("rename", result.lower())
        self.assertIn("process_data", result)

    def test_deobfuscate(self):
        handler = getattr(actions_mod, "_handle_deobfuscate", None)
        if handler is None:
            self.skipTest("_handle_deobfuscate not available")
        result = handler(self._ctx())
        self.assertIn("obfusc", result.lower())
        self.assertIn("process_data", result)

    def test_vuln_audit(self):
        handler = getattr(actions_mod, "_handle_vuln_audit", None)
        if handler is None:
            self.skipTest("_handle_vuln_audit not available")
        result = handler(self._ctx())
        self.assertIn("vulnerabilit", result.lower())
        self.assertIn("process_data", result)

    def test_suggest_types(self):
        handler = getattr(actions_mod, "_handle_suggest_types", None)
        if handler is None:
            self.skipTest("_handle_suggest_types not available")
        result = handler(self._ctx())
        self.assertIn("type", result.lower())
        self.assertIn("process_data", result)

    def test_annotate(self):
        handler = getattr(actions_mod, "_handle_annotate", None)
        if handler is None:
            self.skipTest("_handle_annotate not available")
        result = handler(self._ctx())
        self.assertIn("annot", result.lower())
        self.assertIn("comment", result.lower())

    def test_clean_mcode(self):
        handler = getattr(actions_mod, "_handle_clean_mcode", None)
        if handler is None:
            self.skipTest("_handle_clean_mcode not available")
        result = handler(self._ctx())
        self.assertIn("microcode", result.lower())

    def test_xref_analysis(self):
        handler = getattr(actions_mod, "_handle_xref_analysis", None)
        if handler is None:
            self.skipTest("_handle_xref_analysis not available")
        result = handler(self._ctx())
        self.assertIn("cross-reference", result.lower())

    def test_fallback_function_name(self):
        """When func_name is None, handlers should use sub_<ea> format."""
        for name in ["_handle_explain", "_handle_rename", "_handle_deobfuscate",
                      "_handle_vuln_audit", "_handle_suggest_types", "_handle_annotate",
                      "_handle_clean_mcode", "_handle_xref_analysis"]:
            handler = getattr(actions_mod, name, None)
            if handler is None:
                continue
            result = handler(self._ctx(func_name=None, func_ea=None))
            self.assertIn("sub_401000", result, f"{name} should use sub_<ea> fallback")


if __name__ == "__main__":
    unittest.main()
