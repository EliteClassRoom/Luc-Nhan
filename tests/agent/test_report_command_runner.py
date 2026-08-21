"""BackgroundAgentRunner regression for the /report confirmation flow.

Boots a real ``BackgroundAgentRunner`` whose ``run`` method yields
from the real ``_handle_report_command`` generator. Patches the
store/llm/save helpers so the test exercises the real handler
end-to-end through the runner's event queue. Drains the queue,
submits the user answer, and asserts the terminal ``TEXT_DONE``
arrives intact after the ``USER_QUESTION``.

This is the regression that pins the reported defect: the
post-answer ``TEXT_DONE`` must reach the UI queue, not be absorbed
or coalesced away.
"""

from __future__ import annotations

import queue
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from rikugan.agent.loop import AgentLoop, BackgroundAgentRunner
from rikugan.agent.loop_commands import _handle_report_command
from rikugan.agent.turn import TurnEventType
from rikugan.core.config import RikuganConfig
from rikugan.memory.ingest import ingest_save_memory
from rikugan.memory.report import ReportSaveResult, build_report_context
from rikugan.memory.schema import KnowledgeMemory
from rikugan.state.session import SessionState
from rikugan.tests.knowledge._helpers import fresh_store


def _seed_verified_hypothesis(store, paths) -> KnowledgeMemory:
    """Seed a verified hypothesis for the post-restriction /report flow."""
    mem = KnowledgeMemory(
        id="mem:explore:hypothesis:0x401000:abcd",
        binary_id=paths.binary_id,
        type="hypothesis",
        title="RC4 keystream hypothesis",
        content="Beacon encryption uses RC4 at 0x401000",
        status="verified",
        verdict_claim="Confirmed by decompile_function and xref walk",
        verification_citations=["function:rc4_ksa", "address:0x401000"],
        verified=True,
    )
    store.upsert_memory(mem)
    return mem


def _build_test_loop(store, paths) -> AgentLoop:
    """Minimal AgentLoop whose ``run`` only yields from the real /report handler."""
    loop = AgentLoop.__new__(AgentLoop)
    loop.config = RikuganConfig()
    loop.session = SessionState(idb_path=paths.idb_path)
    loop.provider = object()
    loop.tools = MagicMock()
    loop.skills = None
    loop.host_name = "host"
    loop._user_answer_queue = queue.Queue(maxsize=1)
    loop._cancelled = threading.Event()
    loop._wait_for_queue = lambda q: q.get(timeout=5)
    loop.submit_user_answer = lambda ans: loop._user_answer_queue.put(ans)
    loop.cancel = lambda: loop._cancelled.set()

    def _run(user_message: str):
        if user_message.strip().lower().startswith("/report"):
            parts = user_message.split(maxsplit=1)
            scope = parts[1] if len(parts) > 1 else ""
            yield from _handle_report_command(loop, scope)
            return
        return
        yield  # unreachable but keeps this a generator

    loop.run = _run  # type: ignore[assignment]
    return loop

class TestBackgroundAgentRunnerReportFlow(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.store, self.paths = fresh_store(self.tmp)
        self.idb_path = self.paths.idb_path
        _seed_verified_hypothesis(self.store, self.paths)

    def _start_patches(self, save_mock: MagicMock) -> list:
        def _context_for(*_args, **_kwargs):
            return build_report_context(self.store, self.paths, scope="full")

        return [
            patch(
                "rikugan.agent.loop_commands._open_knowledge_store",
                return_value=(self.store, self.paths, None),
            ),
            patch(
                "rikugan.memory.report.build_report_context",
                side_effect=_context_for,
            ),
            patch(
                "rikugan.memory.report.synthesize_report",
                return_value=(_context_for(), "# Draft\n\nverified body"),
            ),
            patch(
                "rikugan.memory.report.save_report",
                side_effect=save_mock,
            ),
        ]

    def _drive(self, runner: BackgroundAgentRunner, loop: AgentLoop, answer: str):
        save_mock = MagicMock(
            return_value=ReportSaveResult(
                file_path="/tmp/saved-report.md",
                ingested=True,
                ingest_error="",
            )
        )
        patches = self._start_patches(save_mock)
        for p in patches:
            p.start()
        try:
            runner.start("/report executive")
            first = runner.get_event(timeout=2.0)
            self.assertIsNotNone(first, "draft TEXT_DONE missing")
            self.assertEqual(first.type, TurnEventType.TEXT_DONE)
            self.assertIn("Report draft", first.text)
            second = runner.get_event(timeout=2.0)
            self.assertIsNotNone(second, "USER_QUESTION missing")
            self.assertEqual(second.type, TurnEventType.USER_QUESTION)
            self.assertEqual(
                second.metadata["options"], ["Write report", "Cancel"]
            )
            blocking = runner.get_event(timeout=0.2)
            self.assertIsNone(
                blocking,
                f"runner returned event while blocked on answer: {blocking!r}",
            )
            submitter = threading.Thread(
                target=loop.submit_user_answer, args=(answer,)
            )
            submitter.start()
            terminal = runner.get_event(timeout=2.0)

            self.assertIsNotNone(terminal, "terminal TEXT_DONE missing after submit")
            self.assertEqual(terminal.type, TurnEventType.TEXT_DONE)
            submitter.join(timeout=1.0)
            self.assertFalse(submitter.is_alive(), "submitter thread did not finish")
            sentinel = runner.get_event(timeout=2.0)
            self.assertIsNone(sentinel, f"unexpected non-sentinel event: {sentinel}")
            return [first, second, terminal]
        finally:
            for p in patches:
                p.stop()

    def test_approval_terminal_arrives_after_submit(self) -> None:
        loop = _build_test_loop(self.store, self.paths)
        runner = BackgroundAgentRunner(loop)
        runner.event_queue = queue.Queue(maxsize=8)
        events = self._drive(runner, loop, "Write report")
        self.assertIn("Report saved", events[2].text)

    def test_cancel_terminal_arrives_after_submit(self) -> None:
        loop = _build_test_loop(self.store, self.paths)
        runner = BackgroundAgentRunner(loop)
        runner.event_queue = queue.Queue(maxsize=8)
        events = self._drive(runner, loop, "Cancel")
        self.assertIn("Report discarded", events[2].text)


if __name__ == "__main__":
    unittest.main()
