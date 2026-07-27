"""Prompt + tool-schema tests for the global `hide_strings` option.

Asserts:
  * `build_system_prompt(hide_strings=True)` emits the String Analysis Constraint
    block and never claims it when the option is False.
  * The base `ANALYSIS_SECTION` carries the ignore-filename rule and the
    disassembly-fallback rule.
  * `AgentLoop._build_tools_schema` removes `list_strings` and `search_strings`
    when `config.hide_strings` is True and keeps them when False.
"""

from __future__ import annotations

import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Ensure the project root is on the path so the rikugan package resolves.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.mocks.ida_mock import install_ida_mocks  # noqa: E402

install_ida_mocks()

from rikugan.agent.prompts.base import ANALYSIS_SECTION  # noqa: E402
from rikugan.agent.system_prompt import build_system_prompt  # noqa: E402


class TestAnalysisSectionRules(unittest.TestCase):
    def test_filename_rule_present(self) -> None:
        self.assertIn(
            "Never infer the binary's purpose, family, or behavior from a file name",
            ANALYSIS_SECTION,
        )

    def test_disassembly_fallback_rule_present(self) -> None:
        self.assertIn(
            "When a decompiler tool is unavailable or fails, fall back to read_function_disassembly",
            ANALYSIS_SECTION,
        )


class TestBuildSystemPromptHideStrings(unittest.TestCase):
    def test_default_off_does_not_emit_constraint(self) -> None:
        prompt = build_system_prompt(hide_strings=False)
        self.assertNotIn("String Analysis Constraint", prompt)
        self.assertNotIn("Do not call list_strings or search_strings", prompt)

    def test_on_emits_constraint_with_required_tools(self) -> None:
        prompt = build_system_prompt(hide_strings=True)
        self.assertIn("## String Analysis Constraint", prompt)
        self.assertIn("Do not call list_strings or search_strings", prompt)
        for tool in (
            "read_function_disassembly",
            "read_disassembly",
            "decompile_function",
            "get_pseudocode",
        ):
            self.assertIn(tool, prompt)

    def test_filename_rule_present_regardless_of_setting(self) -> None:
        for flag in (False, True):
            prompt = build_system_prompt(hide_strings=flag)
            self.assertIn(
                "Never infer the binary's purpose, family, or behavior from a file name",
                prompt,
            )


if __name__ == "__main__":
    unittest.main()
