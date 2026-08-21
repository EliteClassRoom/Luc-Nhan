"""Tests for the /verify direct command and bounded hypothesis verifier."""

from __future__ import annotations

import json
import queue
import tempfile
import threading
import unittest
from unittest.mock import MagicMock

from rikugan.agent import hypothesis_verification
from rikugan.agent.hypothesis_verification import verify_hypotheses
from rikugan.agent.loop import AgentLoop
from rikugan.agent.loop_commands import _handle_verify_command
from rikugan.agent.turn import TurnEventType
from rikugan.core.config import RikuganConfig
from rikugan.memory.schema import KnowledgeMemory
from rikugan.state.session import SessionState
from rikugan.tests.knowledge._helpers import fresh_store


def _hypothesis(
    mem_id: str,
    summary: str,
    status: str = "unverified",
) -> KnowledgeMemory:
    return KnowledgeMemory(
        id=mem_id,
        binary_id="b1",
        type="hypothesis",
        title=summary.split()[0],
        content=summary,
        status=status,
        verdict_claim="",
        verification_citations=[],
        verified=(status == "verified"),
    )


class TestVerifierContract(unittest.TestCase):
    def test_accepts_valid_verdict(self):
        text = json.dumps(
            {
                "verdicts": [
                    {
                        "id": "mem:a",
                        "status": "verified",
                        "claim": "Decompile confirms the constant.",
                        "citations": ["function:score_handler", "address:0x401000"],
                    }
                ]
            }
        )
        verdicts, _unresolved, err = hypothesis_verification._parse_verifier_response(text, {"mem:a"})
        self.assertIsNone(err)
        self.assertEqual(verdicts["mem:a"].status, "verified")

    def test_rejects_missing_claim(self):
        text = json.dumps(
            {
                "verdicts": [
                    {
                        "id": "mem:a",
                        "status": "verified",
                        "citations": ["function:f"],
                    }
                ]
            }
        )
        _, _unresolved, err = hypothesis_verification._parse_verifier_response(text, {"mem:a"})
        self.assertIsNotNone(err)
        self.assertIn("claim", str(err))

    def test_rejects_empty_citations(self):
        text = json.dumps(
            {
                "verdicts": [
                    {
                        "id": "mem:a",
                        "status": "wrong",
                        "claim": "no",
                        "citations": [],
                    }
                ]
            }
        )
        _, _unresolved, err = hypothesis_verification._parse_verifier_response(text, {"mem:a"})
        self.assertIsNotNone(err)
        self.assertIn("citation", str(err))

    def test_rejects_malformed_citation(self):
        text = json.dumps(
            {
                "verdicts": [
                    {
                        "id": "mem:a",
                        "status": "wrong",
                        "claim": "no",
                        "citations": ["bad-prefix"],
                    }
                ]
            }
        )
        _, _unresolved, err = hypothesis_verification._parse_verifier_response(text, {"mem:a"})
        self.assertIsNotNone(err)
        self.assertIn("malformed citation", str(err))

    def test_rejects_missing_id(self):
        text = json.dumps(
            {
                "verdicts": [
                    {
                        "status": "verified",
                        "claim": "ok",
                        "citations": ["function:f"],
                    }
                ]
            }
        )
        _, _unresolved, err = hypothesis_verification._parse_verifier_response(text, {"mem:a"})
        self.assertIsNotNone(err)
        self.assertIn("missing id", str(err))

    def test_rejects_duplicate_id(self):
        text = json.dumps(
            {
                "verdicts": [
                    {
                        "id": "mem:a",
                        "status": "verified",
                        "claim": "ok",
                        "citations": ["function:f"],
                    },
                    {
                        "id": "mem:a",
                        "status": "wrong",
                        "claim": "bad",
                        "citations": ["function:g"],
                    },
                ]
            }
        )
        _, _unresolved, err = hypothesis_verification._parse_verifier_response(text, {"mem:a"})
        self.assertIsNotNone(err)
        self.assertIn("duplicate", str(err))

    def test_rejects_invalid_status(self):
        text = json.dumps(
            {
                "verdicts": [
                    {
                        "id": "mem:a",
                        "status": "maybe",
                        "claim": "ok",
                        "citations": ["function:f"],
                    }
                ]
            }
        )
        _, _unresolved, err = hypothesis_verification._parse_verifier_response(text, {"mem:a"})
        self.assertIsNotNone(err)
        self.assertIn("invalid status", str(err))

    def test_rejects_unverified_output(self):
        text = json.dumps(
            {
                "verdicts": [
                    {
                        "id": "mem:a",
                        "status": "unverified",
                        "claim": "not allowed",
                        "citations": ["function:f"],
                    }
                ]
            }
        )
        _, _unresolved, err = hypothesis_verification._parse_verifier_response(text, {"mem:a"})
        self.assertIsNotNone(err)
        self.assertIn("invalid status", str(err))


