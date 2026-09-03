"""Unit tests for the IDA docs reviewer prompt.

Task 10 of 13 (offline docs tool).  These tests pin the reviewer
prompt so that ``lookup_idapython_doc`` is the preferred doc source
and ``web_fetch`` is demoted to a fallback only.

Task 11 of 13 (offline docs tool).  Mirrors the same preference at the
SKILL.md level so the skill body recommends the offline tool first and
demotes ``web_fetch`` to a fallback.
"""

from __future__ import annotations

import unittest
from pathlib import Path


class TestReviewerPromptPrefersTool(unittest.TestCase):
    def test_prompt_mentions_lookup_idapython_doc(self):
        from rikugan.agent.agents.ida_docs_reviewer import IDA_DOCS_REVIEWER_PROMPT

        self.assertIn("lookup_idapython_doc", IDA_DOCS_REVIEWER_PROMPT)

    def test_prompt_demotes_web_fetch_to_fallback(self):
        from rikugan.agent.agents.ida_docs_reviewer import (
            build_ida_docs_reviewer_addendum,
        )

        prompt = build_ida_docs_reviewer_addendum()
        tool_idx = prompt.find("lookup_idapython_doc")
        self.assertGreater(tool_idx, -1, "lookup_idapython_doc not in prompt")
        # The first web_fetch occurrence after the tool entry should exist
        # (tool first → fallback later).
        web_fetch_idx = prompt.find("web_fetch", tool_idx) if tool_idx >= 0 else -1
        self.assertGreater(web_fetch_idx, -1, "web_fetch not in prompt after the tool")
        # Tool appears strictly before its fallback statement
        self.assertLess(tool_idx, web_fetch_idx)

    def test_prompt_explains_fallback_reason(self):
        from rikugan.agent.agents.ida_docs_reviewer import IDA_DOCS_REVIEWER_PROMPT

        # The fallback should mention "not in bundle" or similar
        lowered = IDA_DOCS_REVIEWER_PROMPT.lower()
        self.assertTrue(
            "not in bundle" in lowered or "fall back" in lowered,
            "Prompt should explain when to fall back to web_fetch",
        )

    def test_prompt_offline_first_priority(self):
        """Reviewer must explicitly say 'try offline FIRST' — not just 'prefer' it."""
        from rikugan.agent.agents.ida_docs_reviewer import IDA_DOCS_REVIEWER_PROMPT

        # The prompt must make clear that offline is the first attempt, not just a preferred option
        self.assertIn(
            "Always try",
            IDA_DOCS_REVIEWER_PROMPT,
            "Prompt should explicitly tell reviewer to ALWAYS try lookup_idapython_doc first",
        )
        # And explicitly state that web_fetch should not be the first attempt
        self.assertIn(
            "Do NOT use",
            IDA_DOCS_REVIEWER_PROMPT,
            "Prompt should explicitly forbid using web_fetch as first attempt",
        )

    def test_prompt_fallback_after_offline_fails(self):
        """Fallback trigger must be 'after offline fails', not just 'when module missing'."""
        from rikugan.agent.agents.ida_docs_reviewer import IDA_DOCS_REVIEWER_PROMPT

        lowered = IDA_DOCS_REVIEWER_PROMPT.lower()
        # Must mention BOTH fallback scenarios:
        # 1. Module not in bundle
        # 2. Offline docs were consulted but did not resolve
        self.assertIn(
            "not in",
            lowered,
            "Prompt should mention 'not in bundle' as one fallback trigger",
        )
        self.assertIn(
            "did not resolve",
            lowered,
            "Prompt should mention offline docs failing to resolve as fallback trigger",
        )


class TestReviewerPostErrorRole(unittest.TestCase):
    """Task 4 (SDD): reviewer is now a post-error diagnostician, not a
    pre-execute gate.  Its input carries a traceback + exception type and
    it diagnoses why the script FAILED at runtime."""

    def test_reviewer_prompt_describes_post_error_role(self):
        """Reviewer prompt must describe the post-error diagnostician role."""
        from rikugan.agent.agents.ida_docs_reviewer import IDA_DOCS_REVIEWER_PROMPT

        # Phai nhac den runtime error / diagnose failure
        assert "diagnose" in IDA_DOCS_REVIEWER_PROMPT.lower() or "runtime" in IDA_DOCS_REVIEWER_PROMPT.lower()
        # Phai nhac den traceback trong input
        assert "traceback" in IDA_DOCS_REVIEWER_PROMPT.lower()

    def test_reviewer_prompt_keeps_verdict_contract(self):
        """Output contract (VERDICT/REASONS/API_NOTES/REWRITE_GUIDANCE) stays."""
        from rikugan.agent.agents.ida_docs_reviewer import IDA_DOCS_REVIEWER_PROMPT

        assert "VERDICT:" in IDA_DOCS_REVIEWER_PROMPT
        assert "REASONS:" in IDA_DOCS_REVIEWER_PROMPT
        assert "API_NOTES:" in IDA_DOCS_REVIEWER_PROMPT
        assert "REWRITE_GUIDANCE:" in IDA_DOCS_REVIEWER_PROMPT


