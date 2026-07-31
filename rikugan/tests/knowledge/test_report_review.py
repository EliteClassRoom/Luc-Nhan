"""Tests for the verified-only review pipeline."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rikugan.agent import report_review
from rikugan.agent.report_review import ReviewResult, review_memories
from rikugan.core.config import RikuganConfig
from rikugan.memory.ingest import make_store
from rikugan.memory.schema import KnowledgeMemory
from rikugan.state.session import SessionState


def _memory(mem_id: str, content: str = "claim", *, verified: bool = False) -> KnowledgeMemory:
    return KnowledgeMemory(
        id=mem_id,
        binary_id="b1",
        type="general",
        title=mem_id,
        content=content,
        confidence=0.5,
        verified=verified,
    )


class _ScriptedRunner:
    """SubagentRunner replacement that pops responses from a shared queue."""

    def __init__(self, queue: list[str]):
        self._queue = queue
        self.last_session = None
        self.calls = 0

    def run_task(self, prompt: str, max_turns: int = 20, silent: bool = False):
        if not self._queue:
            raise AssertionError("scripted responses exhausted")
        return _consume(self._queue.pop(0))


def _consume(response: str):
    if False:  # pragma: no cover - generator marker
        yield None
    return response


def _make_loop() -> MagicMock:
    loop = MagicMock()
    loop.provider = MagicMock()
    loop.tools = MagicMock()
    loop.config = RikuganConfig()
    loop.session = SessionState()
    loop.host_name = "test"
    loop.skills = None
    return loop


def _drive_review(loop, candidates, *, scripts, max_cycles=3):
    """Drive ``review_memories`` with one shared queue per script list."""
    queue: list[str] = list(scripts)
    runner = _ScriptedRunner(queue)

    def _factory(_loop):
        return runner

    with patch.object(report_review, "_build_runner", _factory):
        result = review_memories(loop, candidates, max_cycles=max_cycles)
    return result, runner


class TestParseResponse(unittest.TestCase):
    def test_complete_pass(self):
        raw = json.dumps(
            {
                "findings": [
                    {
                        "id": "a",
                        "status": "pass",
                        "evidence": "tools confirm",
                        "confidence": 0.9,
                    }
                ]
            }
        )
        findings, unresolved, err = report_review._parse_review_response(raw, {"a"})
        self.assertIsNone(err)
        self.assertEqual(findings["a"]["status"], "pass")
        self.assertEqual(unresolved, {})

    def test_missing_id_marked_unresolved(self):
        raw = json.dumps(
            {
                "findings": [
                    {
                        "id": "a",
                        "status": "pass",
                        "evidence": "ok",
                        "confidence": 0.5,
                    }
                ]
            }
        )
        _, unresolved, err = report_review._parse_review_response(raw, {"a", "b"})
        self.assertIsNotNone(err)
        self.assertIn("b", unresolved)

    def test_duplicate_id_rejected(self):
        raw = json.dumps(
            {
                "findings": [
                    {
                        "id": "a",
                        "status": "pass",
                        "evidence": "ok",
                        "confidence": 0.5,
                    },
                    {
                        "id": "a",
                        "status": "pass",
                        "evidence": "dup",
                        "confidence": 0.5,
                    },
                ]
            }
        )
        _, _, err = report_review._parse_review_response(raw, {"a"})
        self.assertIsNotNone(err)
        self.assertIn("duplicate", err)

    def test_unknown_id_rejected(self):
        raw = json.dumps(
            {
                "findings": [
                    {
                        "id": "z",
                        "status": "pass",
                        "evidence": "ok",
                        "confidence": 0.5,
                    }
                ]
            }
        )
        _, _, err = report_review._parse_review_response(raw, {"a"})
        self.assertIsNotNone(err)
        self.assertIn("unknown", err)

    def test_fail_requires_corrected_content(self):
        raw = json.dumps(
            {
                "findings": [
                    {
                        "id": "a",
                        "status": "fail",
                        "evidence": "no match",
                        "confidence": 0.1,
                    }
                ]
            }
        )
        _, _, err = report_review._parse_review_response(raw, {"a"})
        self.assertIsNotNone(err)
        self.assertIn("corrected_content", err)

    def test_out_of_range_confidence_rejected(self):
        raw = json.dumps(
            {
                "findings": [
                    {
                        "id": "a",
                        "status": "pass",
                        "evidence": "ok",
                        "confidence": 1.5,
                    }
                ]
            }
        )
        _, _, err = report_review._parse_review_response(raw, {"a"})
        self.assertIsNotNone(err)

    def test_malformed_json(self):
        _, _, err = report_review._parse_review_response("not json", {"a"})
        self.assertIsNotNone(err)


class TestReviewPipeline(unittest.TestCase):
    def test_pass_first_cycle(self):
        candidates = [_memory("a"), _memory("b")]
        raw = json.dumps(
            {
                "findings": [
                    {"id": "a", "status": "pass", "evidence": "ok", "confidence": 0.9},
                    {"id": "b", "status": "pass", "evidence": "ok", "confidence": 0.9},
                ]
            }
        )
        result, _ = _drive_review(_make_loop(), candidates, scripts=[raw])
        self.assertTrue(result.passed)
        self.assertEqual(result.unresolved, {})

    def test_correction_then_pass(self):
        candidates = [_memory("a"), _memory("b")]
        first_fail = json.dumps(
            {
                "findings": [
                    {
                        "id": "a",
                        "status": "fail",
                        "evidence": "wrong",
                        "corrected_content": "fixed",
                        "confidence": 0.7,
                    },
                    {
                        "id": "b",
                        "status": "pass",
                        "evidence": "ok",
                        "confidence": 0.9,
                    },
                ]
            }
        )
        corrected = json.dumps(
            {
                "findings": [
                    {
                        "id": "a",
                        "status": "fail",
                        "evidence": "tool-corrected",
                        "corrected_content": "verified claim",
                        "confidence": 0.95,
                    }
                ]
            }
        )
        second_pass = json.dumps(
            {
                "findings": [
                    {
                        "id": "a",
                        "status": "pass",
                        "evidence": "ok now",
                        "confidence": 0.95,
                    },
                    {
                        "id": "b",
                        "status": "pass",
                        "evidence": "ok now",
                        "confidence": 0.95,
                    },
                ]
            }
        )
        result, _ = _drive_review(
            _make_loop(), candidates, scripts=[first_fail, corrected, second_pass]
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.cycles, 2)
        self.assertEqual(result.unresolved, {})
        self.assertEqual(result.records[0].content, "verified claim")

    def test_hard_stop_after_three_cycles(self):
        candidates = [_memory("a")]
        fail = json.dumps(
            {
                "findings": [
                    {
                        "id": "a",
                        "status": "fail",
                        "evidence": "still wrong",
                        "corrected_content": "x",
                        "confidence": 0.5,
                    }
                ]
            }
        )
        result, _ = _drive_review(
            _make_loop(), candidates, scripts=[fail, fail, fail, fail, fail, fail]
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.cycles, 3)
        self.assertIn("a", result.unresolved)

    def test_malformed_response_invalidates_cycle(self):
        candidates = [_memory("a")]
        result, _ = _drive_review(
            _make_loop(), candidates, scripts=["not json"] * 6
        )
        self.assertFalse(result.passed)
        self.assertIn("a", result.unresolved)
        self.assertIn("JSON", result.unresolved["a"])


class TestPersistReviewedMemories(unittest.TestCase):
    def test_persist_requires_pass(self):
        with self.assertRaises(ValueError):
            report_review.persist_reviewed_memories(
                MagicMock(), ReviewResult(passed=False, records=[], unresolved={}, cycles=1)
            )

    def test_persist_marks_verified_and_skips_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = make_store(tmp)
            a = _memory("a", verified=False)
            r = _memory("r", verified=False, content="report body")
            r.type = "report"
            result = ReviewResult(passed=True, records=[a, r], unresolved={}, cycles=1)
            count = report_review.persist_reviewed_memories(store, result)
            self.assertEqual(count, 1)  # report record skipped
            ids = {m.id for m in store.list_memories()}
            self.assertIn("a", ids)
            self.assertNotIn("r", ids)


if __name__ == "__main__":
    unittest.main()