class TestVerifyHypothesesFlow(unittest.TestCase):
    def _build_loop(self) -> AgentLoop:
        loop = AgentLoop.__new__(AgentLoop)
        loop.config = RikuganConfig()
        loop.session = SessionState()
        loop.provider = object()
        loop.tools = MagicMock()
        loop.skills = None
        loop.host_name = "host"
        loop._user_answer_queue = queue.Queue(maxsize=1)
        loop._cancelled = threading.Event()
        loop._memory_authority = None
        return loop

    def _scripted_runner(self, scripts: list[str]):
        queue_ = list(scripts)

        class _R:
            def __init__(self, q):
                self._q = q

            def run_task(self, prompt: str, max_turns: int = 8, silent: bool = True):
                if not self._q:
                    raise AssertionError("scripted responses exhausted")
                text = self._q.pop(0)

                def _gen():
                    return text
                return _gen()

        return _R(queue_)

    def test_bounded_three_attempts_with_invalid_response(self):
        loop = self._build_loop()
        mem = _hypothesis("mem:a", "claim")
        runner = self._scripted_runner(["not json"] * 3)
        result = verify_hypotheses(loop, [mem], runner_factory=lambda: runner)
        self.assertFalse(result.passed)
        self.assertEqual(result.attempts, 3)
        self.assertIn("mem:a", result.unresolved)

    def test_mixed_verified_wrong_batch_commits(self):
        loop = self._build_loop()
        m1 = _hypothesis("mem:a", "claim one")
        m2 = _hypothesis("mem:b", "claim two")
        scripts = [
            json.dumps(
                {
                    "verdicts": [
                        {
                            "id": "mem:a",
                            "status": "verified",
                            "claim": "Confirmed",
                            "citations": ["function:a"],
                        },
                        {
                            "id": "mem:b",
                            "status": "wrong",
                            "claim": "Wrong because constant is elsewhere.",
                            "citations": ["address:0x401000"],
                        },
                    ]
                }
            )
        ]
        runner = self._scripted_runner(scripts)
        result = verify_hypotheses(loop, [m1, m2], runner_factory=lambda: runner)
        self.assertTrue(result.passed)
        self.assertEqual(set(result.verdicts), {"mem:a", "mem:b"})
        self.assertEqual(result.verdicts["mem:a"].status, "verified")
        self.assertEqual(result.verdicts["mem:b"].status, "wrong")


class TestVerifyCommandHandler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store, self.paths = fresh_store(self.tmp)
        self.idb_path = self.paths.idb_path

    def _build_loop(self) -> AgentLoop:
        loop = AgentLoop.__new__(AgentLoop)
        loop.config = RikuganConfig()
        loop.session = SessionState(idb_path=self.idb_path)
        loop.provider = object()
        loop.tools = MagicMock()
        loop.skills = None
        loop.host_name = "host"
        loop._user_answer_queue = queue.Queue(maxsize=1)
        loop._cancelled = threading.Event()
        loop._memory_authority = None
        return loop

    def _seed(self, mem_id: str, status: str = "unverified") -> KnowledgeMemory:
        mem = KnowledgeMemory(
            id=mem_id,
            binary_id=self.paths.binary_id,
            type="hypothesis",
            title=mem_id,
            content="claim",
            status=status,
            verdict_claim="",
            verification_citations=[],
        )
        self.store.upsert_memory(mem)
        return mem

    def test_no_pending_emits_text_event(self):
        loop = self._build_loop()
        with unittest.mock.patch(
            "rikugan.agent.loop_commands._open_knowledge_store",
            return_value=(self.store, self.paths, None),
        ):
            events = list(_handle_verify_command(loop, ""))
        self.assertEqual(len(events), 1)
        self.assertIn("No unverified hypotheses", events[0].text)

    def test_single_id_no_match(self):
        loop = self._build_loop()
        with unittest.mock.patch(
            "rikugan.agent.loop_commands._open_knowledge_store",
            return_value=(self.store, self.paths, None),
        ):
            events = list(_handle_verify_command(loop, "missing-id"))
        self.assertTrue(any("No memory found" in e.text for e in events))

    def test_already_verified_id_is_noop(self):
        loop = self._build_loop()
        self._seed("mem:done", status="verified")
        with unittest.mock.patch(
            "rikugan.agent.loop_commands._open_knowledge_store",
            return_value=(self.store, self.paths, None),
        ):
            events = list(_handle_verify_command(loop, "mem:done"))
        self.assertTrue(any("already verified" in e.text for e in events))

    def test_invalid_batch_emits_error(self):
        loop = self._build_loop()
        self._seed("mem:a")
        bad = MagicMock()
        bad.run_task.side_effect = lambda *a, **k: iter(["not json"])
        with unittest.mock.patch(
            "rikugan.agent.loop_commands._open_knowledge_store",
            return_value=(self.store, self.paths, None),
        ), unittest.mock.patch(
            "rikugan.agent.hypothesis_verification._build_runner",
            lambda _loop: bad,
        ):
            events = list(_handle_verify_command(loop, ""))
        self.assertTrue(any(e.type == TurnEventType.ERROR for e in events))

    def test_valid_batch_emits_verdict_events(self):
        loop = self._build_loop()
        self._seed("mem:a")
        self._seed("mem:b")
        response = json.dumps(
            {
                "verdicts": [
                    {
                        "id": "mem:a",
                        "status": "verified",
                        "claim": "Confirmed.",
                        "citations": ["function:a"],
                    },
                    {
                        "id": "mem:b",
                        "status": "wrong",
                        "claim": "Wrong because of pattern.",
                        "citations": ["address:0x401000"],
                    },
                ]
            }
        )
        good = MagicMock()
        good.run_task.return_value = iter([response])
        with unittest.mock.patch(
            "rikugan.agent.loop_commands._open_knowledge_store",
            return_value=(self.store, self.paths, None),
        ), unittest.mock.patch(
            "rikugan.agent.hypothesis_verification._build_runner",
            lambda _loop: good,
        ):
            events = list(_handle_verify_command(loop, ""))
        verdict_events = [e for e in events if e.type == TurnEventType.HYPOTHESIS_VERDICT]
        self.assertEqual({e.metadata["hypothesis_id"] for e in verdict_events}, {"mem:a", "mem:b"})
        for e in verdict_events:
            self.assertIn("citations", e.metadata)
            self.assertTrue(e.metadata["claim"])
        for hid, expected_status in [("mem:a", "verified"), ("mem:b", "wrong")]:
            stored = next(m for m in self.store.list_memories() if m.id == hid)
            self.assertEqual(stored.status, expected_status)


