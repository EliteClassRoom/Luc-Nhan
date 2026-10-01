"""Executable triple-sync contract: validator BLOCKED_CALLS/WARNED_CALLS/BLOCKED_MODULES
must be mirrored in the system prompt's IDA API Discipline section and the
ida-scripting skill's DO NOT USE table (comment contract at prompts/base.py:311-314).
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.agent.prompts.base import IDA_API_DISCIPLINE_SECTION
from rikugan.tools.validate_idapython import BLOCKED_CALLS, BLOCKED_MODULES, WARNED_CALLS

_SKILL_PATH = (
    Path(__file__).resolve().parent.parent.parent / "rikugan" / "skills" / "builtins" / "ida-scripting" / "SKILL.md"
)


class TestPromptValidatorSync(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill_text = _SKILL_PATH.read_text(encoding="utf-8")

    def test_every_blocked_call_in_prompt_and_skill(self):
        for key in BLOCKED_CALLS:
            self.assertIn(
                key, IDA_API_DISCIPLINE_SECTION, f"{key} is blocked by the validator but missing from the prompt table"
            )
            self.assertIn(
                key, self.skill_text, f"{key} is blocked by the validator but missing from the skill DO NOT USE table"
            )

    def test_every_blocked_module_in_prompt_and_skill(self):
        for module in BLOCKED_MODULES:
            self.assertIn(module, IDA_API_DISCIPLINE_SECTION)
            self.assertIn(module, self.skill_text)

    def test_every_warned_call_in_prompt(self):
        for key in WARNED_CALLS:
            self.assertIn(
                key,
                IDA_API_DISCIPLINE_SECTION,
                f"{key} is warned by the validator but missing from the prompt's discouraged-legacy list",
            )
