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

from lucnhan.agent.agents.ida_docs_reviewer import IDA_DOCS_REVIEWER_PROMPT


class TestReviewerPromptPrefersTool(unittest.TestCase):
    def test_prompt_mentions_lookup_idapython_doc(self):
        from lucnhan.agent.agents.ida_docs_reviewer import IDA_DOCS_REVIEWER_PROMPT

        self.assertIn("lookup_idapython_doc", IDA_DOCS_REVIEWER_PROMPT)

    def test_prompt_demotes_web_fetch_to_fallback(self):
        from lucnhan.agent.agents.ida_docs_reviewer import (
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
        from lucnhan.agent.agents.ida_docs_reviewer import IDA_DOCS_REVIEWER_PROMPT

        # The fallback should mention "not in bundle" or similar
        lowered = IDA_DOCS_REVIEWER_PROMPT.lower()
        self.assertTrue(
            "not in bundle" in lowered or "fall back" in lowered,
            "Prompt should explain when to fall back to web_fetch",
        )

    def test_prompt_offline_first_priority(self):
        """Reviewer must explicitly say 'try offline FIRST' — not just 'prefer' it."""
        from lucnhan.agent.agents.ida_docs_reviewer import IDA_DOCS_REVIEWER_PROMPT

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
        from lucnhan.agent.agents.ida_docs_reviewer import IDA_DOCS_REVIEWER_PROMPT

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
        from lucnhan.agent.agents.ida_docs_reviewer import IDA_DOCS_REVIEWER_PROMPT

        # Phai nhac den runtime error / diagnose failure
        assert "diagnose" in IDA_DOCS_REVIEWER_PROMPT.lower() or "runtime" in IDA_DOCS_REVIEWER_PROMPT.lower()
        # Phai nhac den traceback trong input
        assert "traceback" in IDA_DOCS_REVIEWER_PROMPT.lower()

    def test_reviewer_prompt_keeps_verdict_contract(self):
        """Output contract (VERDICT/REASONS/API_NOTES/REWRITE_GUIDANCE) stays."""
        from lucnhan.agent.agents.ida_docs_reviewer import IDA_DOCS_REVIEWER_PROMPT

        assert "VERDICT:" in IDA_DOCS_REVIEWER_PROMPT
        assert "REASONS:" in IDA_DOCS_REVIEWER_PROMPT
        assert "API_NOTES:" in IDA_DOCS_REVIEWER_PROMPT
        assert "REWRITE_GUIDANCE:" in IDA_DOCS_REVIEWER_PROMPT


class TestSkillPrefersTool(unittest.TestCase):
    SKILL_PATH = (
        Path(__file__).resolve().parent.parent / "lucnhan" / "skills" / "builtins" / "ida-scripting" / "SKILL.md"
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
        # lucnhan/agent/loop.py:2058-2060 filters it out and the agent can't call it
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


# ---------------------------------------------------------------------------
# Docs-fetch URL guidance
#
# The Sphinx docs site behind ``python.docs.hex-rays.com`` returns ``403
# Forbidden`` for deep-link HTML pages (``/<module>/<func>.html``) — the
# response is rejected by the site's bot protection.  Module index pages and
# raw RST source files (``/_sources/<module>/index.rst.txt``) return ``200
# OK``.  The bundled ``ida-scripting`` skill and the reviewer prompt must
# steer the agent to the URL pattern that actually works; a failure here is
# the primary regression guard against sending the LLM into a 403 loop.
# ---------------------------------------------------------------------------


class TestReviewerPromptUrlGuidance(unittest.TestCase):
    """The reviewer system prompt must point to URLs that return 200 OK."""

    def test_prompt_recommends_rst_source_format(self):
        # /_sources/<module>/index.rst.txt is the only pattern that
        # returns full module reference AND survives CDN bot protection.
        self.assertIn(
            "/_sources/",
            IDA_DOCS_REVIEWER_PROMPT,
            "Reviewer prompt must recommend the Sphinx raw RST source format (/_sources/<module>/index.rst.txt).",
        )

    def test_prompt_recommends_source_with_module_template(self):
        # The reviewer must understand the <module> slot in the RST URL.
        # The post-error prompt uses the generic <module> template rather
        # than a concrete module example.
        self.assertIn(
            "_sources/<module>/index.rst.txt",
            IDA_DOCS_REVIEWER_PROMPT,
            "Reviewer prompt must show the RST source URL with a <module> slot.",
        )

    def test_prompt_warns_about_html_403(self):
        # If the reviewer follows the broken HTML pattern, every deep
        # link fetch returns 403 and burns a turn.  Pre-empt it.
        self.assertIn(
            "403",
            IDA_DOCS_REVIEWER_PROMPT,
            "Reviewer prompt must warn that HTML deep-link pages return 403 Forbidden (bot-protected).",
        )

    def test_prompt_demotes_html_pages_below_rst_source(self):
        # The /<module>/<func>.html pattern should be clearly marked
        # as unreliable, not as the primary online source.
        # We accept the legacy pattern being present ONLY if the prompt
        # also explicitly warns against it.
        prompt = IDA_DOCS_REVIEWER_PROMPT
        self.assertIn("DO NOT fetch HTML", prompt)

    def test_prompt_lists_html_pattern_danger_zone(self):
        # The exact broken pattern must be shown so the LLM recognizes
        # it as something to avoid.
        self.assertIn(
            "ida_<module>/<func>.html",
            IDA_DOCS_REVIEWER_PROMPT,
            "Reviewer prompt must show the failing HTML pattern so the LLM can recognize and skip it.",
        )

    def test_prompt_has_documentation_sources_section(self):
        # Sanity: the existing structure is preserved.
        self.assertIn("Documentation sources", IDA_DOCS_REVIEWER_PROMPT)
        self.assertIn("ida-scripting", IDA_DOCS_REVIEWER_PROMPT.lower())


class TestIdaScriptingSkillUrlGuidance(unittest.TestCase):
    """The bundled ``ida-scripting`` SKILL.md teaches the same lesson.

    Otherwise, any agent that consults the skill (not just the docs
    reviewer) will retry the broken HTML pattern.
    """

    SKILL_PATH = (
        Path(__file__).resolve().parent.parent / "lucnhan" / "skills" / "builtins" / "ida-scripting" / "SKILL.md"
    )

    def setUp(self):
        self.body = self.SKILL_PATH.read_text(encoding="utf-8")

    def test_skill_recommends_rst_source_format(self):
        self.assertIn(
            "/_sources/",
            self.body,
            "ida-scripting SKILL.md must recommend the Sphinx raw RST source format (/_sources/<module>/index.rst.txt).",
        )

    def test_skill_warns_about_html_403(self):
        self.assertIn(
            "403",
            self.body,
            "ida-scripting SKILL.md must warn that HTML deep-link pages return 403 Forbidden (bot-protected).",
        )

    def test_skill_when_to_fetch_more_section_present(self):
        self.assertIn("## When to fetch more", self.body)


if __name__ == "__main__":
    unittest.main()
