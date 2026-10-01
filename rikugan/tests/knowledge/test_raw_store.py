"""Tests for rikugan.memory.raw_store.

Exercises append/upsert/list/malformed-line tolerance and the file
plumbing used by the rest of the memory package.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from rikugan.memory.paths import KnowledgePaths, knowledge_paths
from rikugan.memory.raw_store import KnowledgeRawStore
from rikugan.memory.schema import (
    KnowledgeEntity,
    KnowledgeMemory,
    KnowledgeObservation,
    KnowledgeRelation,
)


def make_paths(tmp: str) -> KnowledgePaths:
    paths = knowledge_paths(os.path.join(tmp, "fake.idb"))
    paths.ensure()
    return paths


def make_mem(mem_id: str = "mem:test:001", **kwargs) -> KnowledgeMemory:
    base = dict(
        id=mem_id,
        binary_id="fake-123",
        type="fact",
        title="t",
        content="c",
    )
    base.update(kwargs)
    return KnowledgeMemory(**base)


def make_ent(eid: str = "func:0x401000", **kwargs) -> KnowledgeEntity:
    base = dict(
        id=eid,
        binary_id="fake-123",
        type="function",
        name="sub_401000",
    )
    base.update(kwargs)
    return KnowledgeEntity(**base)


def make_rel(rid: str | None = None, **kwargs) -> KnowledgeRelation:
    base = dict(
        id=rid or "rel:func:0x401000:calls:func:0x401100",
        binary_id="fake-123",
        src="func:0x401000",
        predicate="calls",
        dst="func:0x401100",
    )
    base.update(kwargs)
    return KnowledgeRelation(**base)


class TestUpsertAppend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.paths = make_paths(self.tmp)
        self.store = KnowledgeRawStore(self.paths)

    def test_upsert_memory_idempotent(self):
        m = make_mem(content="v1")
        self.store.upsert_memory(m)
        self.store.upsert_memory(make_mem(content="v2", confidence=0.9))
        mems = self.store.list_memories()
        self.assertEqual(len(mems), 1)
        self.assertEqual(mems[0].content, "v2")
        self.assertAlmostEqual(mems[0].confidence, 0.9)

    def test_append_memories_are_both_kept_when_ids_differ(self):
        self.store.upsert_memory(make_mem(mem_id="mem:a"))
        self.store.upsert_memory(make_mem(mem_id="mem:b"))
        self.assertEqual(len(self.store.list_memories()), 2)

    def test_upsert_entity(self):
        self.store.upsert_entity(make_ent(name="old"))
        self.store.upsert_entity(make_ent(name="new", address="0x401000"))
        ents = self.store.list_entities()
        self.assertEqual(len(ents), 1)
        self.assertEqual(ents[0].name, "new")

    def test_get_entity_found(self):
        self.store.upsert_entity(make_ent())
        ent = self.store.get_entity("func:0x401000")
        self.assertIsNotNone(ent)
        self.assertEqual(ent.name, "sub_401000")

    def test_get_entity_missing(self):
        self.assertIsNone(self.store.get_entity("nope"))

    def test_upsert_relation_from_helper(self):
        self.store.upsert_relation_from("func:0x401000", "calls", "func:0x401100")
        rels = self.store.list_relations()
        self.assertEqual(len(rels), 1)
        self.assertEqual(rels[0].predicate, "calls")
        # Same triple → upsert to single record
        self.store.upsert_relation_from("func:0x401000", "calls", "func:0x401100", confidence=0.99)
        self.assertEqual(len(self.store.list_relations()), 1)
        self.assertAlmostEqual(self.store.list_relations()[0].confidence, 0.99)

    def test_observation_append_only(self):
        obs = KnowledgeObservation(
            id="obs-1",
            binary_id="fake-123",
            ts="2026-01-01T00:00:00Z",
            kind="save_memory",
            payload={"x": 1},
        )
        self.store.append_observation(obs)
        self.store.append_observation(obs)
        # Same id but append-only file keeps both lines
        self.assertEqual(len(self.store.list_observations()), 2)

    def test_counts(self):
        self.store.upsert_memory(make_mem())
        self.store.upsert_entity(make_ent())
        self.store.upsert_relation_from("func:0x401000", "calls", "func:0x401100")
        c = self.store.counts()
        self.assertEqual(c["memories"], 1)
        self.assertEqual(c["entities"], 1)
        self.assertEqual(c["relations"], 1)
        self.assertEqual(c["observations"], 0)


class TestMalformedJsonl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.paths = make_paths(self.tmp)
        self.store = KnowledgeRawStore(self.paths)

    def test_malformed_lines_skipped(self):
        # Write a junk line followed by a valid record.
        with open(self.paths.memories_path, "a", encoding="utf-8") as f:
            f.write("{this is not json}\n")
            f.write(json.dumps({"id": "mem:ok", "binary_id": "x", "type": "fact", "title": "t", "content": "c"}))
            f.write("\n")
            f.write("")  # blank line
            f.write("\n")
        mems = self.store.list_memories()
        self.assertEqual(len(mems), 1)
        self.assertEqual(mems[0].id, "mem:ok")

    def test_missing_files_returns_empty(self):
        # Delete one file's parent to confirm readers don't crash.
        self.assertEqual(self.store.list_memories(), [])


class TestAtomicWrite(unittest.TestCase):
    def test_no_temp_files_leaked(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(tmp)
            store = KnowledgeRawStore(paths)
            for i in range(5):
                store.upsert_memory(make_mem(mem_id=f"mem:{i}"))
            leftovers = [n for n in os.listdir(paths.kb_dir) if n.startswith(".rikugan-tmp-")]
            self.assertEqual(leftovers, [])


class TestThreadSafety(unittest.TestCase):
    def test_concurrent_upserts_no_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(tmp)
            store = KnowledgeRawStore(paths)
            errors: list[Exception] = []

            def worker(start: int):
                try:
                    for i in range(start, start + 10):
                        store.upsert_memory(make_mem(mem_id=f"mem:w{threading.get_ident()}:{i}"))
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=worker, args=(i * 100,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            # 4 workers * 10 records = 40 unique ids, all retained.
            mems = store.list_memories()
            self.assertEqual(len(mems), 40)


class TestCommitHypothesisVerdicts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.paths = make_paths(self.tmp)
        self.store = KnowledgeRawStore(self.paths)

    def _updated(self, mem_id: str = "mem:hyp:1") -> KnowledgeMemory:
        return KnowledgeMemory(
            id=mem_id,
            binary_id="fake-123",
            type="hypothesis",
            title="t",
            content="c",
            status="verified",
            verdict_claim="Confirmed.",
            verification_citations=["address:0x401000"],
            verified=True,
        )

    def _obs(self, obs_id: str) -> KnowledgeObservation:
        return KnowledgeObservation(
            id=obs_id,
            binary_id="fake-123",
            ts="2026-08-17T00:00:00Z",
            kind="hypothesis_verified",
            payload={"memory_id": "mem:hyp:1"},
        )

    def test_commit_hypothesis_verdicts_upserts_and_appends(self):
        self.store.upsert_memory(make_mem(mem_id="mem:hyp:1"))
        self.store.commit_hypothesis_verdicts(
            {"mem:hyp:1": self._updated()}, [self._obs("obs:test1")]
        )
        stored = next(m for m in self.store.list_memories() if m.id == "mem:hyp:1")
        self.assertEqual(stored.status, "verified")
        self.assertEqual(stored.verdict_claim, "Confirmed.")
        self.assertEqual(list(stored.verification_citations), ["address:0x401000"])
        self.assertTrue(any(o.id == "obs:test1" for o in self.store.list_observations()))

    def test_commit_hypothesis_verdicts_rolls_back_both_files_on_failure(self):
        self.store.upsert_memory(make_mem(mem_id="mem:hyp:1"))
        real_write = self.store._write_jsonl_atomic

        def flaky(path, records):
            if path == str(self.store.paths.observations_path):
                raise OSError("disk full")
            return real_write(path, records)

        with patch.object(type(self.store), "_write_jsonl_atomic", staticmethod(flaky)):
            with self.assertRaises(OSError):
                self.store.commit_hypothesis_verdicts(
                    {"mem:hyp:1": self._updated()}, [self._obs("obs:test2")]
                )
        stored = next(m for m in self.store.list_memories() if m.id == "mem:hyp:1")
        self.assertEqual(stored.status, "unverified")  # snapshot restored
        self.assertEqual(self.store.list_observations(), [])


if __name__ == "__main__":
    unittest.main()
