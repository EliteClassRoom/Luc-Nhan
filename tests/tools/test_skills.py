"""Tests for the skills system."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.skills.loader import (
    _parse_frontmatter,
    _split_frontmatter,
    discover_skills,
)
from rikugan.skills.registry import SkillRegistry


class TestFrontmatterParser(unittest.TestCase):
    def test_simple_key_value(self):
        text = "name: My Skill\ndescription: Does something"
        result = _parse_frontmatter(text)
        self.assertEqual(result["name"], "My Skill")
        self.assertEqual(result["description"], "Does something")

    def test_inline_list(self):
        text = "tags: [vuln, audit, security]"
        result = _parse_frontmatter(text)
        self.assertEqual(result["tags"], ["vuln", "audit", "security"])

    def test_block_list(self):
        text = "allowed_tools:\n  - decompile_function\n  - get_xrefs\n  - rename_function"
        result = _parse_frontmatter(text)
        self.assertEqual(
            result["allowed_tools"],
            [
                "decompile_function",
                "get_xrefs",
                "rename_function",
            ],
        )

    def test_quoted_values(self):
        text = 'name: "My Quoted Skill"\nversion: "1.0"'
        result = _parse_frontmatter(text)
        self.assertEqual(result["name"], "My Quoted Skill")
        self.assertEqual(result["version"], "1.0")

    def test_empty_value(self):
        text = "name: Test\nempty_key:"
        result = _parse_frontmatter(text)
        self.assertEqual(result["empty_key"], "")

    def test_comments_ignored(self):
        text = "# This is a comment\nname: Test"
        result = _parse_frontmatter(text)
        self.assertEqual(result["name"], "Test")
        self.assertNotIn("#", result)

    def test_block_list_with_leading_comment(self):
        # Regression: a comment line at the start of a block list must NOT
        # truncate parsing. The builtin ida-scripting SKILL.md relied on this
        # when it had grouped comments (removed later), but the parser must
        # stay robust to comments inside block lists.
        text = "triggers:\n  # grouped keyword\n  - idapython\n  - write ida script\n"
        result = _parse_frontmatter(text)
        self.assertEqual(result["triggers"], ["idapython", "write ida script"])

    def test_inline_list_no_trailing_comment_support(self):
        # Inline lists do not strip trailing comments by design — the whole
        # remainder after `key:` is treated as the scalar. This documents
        # current behavior; use block lists for commented values.
        text = "tags: [a, b]  # comment becomes part of value"
        result = _parse_frontmatter(text)
        # The `[a, b]  # ...` is not recognized as an inline list, so it is
        # stored as the raw scalar string.
        self.assertIsInstance(result["tags"], str)

    # -- YAML folded / literal block scalars (>, |) ---------------------

    def test_folded_scalar_strip_indicator(self):
        # `>-` is the YAML folded block scalar with strip indicator.
        # Newlines between non-empty lines fold into single spaces; the
        # final trailing newline is stripped. This is what
        # deobfuscation/SKILL.md uses for its description.
        text = (
            "name: Deobfuscation\n"
            "description: >-\n"
            "  Systematic binary deobfuscation — string decryption,\n"
            "  control flow flattening (CFF) removal, opaque predicate\n"
            "  elimination.\n"
        )
        result = _parse_frontmatter(text)
        self.assertEqual(
            result["description"],
            "Systematic binary deobfuscation — string decryption, "
            "control flow flattening (CFF) removal, opaque predicate "
            "elimination.",
        )

    def test_folded_scalar_default_indicator(self):
        # `>` alone (no chomping indicator) is the folded scalar with clip
        # behavior: trailing newlines collapse to a single one. We strip
        # it like the existing scalar parser does, so the value is the
        # folded string with no trailing newline.
        text = "notes: >\n  Line one of the note.\n  Line two of the note.\n"
        result = _parse_frontmatter(text)
        self.assertEqual(result["notes"], "Line one of the note. Line two of the note.")

    def test_literal_scalar_strip_indicator(self):
        # `|-` is the literal block scalar with strip indicator: newlines
        # are preserved verbatim, trailing newline stripped.
        text = "example: |-\n  First line of code.\n  Second line of code.\n"
        result = _parse_frontmatter(text)
        self.assertEqual(
            result["example"],
            "First line of code.\nSecond line of code.",
        )

    def test_folded_scalar_preserves_internal_blank_lines(self):
        # In folded style, a blank line between content lines is preserved
        # as a single newline (so paragraphs stay separated). The deobfus-
        # cation SKILL.md relies on this between the main description and
        # the Trigger: keyword.
        text = "description: >-\n  First paragraph about the skill.\n\n  Second paragraph that should stay separate.\n"
        result = _parse_frontmatter(text)
        self.assertEqual(
            result["description"],
            "First paragraph about the skill.\nSecond paragraph that should stay separate.",
        )

    def test_folded_scalar_ignored_when_value_inline(self):
        # If the indicator has content after it on the same line, it is
        # NOT a folded scalar — YAML treats that as a comment-style or
        # plain scalar. Our parser should treat it as a plain scalar
        # (existing behavior, documented here for coverage).
        text = "name: value with > in the middle"
        result = _parse_frontmatter(text)
        self.assertEqual(result["name"], "value with > in the middle")


class TestSplitFrontmatter(unittest.TestCase):
    def test_with_frontmatter(self):
        text = "---\nname: Test\n---\nBody content here"
        fm, body = _split_frontmatter(text)
        self.assertIn("name: Test", fm)
        self.assertEqual(body.strip(), "Body content here")

    def test_without_frontmatter(self):
        text = "Just a body with no frontmatter"
        fm, body = _split_frontmatter(text)
        self.assertEqual(fm, "")
        self.assertEqual(body, text)

    def test_empty_body(self):
        text = "---\nname: Test\n---\n"
        fm, body = _split_frontmatter(text)
        self.assertIn("name: Test", fm)


class TestDiscoverSkills(unittest.TestCase):
    def test_discover_from_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a skill
            skill_dir = os.path.join(tmpdir, "vuln-audit")
            os.makedirs(skill_dir)
            with open(os.path.join(skill_dir, "SKILL.md"), "w") as f:
                f.write(
                    "---\nname: Vulnerability Audit\ndescription: Find security bugs\ntags: [security]\n---\nYou are a security auditor.\nAnalyze the function for vulnerabilities.\n"
                )

            skills = discover_skills(tmpdir)
            self.assertEqual(len(skills), 1)
            self.assertEqual(skills[0].name, "Vulnerability Audit")
            self.assertEqual(skills[0].slug, "vuln-audit")
            self.assertEqual(skills[0].description, "Find security bugs")
            self.assertEqual(skills[0].tags, ["security"])
            self.assertIn("security auditor", skills[0].body)

    def test_missing_directory(self):
        skills = discover_skills("/nonexistent/path")
        self.assertEqual(skills, [])

    def test_skip_invalid_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Directory without SKILL.md
            os.makedirs(os.path.join(tmpdir, "no-skill"))
            # Regular file (not directory)
            with open(os.path.join(tmpdir, "file.txt"), "w") as f:
                f.write("not a skill")

            skills = discover_skills(tmpdir)
            self.assertEqual(len(skills), 0)


class TestSkillRegistry(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Create two skills
        for slug, name, desc in [
            ("vuln-audit", "Vulnerability Audit", "Find security bugs"),
            ("rename-all", "Bulk Rename", "Rename everything in a function"),
        ]:
            skill_dir = os.path.join(self.tmpdir, slug)
            os.makedirs(skill_dir)
            with open(os.path.join(skill_dir, "SKILL.md"), "w") as f:
                f.write(f"---\nname: {name}\ndescription: {desc}\n---\nBody for {slug}\n")

    def test_discover_and_list(self):
        reg = SkillRegistry(self.tmpdir)
        count = reg.discover()
        # Built-in skills + 2 user skills (vuln-audit overrides the built-in)
        self.assertGreaterEqual(count, 2)
        self.assertIn("vuln-audit", reg.list_slugs())
        self.assertIn("rename-all", reg.list_slugs())

    def test_get_skill(self):
        reg = SkillRegistry(self.tmpdir)
        reg.discover()
        skill = reg.get("vuln-audit")
        self.assertIsNotNone(skill)
        # User skill overrides the built-in one with the same slug
        self.assertEqual(skill.name, "Vulnerability Audit")
        self.assertIn("Body for vuln-audit", skill.body)

    def test_get_missing(self):
        reg = SkillRegistry(self.tmpdir)
        reg.discover()
        self.assertIsNone(reg.get("nonexistent"))

    def test_summary_for_prompt(self):
        reg = SkillRegistry(self.tmpdir)
        reg.discover()
        summary = reg.get_summary_for_prompt()
        self.assertIn("/vuln-audit", summary)
        self.assertIn("/rename-all", summary)
        self.assertIn("Find security bugs", summary)

    def test_empty_registry_summary(self):
        # A fresh registry with no discover() call has no skills
        reg = SkillRegistry("/nonexistent")
        self.assertIsNone(reg.get_summary_for_prompt())

    def test_resolve_skill_invocation(self):
        reg = SkillRegistry(self.tmpdir)
        reg.discover()

        skill, remaining = reg.resolve_skill_invocation("/vuln-audit check this function")
        self.assertIsNotNone(skill)
        self.assertEqual(skill.slug, "vuln-audit")
        self.assertEqual(remaining, "check this function")

    def test_resolve_no_match(self):
        reg = SkillRegistry(self.tmpdir)
        reg.discover()

        skill, remaining = reg.resolve_skill_invocation("just a normal message")
        self.assertIsNone(skill)
        self.assertEqual(remaining, "just a normal message")

    def test_resolve_unknown_slug(self):
        reg = SkillRegistry(self.tmpdir)
        reg.discover()

        skill, remaining = reg.resolve_skill_invocation("/unknown-skill do something")
        self.assertIsNone(skill)
        self.assertEqual(remaining, "/unknown-skill do something")

    def test_report_skill_is_discoverable(self):
        # The /report built-in skill must be discovered from
        # rikugan/skills/builtins/report/ with the documented slug
        # and description, so the migration from a hard-coded
        # autocomplete entry to a real registered skill is locked in.
        reg = SkillRegistry()
        reg.discover()
        skill = reg.get("report")
        self.assertIsNotNone(skill, "report skill not discovered")
        self.assertEqual(skill.slug, "report")
        self.assertEqual(skill.name, "Verified Report")
        self.assertIn("verified", skill.description.lower())
        self.assertIn("/report", skill.body)


class TestBuiltinTriggerMatching(unittest.TestCase):
    """Regression: ensure the merged ida-scripting skill wins IDAPython scripting
    queries and the removed ida-docs / ida-pro-mcp skills no longer compete.

    These tests load the real built-in skills directory, so they catch drift if
    someone re-adds a competing skill or drops ida-scripting's triggers.
    """

    @classmethod
    def setUpClass(cls):
        cls.reg = SkillRegistry()  # uses default builtins dir
        cls.reg.discover()

    def test_merged_skills_absent(self):
        # ida-docs and ida-pro-mcp were merged into ida-scripting.
        slugs = set(self.reg.list_slugs())
        self.assertNotIn("ida-docs", slugs)
        self.assertNotIn("ida-pro-mcp", slugs)
        self.assertIn("ida-scripting", slugs)

    def test_ida_scripting_has_triggers(self):
        # Regression for the comment-in-block-list bug: if triggers parse to
        # empty, every query below would return (none).
        skill = self.reg.get("ida-scripting")
        self.assertIsNotNone(skill)
        self.assertGreater(len(skill.triggers), 10, "ida-scripting must carry its scripting triggers")

    def test_scripting_queries_hit_ida_scripting(self):
        for query in (
            "write an idapython script to iterate all functions",
            "use ida_bytes to read memory at an address",
            "help me use ida_hexrays to decompile this function",
            "walk the ctree to find all calls",
            "install a microcode optimizer",
        ):
            with self.subTest(query=query):
                skill = self.reg.match_triggers(query)
                self.assertIsNotNone(skill, f"no match for: {query}")
                self.assertEqual(skill.slug, "ida-scripting", f"expected ida-scripting for: {query}")

    def test_unrelated_query_no_match(self):
        # A non-scripting query should not trip the scripting skill.
        skill = self.reg.match_triggers("analyze this binary for malware behavior")
        self.assertIsNone(skill)

    def test_vuln_audit_still_wins_own_queries(self):
        # ida-scripting must not steal vuln-audit's territory.
        skill = self.reg.match_triggers("scan for buffer overflow and format string bugs")
        self.assertIsNotNone(skill)
        self.assertEqual(skill.slug, "vuln-audit")

    def test_naming_convention_skill_discovered(self):
        """The naming-convention skill must be discoverable with its triggers."""
        skill = self.reg.get("naming-convention")
        self.assertIsNotNone(skill, "naming-convention skill not discovered")
        self.assertGreater(len(skill.triggers), 10, "naming-convention must carry its triggers")

    def test_naming_convention_wins_rename_queries(self):
        """Rename/wrapper/thunk queries must route to naming-convention."""
        for query in (
            "what naming convention should I use for this function",
            "how should I pick a wrapper name for this function",
            "what thunk name should I use when the target is malloc",
            "how to name an unknown function I'm not sure about",
            "naming standard for enum members",
        ):
            with self.subTest(query=query):
                skill = self.reg.match_triggers(query)
                self.assertIsNotNone(skill, f"no match for: {query}")
                self.assertEqual(
                    skill.slug,
                    "naming-convention",
                    f"expected naming-convention for: {query}",
                )

    def test_naming_convention_does_not_steal_analysis_queries(self):
        """naming-convention must not steal general analysis queries that
        belong to generic-re or malware-analysis."""
        for query in (
            "analyze this binary for malware behavior",
            "what does this binary do overall",
            "find all crypto imports in this sample",
            "explain what this wrapper class does in the codebase",
        ):
            with self.subTest(query=query):
                # These should NOT resolve to naming-convention.
                skill = self.reg.match_triggers(query)
                if skill is not None:
                    self.assertNotEqual(
                        skill.slug,
                        "naming-convention",
                        f"naming-convention stole a general query: {query}",
                    )



    def test_malware_analysis_requires_hypothesis_logging_and_verification(self):
        """The built-in malware-analysis skill must instruct the agent to:

        1. Log every concrete behavioral hypothesis through
           ``exploration_report(category=\"hypothesis\", summary=...)``.
        2. Treat hypotheses as provisional ``unverified`` records.
        3. Run ``/verify`` before ``/report`` and never author a report
           that promotes unverified or ``wrong`` hypotheses to facts.
        """
        skill = self.reg.get("malware-analysis")
        self.assertIsNotNone(skill, "malware-analysis skill not discovered")
        body = skill.body
        # Hypothesis logging contract.
        self.assertIn("exploration_report", body)
        self.assertIn("category=\"hypothesis\"", body)
        self.assertIn("summary=", body)
        self.assertIn("evidence=", body)
        self.assertIn("relevance=", body)
        # Provisional-until-verified contract.
        self.assertIn("unverified", body)
        # /verify runs before /report, and the report writer consumes
        # only verified hypotheses.
        self.assertIn("/verify", body)
        self.assertIn("/report", body)
        self.assertIn("verified", body)
        # The report must not promote unverified or wrong hypotheses.
        self.assertIn("wrong", body)
        # Surface the verdict claim/citations from /verify in the report.
        self.assertIn("claim", body)
        self.assertIn("citation", body)

    def test_malware_analysis_skill_naming_section_expanded(self):
        """malware-analysis must carry the full 6-rule naming summary, not just 3."""
        skill = self.reg.get("malware-analysis")
        self.assertIsNotNone(skill)
        body = skill.body
        self.assertIn("Variables: snake_case", body)
        self.assertIn("Enums", body)
        self.assertIn("/naming-convention", body)

    def test_generic_re_skill_naming_section_expanded(self):
        """generic-re must carry the full 6-rule naming summary, not just 1 line."""
        skill = self.reg.get("generic-re")
        self.assertIsNotNone(skill)
        body = skill.body
        self.assertIn("Variables: snake_case", body)
        self.assertIn("Enums", body)
        self.assertIn("/naming-convention", body)


if __name__ == "__main__":
    unittest.main()
