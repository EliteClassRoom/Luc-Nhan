"""Parallel ``spawn_subagent`` fan-out: concurrency, ordering, cancellation.

No real LLM. Children are driven either by a fake manager (deterministic
ordering/aggregation assertions) or by stub runners on real manager
threads (the concurrency-overlap proof).
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from lucnhan.agent.loop import AgentLoop
from lucnhan.agent.subagent_manager import SubagentManager, SubagentStatus
from lucnhan.agent.turn import TurnEvent, TurnEventType
from lucnhan.core.config import LucNhanConfig
from lucnhan.core.errors import CancellationError
from lucnhan.core.types import ToolCall
from lucnhan.providers.base import LLMProvider, ModelInfo, ProviderCapabilities
from lucnhan.state.session import SessionState
from lucnhan.tools.base import ParameterSchema, ToolDefinition
from lucnhan.tools.registry import ToolRegistry


class _StubProvider(LLMProvider):
    """Provider stub — the children never reach it in these tests."""

    def __init__(self) -> None:
        super().__init__(api_key="test", model="stub-model")

    @property
    def name(self) -> str:
        return "stub"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    def _get_client(self):  # pragma: no cover - never called
        return None

    def _fetch_models_live(self) -> list[ModelInfo]:  # pragma: no cover
        return []

    @staticmethod
    def _builtin_models() -> list[ModelInfo]:  # pragma: no cover
        return []

    def _format_messages(self, messages):  # pragma: no cover
        return messages

    def _normalize_response(self, raw):  # pragma: no cover
        return raw

    def _build_request_kwargs(self, messages, tools, **kwargs):  # pragma: no cover
        return {}

    def _call_api(self, client, kwargs):  # pragma: no cover
        return {}

    def _handle_api_error(self, e):  # pragma: no cover
        raise e

    def _stream_chunks(self, client, kwargs):  # pragma: no cover
        return iter([])


def _make_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="noop_tool",
            description="does nothing",
            parameters=[ParameterSchema(name="x", type="string")],
            handler=lambda name="": "ok",
        )
    )
    return registry


def _spawn_call(task: str, call_id: str) -> ToolCall:
    return ToolCall(id=call_id, name="spawn_subagent", arguments={"task": task})


class _RecordingRunner:
    """Stub runner recording construction-time flags, completing instantly."""

    def __init__(self, summary: str, log: list[dict]) -> None:
        self._summary = summary
        self._log = log
        self.last_session = None

    def run_task(self, task, max_turns=20, system_addendum=""):
        self._log.append({"task": task, "max_turns": max_turns})
        yield TurnEvent.text_done(self._summary)


class _FakeManager(SubagentManager):
    """Registers children and completes them synchronously via update_external.

    Ordering and aggregation assertions must not depend on thread
    scheduling, so this path never starts a worker thread.
    """

    def __init__(self) -> None:
        super().__init__(
            provider=_StubProvider(),
            tool_registry=_make_registry(),
            config=LucNhanConfig(),
            host_name="test",
        )
        self.spawned: list[tuple[str, Any]] = []
        self._counter = 0

    def spawn(self, name: str, task: str, **kwargs) -> str:  # type: ignore[override]
        self._counter += 1
        self.spawned.append((task, kwargs.get("runner")))
        info = super().register(name=name, task=task, agent_type=kwargs.get("agent_type", "custom"))
        self.update_external(info, SubagentStatus.COMPLETED, summary=f"done-{self._counter}")
        return info

    def cancel(self, agent_id: str) -> None:  # pragma: no cover - asserted elsewhere
        self.update_external(agent_id, SubagentStatus.CANCELLED, summary="cancelled")


def _make_manager(cls=SubagentManager) -> SubagentManager:
    """Build a manager of *cls* with stub dependencies."""
    return cls(
        provider=_StubProvider(),
        tool_registry=_make_registry(),
        config=LucNhanConfig(),
        host_name="test",
    )


def _make_loop(manager: SubagentManager | None = None) -> AgentLoop:
    loop = AgentLoop(
        provider=_StubProvider(),
        tool_registry=_make_registry(),
        config=LucNhanConfig(),
        session=SessionState(),
        subagent_manager=manager,
    )
    return loop


class TestParallelDispatchAndOrdering(unittest.TestCase):
    """A batch of spawn calls fans out and joins in tool-call order."""

    def test_two_spawns_produce_ordered_results(self) -> None:
        manager = _FakeManager()
        loop = _make_loop(manager)
        calls = [
            _spawn_call("task one", "c1"),
            _spawn_call("task two", "c2"),
        ]

        results = []
        events = []
        gen = loop._execute_tool_calls(calls)
        try:
            while True:
                events.append(gen.send(None))
        except StopIteration as stop:
            results = stop.value

        # Both children were dispatched to the manager.
        assert [task for task, _ in manager.spawned] == ["task one", "task two"]
        # Results come back in tool-call order, not completion order.
        assert [tr.tool_call_id for tr in results] == ["c1", "c2"]
        # Content arrives wrapped in the anti-injection envelope.
        assert "done-1" in results[0].content and "done-2" in results[1].content
        assert 'name="spawn_subagent"' in results[0].content
        assert all(tr.is_error is False for tr in results)

        tool_results = [e for e in events if e.type == TurnEventType.TOOL_RESULT]
        assert [e.tool_call_id for e in tool_results] == ["c1", "c2"]

    def test_single_spawn_keeps_attended_child(self) -> None:
        """One spawn in a batch is not parallel — attended behaviour preserved."""
        manager = _FakeManager()
        loop = _make_loop(manager)
        gen = loop._execute_tool_calls([_spawn_call("solo", "c1")])
        try:
            while True:
                gen.send(None)
        except StopIteration as stop:
            results = stop.value
        assert len(results) == 1 and "done-1" in results[0].content

    def test_missing_task_is_inline_error(self) -> None:
        """Validation failures short-circuit without touching the manager."""
        manager = _FakeManager()
        loop = _make_loop(manager)
        bad = ToolCall(id="bad", name="spawn_subagent", arguments={})
        gen = loop._execute_tool_calls([bad])
        events = []
        try:
            while True:
                events.append(gen.send(None))
        except StopIteration as stop:
            results = stop.value
        assert manager.spawned == []
        assert len(results) == 1
        assert results[0].is_error is True
        assert "task" in results[0].content
        assert any(e.type == TurnEventType.TOOL_RESULT for e in events)


class TestUnattendedPropagation(unittest.TestCase):
    """Parallel children are forced unattended; a solo spawn is not."""

    def _capture_runners(self, calls_count: int) -> tuple[list[dict], _FakeManager]:
        manager = _FakeManager()
        loop = _make_loop(manager)
        seen: list[dict] = []
        original = loop.__class__._handle_spawn_subagent_tool

        calls = [_spawn_call(f"t{i}", f"c{i}") for i in range(calls_count)]

        # Intercept runner construction so we can read the flags the loop chose.
        import lucnhan.agent.loop as loop_mod

        real_runner_cls = loop_mod.SubagentRunner

        def fake_runner(**kwargs):
            seen.append(kwargs)
            return _RecordingRunner(f"done-{len(seen)}", seen)

        loop_mod.SubagentRunner = fake_runner
        try:
            gen = loop._execute_tool_calls(calls)
            try:
                while True:
                    gen.send(None)
            except StopIteration:
                pass
        finally:
            loop_mod.SubagentRunner = real_runner_cls
        assert original is not None
        return seen, manager

    def test_batch_of_two_is_unattended(self) -> None:
        seen, manager = self._capture_runners(2)
        assert len(seen) == 2
        assert all(kwargs.get("unattended") is True for kwargs in seen)
        assert len(manager.spawned) == 2

    def test_batch_of_one_is_not_forced_unattended(self) -> None:
        seen, manager = self._capture_runners(1)
        assert len(seen) == 1
        assert seen[0].get("unattended") is False
        assert len(manager.spawned) == 1

    def test_shared_manager_forwarded_to_runner(self) -> None:
        """A loop with an injected manager passes it down to the runner."""
        manager = _FakeManager()
        loop = _make_loop(manager)
        seen: list[dict] = []
        import lucnhan.agent.loop as loop_mod

        real_runner_cls = loop_mod.SubagentRunner

        def fake_runner(**kwargs):
            seen.append(kwargs)
            return _RecordingRunner("ok", seen)

        loop_mod.SubagentRunner = fake_runner
        try:
            gen = loop._execute_tool_calls([_spawn_call("x", "c1")])
            try:
                while True:
                    gen.send(None)
            except StopIteration:
                pass
        finally:
            loop_mod.SubagentRunner = real_runner_cls

        assert seen[0].get("subagent_manager") is manager


class TestConcurrencyOverlap(unittest.TestCase):
    """Real threads: children overlap in time instead of serializing."""

    def test_children_run_concurrently(self) -> None:
        manager = _make_manager()
        intervals: dict[str, tuple[float, float]] = {}
        lock = threading.Lock()

        class _SlowRunner:
            last_session = None

            def __init__(self, label: str) -> None:
                self._label = label

            def run_task(self, task, max_turns=20, system_addendum=""):
                start = time.monotonic()
                for _ in range(3):
                    time.sleep(0.1)
                    yield TurnEvent.text_done("")
                end = time.monotonic()
                with lock:
                    intervals[self._label] = (start, end)
                yield TurnEvent.text_done(f"{self._label} summary")

        loop = _make_loop(manager)
        import lucnhan.agent.loop as loop_mod

        real_runner_cls = loop_mod.SubagentRunner
        counter = {"n": 0}

        def fake_runner(**kwargs):
            counter["n"] += 1
            return _SlowRunner(f"child{counter['n']}")

        loop_mod.SubagentRunner = fake_runner
        started = time.monotonic()
        try:
            gen = loop._execute_tool_calls(
                [_spawn_call("slow one", "c1"), _spawn_call("slow two", "c2")]
            )
            try:
                while True:
                    gen.send(None)
            except StopIteration as stop:
                results = stop.value
        finally:
            loop_mod.SubagentRunner = real_runner_cls
        wall = time.monotonic() - started

        assert "child1 summary" in results[0].content
        assert "child2 summary" in results[1].content
        assert len(intervals) == 2
        (s1, e1), (s2, e2) = intervals.values()
        # Serial execution would make this non-overlapping.
        assert s2 < e1 and s1 < e2, f"intervals did not overlap: {intervals}"
        # 2 children x 0.3s each; overlap keeps it near one child's time.
        assert wall < 0.5, f"wall {wall:.2f}s suggests serialized execution"


class TestJoinCancellation(unittest.TestCase):
    """Cancelling the parent during the join cancels every child."""

    def test_cancel_cancels_all_pending_and_raises(self) -> None:
        """A parent cancel mid-join cancels every child, then propagates."""
        cancelled: list[str] = []

        class _HangingManager(SubagentManager):
            def cancel(self, agent_id: str) -> None:
                cancelled.append(agent_id)
                self.update_external(agent_id, SubagentStatus.CANCELLED, summary="cancelled")

        manager = _make_manager(_HangingManager)
        loop = _make_loop(manager)

        # Two children that never reach a terminal status on their own.
        id1 = manager.register(name="one", task="t")
        id2 = manager.register(name="two", task="t")
        pending = [(0, _spawn_call("one", "c1"), id1), (1, _spawn_call("two", "c2"), id2)]
        tool_results: list = [None, None]

        loop._cancelled.set()
        gen = loop._join_subagents(pending, tool_results)
        with self.assertRaises(CancellationError):
            while True:
                gen.send(None)

        # Every child was cancelled exactly once, and the loop re-raised.
        assert sorted(cancelled) == sorted([id1, id2])
        assert all(
            manager.get(aid).status == SubagentStatus.CANCELLED for aid in (id1, id2)
        )
        # No results were produced for the cancelled children.
        assert tool_results == [None, None]

    def test_cancel_during_dispatch_skips_join(self) -> None:
        """A cancel arriving mid-join stops the children and aborts the batch.

        Production cancellation is set from the UI thread while this
        generator blocks inside the join poll, so the test does the same
        rather than flipping the flag between ``send`` calls.
        """
        cancelled: list[str] = []

        class _HangingManager(SubagentManager):
            def spawn(self, name: str, task: str, **kwargs) -> str:  # type: ignore[override]
                return super().register(name=name, task=task)

            def cancel(self, agent_id: str) -> None:
                cancelled.append(agent_id)
                self.update_external(agent_id, SubagentStatus.CANCELLED, summary="cancelled")

        manager = _make_manager(_HangingManager)
        loop = _make_loop(manager)
        import lucnhan.agent.loop as loop_mod

        real_runner_cls = loop_mod.SubagentRunner
        loop_mod.SubagentRunner = lambda **kwargs: _RecordingRunner("never", [])

        def cancel_when_both_registered() -> None:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if len(manager.list_all()) == 2:
                    loop._cancelled.set()
                    return
                time.sleep(0.01)

        try:
            canceller = threading.Thread(target=cancel_when_both_registered)
            canceller.start()
            gen = loop._execute_tool_calls(
                [_spawn_call("hang one", "c1"), _spawn_call("hang two", "c2")]
            )
            with self.assertRaises(CancellationError):
                while True:
                    gen.send(None)
        finally:
            loop_mod.SubagentRunner = real_runner_cls

        assert sorted(cancelled) == sorted(a.id for a in manager.list_all())
        assert len(cancelled) == 2


if __name__ == "__main__":
    unittest.main()
