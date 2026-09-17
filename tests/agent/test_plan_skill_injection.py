"""Pin: plan-mode skills skip the _resolve_skill rewrite (single skill-body injection)."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.agent.loop import AgentLoop


class _FakeSkill:
    def __init__(self, slug: str, mode: str):
        self.slug = slug
        self.name = slug
        self.body = "SKILL BODY CONTENT"
        self.mode = mode


class _FakeRegistry:
    def __init__(self, skill):
        self._skill = skill

    def resolve_skill_invocation(self, message):
        prefix = f"/{self._skill.slug} "
        if message.startswith(prefix):
            return self._skill, message[len(prefix) :]
        return None, message

    def match_triggers(self, message):
        return self._skill if "triggerword" in message else None


class TestPlanSkillNoRewrite(unittest.TestCase):
    def _loop_with(self, skill):
        loop = object.__new__(AgentLoop)
        loop.skills = _FakeRegistry(skill)
        return loop

    def test_explicit_plan_skill_returns_remaining_unrewritten(self):
        loop = self._loop_with(_FakeSkill("deobfuscation", "plan"))
        msg, skill = AgentLoop._resolve_skill(loop, "/deobfuscation analyze this packer")
        self.assertEqual(msg, "analyze this packer")
        self.assertEqual(skill.slug, "deobfuscation")
        self.assertNotIn("SKILL BODY CONTENT", msg)

    def test_trigger_matched_plan_skill_unrewritten(self):
        loop = self._loop_with(_FakeSkill("deobfuscation", "plan"))
        msg, skill = AgentLoop._resolve_skill(loop, "please help, triggerword here")
        self.assertEqual(msg, "please help, triggerword here")
        self.assertNotIn("SKILL BODY CONTENT", msg)

    def test_exploration_skill_still_rewrites(self):
        loop = self._loop_with(_FakeSkill("modify", "exploration"))
        msg, _ = AgentLoop._resolve_skill(loop, "/modify patch the check")
        self.assertIn("[Skill: modify]", msg)
        self.assertIn("SKILL BODY CONTENT", msg)

    def test_normal_skill_still_rewrites(self):
        loop = self._loop_with(_FakeSkill("generic-re", ""))
        msg, _ = AgentLoop._resolve_skill(loop, "/generic-re look at this")
        self.assertIn("SKILL BODY CONTENT", msg)
        self.assertIn("look at this", msg)


if __name__ == "__main__":
    unittest.main()