class TestSkillPrefersTool(unittest.TestCase):
    SKILL_PATH = (
        Path(__file__).resolve().parent.parent / "rikugan" / "skills" / "builtins" / "ida-scripting" / "SKILL.md"
    )

    def setUp(self):
        self.body = self.SKILL_PATH.read_text(encoding="utf-8")

    def test_skill_recommends_lookup_idapython_doc(self):
        self.assertIn("lookup_idapython_doc", self.body)

    def test_skill_demotes_web_fetch_to_fallback(self):
        tool_idx = self.body.find("lookup_idapython_doc")
        web_fetch_idx = self.body.find("web_fetch", tool_idx) if tool_idx >= 0 else -1
        self.assertGreater(tool_idx, -1)
        self.assertGreater(web_fetch_idx, -1)
        self.assertLess(tool_idx, web_fetch_idx)

    def test_skill_frontmatter_allows_lookup_idapython_doc(self):
        # Frontmatter allowed_tools must include lookup_idapython_doc — otherwise
        # rikugan/agent/loop.py:2058-2060 filters it out and the agent can't call it
        # even though the skill body recommends it.
        import yaml

        text = self.SKILL_PATH.read_text(encoding="utf-8")
        # Parse frontmatter (between --- markers)
        parts = text.split("---", 2)
        assert len(parts) >= 3, "frontmatter not found"
        fm = yaml.safe_load(parts[1])
        self.assertIn("lookup_idapython_doc", fm.get("allowed_tools", []))

    def test_skill_offline_first_priority(self):
        """SKILL.md must tell agent to try offline FIRST, web_fetch as last resort."""
        self.assertIn(
            "always try",
            self.body.lower(),
            "SKILL.md must say 'always try' offline tool first",
        )
        self.assertIn(
            "do **not**",
            self.body.lower(),
            "SKILL.md must explicitly forbid using web_fetch as first attempt",
        )

    def test_skill_fallback_after_offline_fails(self):
        """SKILL.md fallback trigger must include both 'module not in bundle' AND 'verification still has gaps'."""
        lowered = self.body.lower()
        self.assertIn("module not in offline bundle", lowered)
        self.assertIn("still has gaps", lowered)

    def test_skill_triggers_include_common_ida_modules(self):
        """Skill frontmatter `triggers` list must include the 13 common IDA modules
        so the skill auto-activates when an agent mentions any of them. Regression
        guard: skills without these triggers will fail to load when the agent's
        message contains e.g. 'ida_typeinf' but no broader trigger word.
        """
        import yaml

        text = self.SKILL_PATH.read_text(encoding="utf-8")
        parts = text.split("---", 2)
        assert len(parts) >= 3, "frontmatter not found"
        fm = yaml.safe_load(parts[1])
        triggers = fm.get("triggers", [])
        missing = []
        for module in [
            "ida_bytes",
            "ida_funcs",
            "ida_hexrays",
            "ida_typeinf",
            "ida_name",
            "ida_segment",
            "ida_xref",
            "ida_kernwin",
            "ida_frame",
            "idautils",
            "idaapi",
            "ida_ua",
            "idc",
        ]:
            if module not in triggers:
                missing.append(module)
        self.assertEqual(
            missing,
            [],
            f"Skill triggers missing common IDA modules: {missing}. "
            f"Add these so the skill activates when agent mentions them, "
            f"triggering the lookup_idapython_doc recommendation.",
        )

    def test_skill_prefers_point_lookup_over_hasattr(self):
        """SKILL.md must recommend the `name` parameter for point-lookups,
        and explicitly contrast it against hasattr()/execute_python probes."""
        self.assertIn("name", self.body)  # the parameter name
        self.assertIn("hasattr", self.body)
        self.assertIn("execute_python", self.body)
        self.assertIn("instead of", self.body.lower())


if __name__ == "__main__":
    unittest.main()
