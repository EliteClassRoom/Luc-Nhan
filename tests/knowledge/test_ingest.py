"""Tests for rikugan.memory.ingest.

Exercises auto-ingest paths for save_memory, exploration_report,
and research_note. These tests don't touch IDA / Qt — they operate
purely against a temporary KnowledgeRawStore.
"""

from __future__ import annotations

import os
import tempfile
import textwrap
import unittest

from rikugan.memory.ingest import (
    ingest_exploration_finding,
    ingest_report,
    ingest_research_note,
    ingest_save_memory,
    make_store,
)
from rikugan.tests.knowledge._helpers import fresh_store


class TestMakeStore(unittest.TestCase):
    def test_no_idb_returns_none(self):
        store, paths = make_store("")
        self.assertIsNone(store)
        self.assertIsNone(paths)

    def test_with_idb_returns_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, paths = make_store(os.path.join(tmp, "x.idb"))
            self.assertIsNotNone(store)
            self.assertIsNotNone(paths)


class TestIngestSaveMemory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store, self.paths = fresh_store(self.tmp)

    def test_basic_save(self):
        ingest_save_memory(self.store, self.paths, fact="Crypto uses RC4 at 0x401000", category="crypto")
        mems = self.store.list_memories()
        self.assertEqual(len(mems), 1)
        m = mems[0]
        self.assertEqual(m.category if hasattr(m, "category") else m.type, "crypto")
        self.assertIn("func:0x401000", m.entity_refs)
        # Observation appended
        obs = self.store.list_observations()
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0].kind, "save_memory")

    def test_upsert_idempotent(self):
        for _ in range(3):
            ingest_save_memory(self.store, self.paths, fact="uses RC4", category="crypto")
        # Same content + category → same deterministic ID → 1 memory
        self.assertEqual(len(self.store.list_memories()), 1)


class TestIngestExplorationFinding(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store, self.paths = fresh_store(self.tmp)

    def test_function_purpose_creates_entity_and_relation(self):
        ingest_exploration_finding(
            self.store,
            self.paths,
            category="function_purpose",
            summary="Decrypts buffer with RC4",
            address=0x401000,
            relevance="high",
            function_name="rc4_decrypt",
        )
        ents = self.store.list_entities()
        # At least the function entity
        ids = {e.id for e in ents}
        self.assertIn("func:0x401000", ids)
        mems = self.store.list_memories()
        self.assertEqual(len(mems), 1)
        self.assertEqual(mems[0].type, "function_purpose")
        self.assertEqual(mems[0].status, "unverified")
        self.assertFalse(mems[0].verified)

    def test_no_address_creates_concept_entity(self):
        ingest_exploration_finding(
            self.store,
            self.paths,
            category="hypothesis",
            summary="Uses unpacking",
            address=None,
            relevance="medium",
        )
        mems = self.store.list_memories()
        self.assertEqual(len(mems), 1)
        self.assertIn("concept:", mems[0].entity_refs[0])


class TestIngestResearchNote(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store, self.paths = fresh_store(self.tmp)
        self.note_path = os.path.join(self.paths.notes_dir, "functions", "rc4-decrypt.md")
        os.makedirs(os.path.dirname(self.note_path), exist_ok=True)
        self.content = textwrap.dedent(
            """
            ---
            title: RC4 Decrypt
            genre: functions
            tags: [crypto, rc4]
            addresses: 0x401000
            related: [[key-schedule]]
            ---

            # RC4 Decrypt

            > Addresses: 0x401000
            > Genre: #crypto

            ## Summary

            RC4 keystream decrypts buffer at `0x402000`.
            """
        ).strip()
        with open(self.note_path, "w", encoding="utf-8") as f:
            f.write(self.content)

    def test_ingest_creates_entities_relations_memory(self):
        ingest_research_note(
            self.store,
            self.paths,
            note_path=self.note_path,
            genre="functions",
            title="RC4 Decrypt",
            content=self.content,
            related=["key-schedule"],
            review_passed=True,
        )
        ents = self.store.list_entities()
        ids = {e.id for e in ents}
        self.assertIn("note:rc4-decrypt", ids)
        self.assertIn("func:0x401000", ids)
        self.assertIn("note:key-schedule", ids)
        rels = self.store.list_relations()
        pred_pairs = {(r.src, r.predicate, r.dst) for r in rels}
        self.assertIn(("note:rc4-decrypt", "mentions", "func:0x401000"), pred_pairs)
        self.assertIn(("note:rc4-decrypt", "related_to", "note:key-schedule"), pred_pairs)
        mems = self.store.list_memories()
        self.assertEqual(len(mems), 1)
        m = mems[0]
        self.assertTrue(m.verified)


class TestIngestReport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store, self.paths = fresh_store(self.tmp)

    def test_creates_report_entity_and_memory(self):
        ingest_report(
            self.store,
            self.paths,
            report_path="/notes/reports/report.md",
            slug="report-final",
            scope="full",
            body_excerpt="Executive summary here.",
        )
        ents = self.store.list_entities()
        ids = {e.id for e in ents}
        self.assertIn("report:report-final", ids)
        mems = self.store.list_memories()
        self.assertEqual(len(mems), 1)
        self.assertEqual(mems[0].type, "report")
        obs = self.store.list_observations()
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0].kind, "report_generated")


class TestIngestSilence(unittest.TestCase):
    def test_handles_no_idb_path(self):
        # All ingest functions tolerate (None, None) gracefully.
        ingest_save_memory(None, None, fact="x", category="general")  # type: ignore[arg-type]
        ingest_exploration_finding(None, None, category="general", summary="x", address=None, relevance="low")  # type: ignore[arg-type]
        ingest_research_note(None, None, note_path="x.md", genre="g", title="t", content="c")  # type: ignore[arg-type]


class TestCanonicalIdHelpers(unittest.TestCase):
    """ingest.* must use the canonical entity-ID helpers from paths."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store, self.paths = fresh_store(self.tmp)

    def test_report_entity_id_sanitizes_hostile_slug(self):
        from rikugan.memory.paths import report_entity_id

        hostile_slug = "../etc/passwd"
        ingest_report(
            self.store,
            self.paths,
            report_path="/tmp/x.md",
            slug=hostile_slug,
            scope="full",
            body_excerpt="body",
        )
        # The entity ID for the report must match what
        # ``report_entity_id`` produces for the same slug.
        ents = self.store.list_entities()
        report_ents = [e for e in ents if e.type == "report"]
        self.assertEqual(len(report_ents), 1)
        self.assertEqual(report_ents[0].id, report_entity_id(hostile_slug))

    def test_import_entity_id_sanitizes_unsafe_name(self):
        # ingest_exploration_finding with category "import_usage" and an
        # address falls through the "_entity_id_for" path that builds
        # an "import:unknown:{name}" id. Verify the unsafe characters
        # are sanitized (the old hard-coded f-string skipped that).
        from rikugan.memory.paths import import_entity_id

        # ``address`` is required for the import branch; use a dummy.
        ingest_exploration_finding(
            self.store,
            self.paths,
            category="import_usage",
            summary="calls interesting Win32 API",
            address=0x401000,
            relevance="medium",
            function_name="evil name with spaces & slashes",
        )
        ents = self.store.list_entities()
        import_ents = [e for e in ents if e.type == "import"]
        self.assertEqual(len(import_ents), 1)
        self.assertEqual(import_ents[0].id, import_entity_id("unknown", "evil name with spaces & slashes"))


if __name__ == "__main__":
    unittest.main()
