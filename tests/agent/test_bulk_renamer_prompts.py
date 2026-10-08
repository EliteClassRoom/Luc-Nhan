"""Regression tests for bulk_renamer prompt naming conventions.

The bulk_renamer prompts historically demanded snake_case, contradicting the
system prompt's PascalCase. These tests lock in the PascalCase fix so the
inconsistency cannot silently return.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from lucnhan.agent.bulk_renamer import DEEP_ANALYSIS_PROMPT, QUICK_ANALYSIS_PROMPT


class TestBulkRenamerPromptsUsePascalCase(unittest.TestCase):
    def test_quick_prompt_does_not_demand_snake_case(self):
        """The original 'Use snake_case naming convention' directive must be gone."""
        self.assertNotIn(
            "Use snake_case naming convention",
            QUICK_ANALYSIS_PROMPT,
            "QUICK_ANALYSIS_PROMPT still demands snake_case",
        )

    def test_deep_prompt_does_not_demand_snake_case(self):
        """The original 'using snake_case convention' directive must be gone."""
        self.assertNotIn(
            "using snake_case convention",
            DEEP_ANALYSIS_PROMPT,
            "DEEP_ANALYSIS_PROMPT still demands snake_case",
        )

    def test_quick_prompt_enforces_pascalcase(self):
        self.assertIn("PascalCase", QUICK_ANALYSIS_PROMPT)
        self.assertIn("NEVER snake_case", QUICK_ANALYSIS_PROMPT)

    def test_deep_prompt_enforces_pascalcase(self):
        self.assertIn("PascalCase", DEEP_ANALYSIS_PROMPT)
        self.assertIn("NEVER snake_case", DEEP_ANALYSIS_PROMPT)

    def test_quick_prompt_mentions_uncertain_placeholder(self):
        """Quick prompt must teach the Unknown_ placeholder for <70% confidence."""
        self.assertIn("Unknown_<Hint>", QUICK_ANALYSIS_PROMPT)

    def test_deep_prompt_mentions_uncertain_placeholder(self):
        self.assertIn("Unknown_<Hint>", DEEP_ANALYSIS_PROMPT)

    def test_output_format_unchanged(self):
        """Output format must stay '0x<addr> <name>' / 'RENAME:' so parsers work."""
        self.assertIn("0x<address> <new_name>", QUICK_ANALYSIS_PROMPT)
        self.assertIn("RENAME: 0x<address> <new_name>", DEEP_ANALYSIS_PROMPT)


if __name__ == "__main__":
    unittest.main()