if __name__ == "__main__":
    unittest.main()



class TestVerifierReadOnlyToolView(unittest.TestCase):
    """The /verify subagent must not have access to mutating tools.

    Prompt text alone does not enforce plan step 3's read-only
    contract; the builder must hand the verifier a tool registry
    that physically omits mutating tools so the registry's
    ``ToolNotFoundError`` path triggers on any attempt to use one.
    """

    def _build_loop_with_tools(self) -> AgentLoop:
        from rikugan.tools.base import ParameterSchema, ToolDefinition
        from rikugan.tools.registry import ToolRegistry

        loop = AgentLoop.__new__(AgentLoop)
        loop.config = RikuganConfig()
        loop.session = SessionState()
        loop.provider = object()
        loop.tools = ToolRegistry()
        # Register one read-only tool and one mutating tool.
        loop.tools.register(
            ToolDefinition(
                name="decompile_function",
                description="read-only decompile",
                parameters=[],
                mutating=False,
            )
        )
        loop.tools.register(
            ToolDefinition(
                name="rename_function",
                description="mutates the IDB",
                parameters=[],
                mutating=True,
            )
        )
        loop.skills = None
        loop.host_name = "host"
        loop._user_answer_queue = queue.Queue(maxsize=1)
        loop._cancelled = threading.Event()
        loop._memory_authority = None
        return loop

    def test_build_runner_strips_mutating_tools(self):
        loop = self._build_loop_with_tools()
        runner = hypothesis_verification._build_runner(loop)
        # The SubagentRunner must hold the read-only view, not the
        # parent's full registry.
        names = set(runner.tools.list_names())
        self.assertIn("decompile_function", names)
        self.assertNotIn(
            "rename_function",
            names,
            "verifier subagent must not be able to invoke mutating tools",
        )

    def test_read_only_view_omits_every_mutating_definition(self):
        from rikugan.tools.base import ParameterSchema, ToolDefinition
        from rikugan.tools.registry import ToolRegistry

        reg = ToolRegistry()
        mutating_names = ["rename_function", "set_comment", "patch_bytes"]
        for n in mutating_names:
            reg.register(
                ToolDefinition(
                    name=n,
                    description="mutates",
                    parameters=[],
                    mutating=True,
                )
            )
        reg.register(
            ToolDefinition(
                name="decompile_function",
                description="read-only",
                parameters=[],
                mutating=False,
            )
        )
        view = reg.read_only_view()
        view_names = set(view.list_names())
        self.assertEqual(view_names, {"decompile_function"})
        for n in mutating_names:
            self.assertNotIn(n, view_names)

    def test_read_only_view_isolated_from_parent(self):
        from rikugan.tools.base import ParameterSchema, ToolDefinition
        from rikugan.tools.registry import ToolRegistry

        reg = ToolRegistry()
        reg.register(
            ToolDefinition(
                name="decompile_function",
                description="read-only",
                parameters=[],
                mutating=False,
            )
        )
        view = reg.read_only_view()
        view.register(
            ToolDefinition(
                name="extra",
                description="view-only",
                parameters=[],
                mutating=False,
            )
        )
        self.assertNotIn("extra", set(reg.list_names()))
        self.assertIn("decompile_function", set(reg.list_names()))
        view.set_capabilities({"fake_cap": True})
        self.assertNotIn("fake_cap", reg._capabilities)
