"""Tests for the evidence-comment merge helper and policy."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.ida.tools import annotations
from rikugan.ida.tools.annotations import RUGUGAN_EVIDENCE_TAG, merge_evidence_line
from rikugan.tools.registry import ToolRegistry


class TestMergeEvidenceLine(unittest.TestCase):
    def test_appends_when_empty(self):
        self.assertEqual(
            merge_evidence_line("", "calls recv"),
            f"{RUGUGAN_EVIDENCE_TAG} calls recv",
        )

    def test_appends_when_other_text_preserves_analyst_note(self):
        merged = merge_evidence_line("analyst note", "calls recv")
        self.assertIn("analyst note", merged)
        self.assertIn(f"{RUGUGAN_EVIDENCE_TAG} calls recv", merged)

    def test_replaces_existing_tagged_line_preserves_following(self):
        existing = (
            f"analyst note\n{RUGUGAN_EVIDENCE_TAG} old claim\nfollowup analyst"
        )
        merged = merge_evidence_line(existing, "new claim")
        self.assertIn("analyst note", merged)
        self.assertIn("followup analyst", merged)
        self.assertIn(f"{RUGUGAN_EVIDENCE_TAG} new claim", merged)
        self.assertNotIn("old claim", merged)

    def test_blank_evidence_returns_existing(self):
        self.assertEqual(merge_evidence_line("hello", ""), "hello")
        self.assertEqual(merge_evidence_line("hello", "   "), "hello")

    def test_tag_constant_matches_helper(self):
        self.assertEqual(RUGUGAN_EVIDENCE_TAG, "[Rikugan Evidence]")

    def test_helper_not_registered_as_tool(self):
        """merge_evidence_line must NOT be a @tool-registered function."""
        registry = ToolRegistry()
        registry.register_module(annotations)
        names = {t.name for t in registry.list_available_tools()}
        self.assertNotIn("merge_evidence_line", names)
        self.assertIn("rename_function", names)
        self.assertIn("set_function_comment", names)


class TestExplorationAddendumContract(unittest.TestCase):
    def _text(self):
        from rikugan.agent.exploration_mode import EXPLORATION_SYSTEM_ADDENDUM

        # Normalize whitespace so the ordering assertions match across
        # line breaks the prompt uses.
        return " ".join(EXPLORATION_SYSTEM_ADDENDUM.split())

    def test_exploration_addendum_step_ordering(self):
        """Plan §4.38 requires the explicit numbered sequence
        get→merge→set→verify→rename.
        """
        text = self._text()
        # The >0.90 sentence must appear before the 0.70-0.90 block.
        self.assertLess(
            text.index("Confidence **> 0.90**"),
            text.index("Confidence **0.70 – 0.90**"),
        )
        # The numbered mid-confidence sequence must be in order.
        idx_get = text.index(
            "Call `get_function_comment(address, repeatable=True)` to read."
        )
        idx_set = text.index(
            "Call `set_function_comment(address, merged, repeatable=True)`."
        )
        idx_re = text.index("Re-read with `get_function_comment")
        idx_skip = text.index("do NOT call `rename_function`")
        self.assertLess(idx_get, idx_set)
        self.assertLess(idx_set, idx_re)
        self.assertLess(idx_re, idx_skip)
        # The <0.70 branch must follow the 0.70-0.90 block.
        self.assertLess(
            text.index("Confidence **0.70 – 0.90**"),
            text.index("Confidence **< 0.70**"),
        )
        # Coverage: <0.70 must forbid renames and persist hypothesis.
        self.assertIn(
            "Confidence **< 0.70**: do not rename", text
        )
        # Coverage: verified persistence gate (Plan §1/§5).
        self.assertIn("verified", text.lower())
        # Coverage: readback-failure must log a hypothesis, not rename.
        self.assertIn(
            'log an `exploration_report(category="hypothesis")`',
            text,
        )


if __name__ == "__main__":
    unittest.main()
