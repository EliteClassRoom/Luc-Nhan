"""Tests for the verified-explore memory finalizer."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.agent import report_review
from rikugan.agent.exploration_mode import ExplorationState, Finding
from rikugan.agent.modes import exploration as exploration_mode
from rikugan.core.config import RikuganConfig
from rikugan.memory.ingest import _stable_hash, make_store
from rikugan.state.session import SessionState


def _drain(gen):
    events = []
    for ev in gen:
        events.append(ev)
    return events


def _consume(response: str):
    if False:  # pragma: no cover - generator marker
        yield None
    return response


def _scripted_runner_factory(scripts: list[str]):
    queue = list(scripts)

    class _R:
        def __init__(self, q):
            self._q = q
            self.last_session = None

        def run_task(self, prompt: str, max_turns: int = 20, silent: bool = False):
            if not self._q:
                raise AssertionError("scripted responses exhausted")
            return _consume(self._q.pop(0))

    return _R(queue)


def _make_loop(idb_path: str) -> Any:
    loop = MagicMock()
    loop.provider = MagicMock()
    loop.tools = MagicMock()
    loop.config = RikuganConfig()
    loop.session = SessionState(idb_path=idb_path)
    loop.session.messages = []
    loop.host_name = "test"
    loop.skills = None
    loop.memory_service = None
    loop._memory_authority = None
    loop._cancelled = threading.Event()
    return loop


def _state_with(findings: list[Finding]) -> ExplorationState:
    state = ExplorationState(explore_only=True)
    state.knowledge_base.user_goal = "find entry"
    for f in findings:
        state.knowledge_base.add_finding(f)
    return state


def _id_for(category: str, summary: str, address: int | None) -> str:
    cat = (category or "general").strip().lower()
    addr_part = f"0x{int(address):x}" if address is not None else "noaddr"
    return f"mem:explore:{cat}:{addr_part}:{_stable_hash(cat, summary, address)}"


def _passing_response(mem_id: str) -> str:
    return json.dumps(
        {
            "findings": [
                {
                    "id": mem_id,
                    "status": "pass",
                    "evidence": "ok",
                    "confidence": 0.9,
                }
            ]
        }
    )


class TestFinalizeExploreMemory(unittest.TestCase):
    def test_no_findings_yields_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            idb = os.path.join(tmp, "x.idb")
            with open(idb, "w", encoding="utf-8") as fh:
                fh.write("")
            loop = _make_loop(idb)
            state = _state_with([])
            events = _drain(exploration_mode._finalize_explore_memory(loop, state))
            self.assertEqual(events, [])

    def test_hypotheses_only_batch_saves_central_index(self):
        """A batch with only hypotheses must still save the central index.

        The ``review is None`` fallback passes ``empty_review_result()``
        to ``_build_central_index``; a dropped-underscore typo made this
        raise NameError inside the try/except, silently skipping the
        central save.
        """
        with tempfile.TemporaryDirectory() as tmp:
            idb = os.path.join(tmp, "x.idb")
            with open(idb, "w", encoding="utf-8") as fh:
                fh.write("")
            loop = _make_loop(idb)
            loop.memory_service = MagicMock()
            loop._memory_authority = MagicMock()
            state = _state_with(
                [
                    Finding(
                        category="hypothesis",
                        address=0x401000,
                        summary="suspicious call",
                        relevance="high",
                    )
                ]
            )
            _drain(exploration_mode._finalize_explore_memory(loop, state))
            loop.memory_service.save_fact.assert_called_once()

    def test_review_pass_persists_with_deterministic_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            idb = os.path.join(tmp, "x.idb")
            with open(idb, "w", encoding="utf-8") as fh:
                fh.write("")
            loop = _make_loop(idb)
            state = _state_with(
                [
                    Finding(
                        category="function_purpose",
                        address=0x401000,
                        summary="entry",
                        relevance="high",
                    )
                ]
            )
            mem_id = _id_for("function_purpose", "entry", 0x401000)
            runner = _scripted_runner_factory([_passing_response(mem_id)])
            with patch.object(report_review, "_build_runner", lambda _loop: runner):
                events = _drain(exploration_mode._finalize_explore_memory(loop, state))
            self.assertTrue(any("reviewing" in (e.text or "").lower() for e in events))
            store, _ = make_store(idb)
            ids = {m.id for m in store.list_memories()}
            self.assertIn(mem_id, ids)

    def test_review_failure_keeps_hypotheses_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            idb = os.path.join(tmp, "x.idb")
            with open(idb, "w", encoding="utf-8") as fh:
                fh.write("")
            loop = _make_loop(idb)
            state = _state_with(
                [
                    Finding(
                        category="hypothesis",
                        address=None,
                        summary="guess",
                        relevance="medium",
                    )
                ]
            )
            # A pure-hypothesis batch never invokes the reviewer; we
            # still expect a no-op event sequence and the hypothesis
            # to land in the raw store as ``unverified``.
            runner = _scripted_runner_factory(["unused"])
            with patch.object(report_review, "_build_runner", lambda _loop: runner):
                events = _drain(exploration_mode._finalize_explore_memory(loop, state))
            # No error event: the hypothesis path is independent of
            # the reviewer.
            error_events = [e for e in events if e.type.value == "error"]
            self.assertFalse(error_events)
            store, _ = make_store(idb)
            ids = {m.id for m in store.list_memories()}
            hyp_id = _id_for("hypothesis", "guess", None)
            self.assertIn(hyp_id, ids)
            stored = next(m for m in store.list_memories() if m.id == hyp_id)
            self.assertEqual(stored.status, "unverified")

    def test_central_only_message_when_raw_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            idb = os.path.join(tmp, "x.idb")
            # No IDB file is created so make_store returns (None, None)
            # and the finalizer must fall back to central memory only.
            loop = _make_loop(idb)
            state = _state_with(
                [
                    Finding(
                        category="function_purpose",
                        address=0x401000,
                        summary="entry",
                        evidence="ok",
                        relevance="high",
                    )
                ]
            )
            mem_id = _id_for("function_purpose", "entry", 0x401000)
            fake_service = MagicMock()
            fake_service.save_fact.return_value = None
            loop.memory_service = fake_service
            loop._memory_authority = MagicMock()
            runner = _scripted_runner_factory([_passing_response(mem_id)])
            with (
                patch.object(report_review, "_build_runner", lambda _loop: runner),
                patch("rikugan.memory.ingest.make_store", return_value=(None, None)),
            ):
                events = _drain(exploration_mode._finalize_explore_memory(loop, state))
            system_msgs = [e for e in events if e.text and "central index only" in e.text]
            self.assertTrue(
                system_msgs,
                "expected central-only system message when raw store unavailable",
            )
            fake_service.save_fact.assert_called_once()
            kwargs = fake_service.save_fact.call_args.kwargs
            self.assertEqual(kwargs["category"], "exploration_index")
            self.assertIn("addr=0x401000", kwargs["fact"])
            self.assertIn("evidence=ok", kwargs["fact"])
            self.assertIn(mem_id, kwargs["fact"])

    def test_id_alignment_after_correction(self):
        """Plan §5.43 alignment: when review rewrites mem.content,
        the raw-store memory id and the central-index id must both
        equal the original candidate id. Regression-guard for the
        prebuilt-memory_id path.
        """
        with tempfile.TemporaryDirectory() as tmp:
            idb = os.path.join(tmp, "x.idb")
            with open(idb, "w", encoding="utf-8") as fh:
                fh.write("")
            loop = _make_loop(idb)
            state = _state_with(
                [
                    Finding(
                        category="function_purpose",
                        address=0x401000,
                        summary="entry",
                        relevance="high",
                    )
                ]
            )
            candidate_id = _id_for("function_purpose", "entry", 0x401000)
            fake_service = MagicMock()
            fake_service.save_fact.return_value = None
            loop.memory_service = fake_service
            loop._memory_authority = MagicMock()
            corrected_summary = "verified entry claim"
            runner = _scripted_runner_factory(
                [
                    json.dumps(
                        {
                            "findings": [
                                {
                                    "id": candidate_id,
                                    "status": "fail",
                                    "evidence": "tools contradict",
                                    "corrected_content": corrected_summary,
                                    "confidence": 0.95,
                                }
                            ]
                        }
                    ),
                    json.dumps(
                        {
                            "findings": [
                                {
                                    "id": candidate_id,
                                    "status": "fail",
                                    "evidence": "tool-corrected",
                                    "corrected_content": corrected_summary,
                                    "confidence": 0.95,
                                }
                            ]
                        }
                    ),
                    json.dumps(
                        {
                            "findings": [
                                {
                                    "id": candidate_id,
                                    "status": "pass",
                                    "evidence": "ok now",
                                    "confidence": 0.95,
                                }
                            ]
                        }
                    ),
                ]
            )
            with patch.object(report_review, "_build_runner", lambda _loop: runner):
                _drain(exploration_mode._finalize_explore_memory(loop, state))
            # Raw store must contain a record whose id equals the
            # prebuilt candidate id.
            store, _ = make_store(idb)
            ids = {m.id for m in store.list_memories()}
            self.assertIn(candidate_id, ids)
            # Central index must reference the same id.
            self.assertTrue(fake_service.save_fact.called)
            kwargs = fake_service.save_fact.call_args.kwargs
            self.assertIn(candidate_id, kwargs["fact"])
            # The content in the index should reflect the corrected
            # claim, not the original finding.
            self.assertIn(corrected_summary, kwargs["fact"])

    def test_sync_wrapper_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            idb = os.path.join(tmp, "x.idb")
            with open(idb, "w", encoding="utf-8") as fh:
                fh.write("")
            loop = _make_loop(idb)
            state = _state_with(
                [
                    Finding(
                        category="function_purpose",
                        address=0x401000,
                        summary="entry",
                        relevance="high",
                    )
                ]
            )
            mem_id = _id_for("function_purpose", "entry", 0x401000)
            runner = _scripted_runner_factory([_passing_response(mem_id)])
            with patch.object(report_review, "_build_runner", lambda _loop: runner):
                persisted, message = exploration_mode.finalize_explore_memory(loop, state)
            self.assertTrue(persisted)
            self.assertIn("saved", message.lower())

    def test_id_alignment_after_correction(self):
        """Plan §5.43 alignment: when review rewrites mem.content,
        mem.title, or mem.confidence, the raw-store record and the
        central-index fact must both reference the prebuilt candidate
        id and retain the corrected title and confidence.
        """
        with tempfile.TemporaryDirectory() as tmp:
            idb = os.path.join(tmp, "x.idb")
            with open(idb, "w", encoding="utf-8") as fh:
                fh.write("")
            loop = _make_loop(idb)
            state = _state_with(
                [
                    Finding(
                        category="function_purpose",
                        address=0x401000,
                        summary="entry",
                        relevance="high",
                    )
                ]
            )
            candidate_id = _id_for("function_purpose", "entry", 0x401000)
            fake_service = MagicMock()
            fake_service.save_fact.return_value = None
            loop.memory_service = fake_service
            loop._memory_authority = MagicMock()
            corrected_summary = "verified entry claim"
            corrected_title = "renamed entry"
            corrected_confidence = 0.97
            runner = _scripted_runner_factory(
                [
                    json.dumps(
                        {
                            "findings": [
                                {
                                    "id": candidate_id,
                                    "status": "fail",
                                    "evidence": "tools contradict",
                                    "corrected_content": corrected_summary,
                                    "corrected_title": corrected_title,
                                    "confidence": corrected_confidence,
                                }
                            ]
                        }
                    ),
                    json.dumps(
                        {
                            "findings": [
                                {
                                    "id": candidate_id,
                                    "status": "fail",
                                    "evidence": "tool-corrected",
                                    "corrected_content": corrected_summary,
                                    "corrected_title": corrected_title,
                                    "confidence": corrected_confidence,
                                }
                            ]
                        }
                    ),
                    json.dumps(
                        {
                            "findings": [
                                {
                                    "id": candidate_id,
                                    "status": "pass",
                                    "evidence": "ok now",
                                    "confidence": corrected_confidence,
                                }
                            ]
                        }
                    ),
                ]
            )
            with patch.object(report_review, "_build_runner", lambda _loop: runner):
                _drain(exploration_mode._finalize_explore_memory(loop, state))
            store, _ = make_store(idb)
            ids = {m.id for m in store.list_memories()}
            self.assertIn(candidate_id, ids)
            self.assertTrue(fake_service.save_fact.called)
            kwargs = fake_service.save_fact.call_args.kwargs
            self.assertIn(candidate_id, kwargs["fact"])
            self.assertIn(corrected_summary, kwargs["fact"])
            # Raw store must persist reviewer-corrected title and
            # confidence (Plan §5.43), not the relevance-derived
            # defaults.
            stored = next(
                (m for m in store.list_memories() if m.id == candidate_id),
                None,
            )
            self.assertIsNotNone(stored)
            self.assertIn(corrected_summary, stored.content)
            self.assertEqual(stored.title, corrected_title)
            self.assertEqual(stored.confidence, corrected_confidence)


if __name__ == "__main__":
    unittest.main()
