"""Session-local /undo: the stack must survive between normal runs.

Reproduced defect: ``AgentLoop`` built a private ``_mutation_log`` per run,
so run 2's ``/undo`` always saw an empty log ("Nothing to undo") even though
run 1 had mutated the database. The controller now owns one list per live
chat tab and hands it to every loop it starts, so ``/undo`` in a *later* run
reverses a mutation made in an *earlier* one.

These tests drive real controller runs (``start_agent`` → runner thread →
``AgentLoop.run``) against an in-memory tool registry with a scripted
provider — no IDB, no provider network, no credentials.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from lucnhan.agent.loop import AgentLoop, BackgroundAgentRunner
from lucnhan.agent.loop_commands import _handle_undo_command
from lucnhan.agent.mutation import MutationRecord
from lucnhan.agent.turn import TurnEvent, TurnEventType
from lucnhan.core.config import LucNhanConfig
from lucnhan.core.errors import ToolError
from lucnhan.core.types import (
    Message,
    ModelInfo,
    ProviderCapabilities,
    Role,
    StreamChunk,
    TokenUsage,
)
from lucnhan.ida.ui.session_controller import IdaSessionController
from lucnhan.providers.base import LLMProvider
from lucnhan.state.history import SessionHistory
from lucnhan.state.session import SessionState
from lucnhan.tools.base import ParameterSchema, ToolDefinition
from lucnhan.tools.registry import ToolRegistry
from lucnhan.ui import session_controller_base

_RENAME_ARGS = {"address": "0x401000", "new_name": "main"}


class ScriptedProvider(LLMProvider):
    """Provider stub. The first turn calls a mutating tool, every later
    turn answers with plain text — enough for a normal run to finish."""

    def __init__(self, *, rename: bool = True) -> None:
        super().__init__(api_key="test", model="mock-model")
        self.calls = 0
        self._rename = rename

    @property
    def name(self) -> str:
        return "mock"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    def _get_client(self):
        return None

    def _fetch_models_live(self) -> list[ModelInfo]:
        return [ModelInfo(id="mock-model", name="Mock", provider="mock")]

    @staticmethod
    def _builtin_models() -> list[ModelInfo]:
        return [ModelInfo(id="mock-model", name="Mock", provider="mock")]

    def _format_messages(self, messages):
        return messages

    def _normalize_response(self, raw):
        return raw

    def _build_request_kwargs(self, messages, tools, temperature, max_tokens, system, **kwargs):
        return {}

    def _call_api(self, client, kwargs):
        return None

    def _handle_api_error(self, e):
        raise e

    def _stream_chunks(self, client, kwargs, cancel_event=None):
        yield from ()

    def chat(self, messages, tools=None, temperature=0.3, max_tokens=4096, system="", **kwargs) -> Message:
        return Message(role=Role.ASSISTANT, content="mock response")

    def chat_stream(
        self,
        messages,
        tools=None,
        temperature=0.3,
        max_tokens=4096,
        system="",
        cancel_event=None,
        *,
        request_context=None,
        **kwargs,
    ):
        self.calls += 1
        if self._rename and self.calls == 1:
            yield StreamChunk(is_tool_call_start=True, tool_call_id="c1", tool_name="rename_function")
            yield StreamChunk(tool_args_delta=json.dumps(_RENAME_ARGS), tool_call_id="c1")
            yield StreamChunk(is_tool_call_end=True, tool_call_id="c1", tool_name="rename_function")
        yield StreamChunk(text="Done.")
        yield StreamChunk(usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15))


def _make_registry(state: dict[str, str], *, fail_rename: bool = False) -> ToolRegistry:
    """``rename_function`` + ``get_function_name`` over a plain dict.

    ``state["name"]`` is the observable database state, so a reverse that
    really ran is visible without any IDB.
    """
    registry = ToolRegistry()

    def _rename(address: str = "", new_name: str = "", s: dict[str, str] = state) -> str:
        if fail_rename:
            raise ToolError("rename rejected")
        s["name"] = new_name
        return f"Renamed {address} to {new_name}"

    registry.register(
        ToolDefinition(
            name="rename_function",
            description="Rename a function",
            parameters=[
                ParameterSchema(name="address", type="string"),
                ParameterSchema(name="new_name", type="string"),
            ],
            mutating=True,
            handler=_rename,
        )
    )
    registry.register(
        ToolDefinition(
            name="get_function_name",
            description="Get the current function name",
            parameters=[ParameterSchema(name="address", type="string")],
            mutating=False,
            handler=lambda address="", s=state: s.get("name", ""),
        )
    )
    return registry


def _drain_until_finished(ctrl: IdaSessionController, tab_id: str, timeout: float = 30.0) -> list[TurnEvent]:
    """Consume one tab's run until the runner's end-of-stream sentinel.

    ``BackgroundAgentRunner._run`` always puts a ``None`` sentinel
    (``_put_sentinel``), so a bounded blocking ``get`` is the
    deterministic end-of-run signal — polling ``is_running`` races a run
    that already finished before the first poll.
    """
    runner = ctrl.get_runner(tab_id)
    assert runner is not None
    events: list[TurnEvent] = []
    try:
        while True:
            event = runner.event_queue.get(timeout=timeout)
            if event is None:
                break
            events.append(event)
    except queue.Empty:  # pragma: no cover - guard, not an expected path
        ctrl.cancel(tab_id)
        raise AssertionError(f"run on tab {tab_id} did not finish within {timeout}s") from None
    ctrl.on_agent_finished(tab_id)
    return events


class TestSessionUndoStack(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = LucNhanConfig()
        self.cfg._config_dir = tempfile.mkdtemp()
        self.cfg.checkpoint_auto_save = False
        self.state = {"name": "sub_401000"}
        self.registry = _make_registry(self.state)
        # Two runs of the SAME chat tab must use the same registry so the
        # second run's /undo reverses the first run's rename.
        _patcher = patch.object(IdaSessionController, "_create_provider", side_effect=ScriptedProvider)
        _patcher.start()
        self.addCleanup(_patcher.stop)
        self.ctrl = IdaSessionController(self.cfg)
        self.ctrl._runtime_init_done.wait(timeout=5.0)
        # The tests own the registry; skip the host's deferred IDA-tool
        # registration so a run never imports the real tool module tree.
        self.ctrl._advanced_tools_registered = True
        self.ctrl._tool_registry = self.registry
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        # Some tests shut the controller down themselves; shutdown is
        # idempotent, but flushing the async saver first is not.
        SessionHistory.flush_saves()
        self.ctrl.shutdown()
        shutil.rmtree(self.cfg._config_dir, ignore_errors=True)

    def _run(self, message: str, tab_id: str | None = None) -> list[TurnEvent]:
        tid = tab_id if tab_id is not None else self.ctrl.active_tab_id
        error = self.ctrl.start_agent(message, tab_id=tid)
        self.assertIsNone(error, f"start_agent failed: {error}")
        return _drain_until_finished(self.ctrl, tid)

    @staticmethod
    def _mutation_events(events: list[TurnEvent]) -> list[TurnEvent]:
        return [e for e in events if e.type == TurnEventType.MUTATION_RECORDED]

    @staticmethod
    def _undone_events(events: list[TurnEvent]) -> list[TurnEvent]:
        return [e for e in events if e.type == TurnEventType.MUTATION_UNDONE]

    # ------------------------------------------------------------------
    # Cross-run undo — the reproduced defect
    # ------------------------------------------------------------------

    def test_later_run_undo_reverses_earlier_run_mutation(self) -> None:
        tab = self.ctrl.active_tab_id

        run1 = self._run("Rename sub_401000 to main")
        self.assertEqual(len(self._mutation_events(run1)), 1, "run 1 must record the mutation")
        self.assertEqual(self.state["name"], "main", "run 1 must really rename")

        run2 = self._run("/undo 1")
        undone_texts = [e.text for e in run2 if e.type == TurnEventType.TEXT_DONE]
        self.assertTrue(any("empty" not in t.lower() for t in undone_texts), f"/undo saw an empty log: {undone_texts}")
        self.assertEqual(self.state["name"], "sub_401000", "run 2 must restore the original state")
        self.assertEqual([e.metadata["count"] for e in self._undone_events(run2)], [1])
        self.assertEqual(self.ctrl.mutation_records(tab), [], "reversed record must leave the stack")

    def test_other_session_cannot_undo_these_records(self) -> None:
        tab_a = self.ctrl.active_tab_id
        tab_b = self.ctrl.create_tab()
        self.ctrl.switch_tab(tab_a)
        self._run("Rename sub_401000 to main")
        self.assertEqual(len(self.ctrl.mutation_records(tab_a)), 1)
        self.assertEqual(self.ctrl.mutation_records(tab_b), [], "tab B must not see tab A's mutations")

        self._run("/undo 1", tab_id=tab_b)
        self.assertEqual(self.state["name"], "main", "tab B must not reverse tab A's change")
        self.assertEqual(len(self.ctrl.mutation_records(tab_a)), 1, "tab A keeps its record")

        self._run("/undo 1", tab_id=tab_a)
        self.assertEqual(self.state["name"], "sub_401000", "the owning session can still undo it")

    def test_fork_does_not_inherit_undo_ownership(self) -> None:
        tab_a = self.ctrl.active_tab_id
        self._run("Rename sub_401000 to main")
        fork = self.ctrl.fork_session(tab_a)
        self.assertIsNotNone(fork)
        self.assertEqual(self.ctrl.mutation_records(fork), [], "a fork must not clone real mutations")

    # ------------------------------------------------------------------
    # Failure / lifecycle semantics
    # ------------------------------------------------------------------

    def test_failed_reverse_keeps_record_retryable(self) -> None:
        tab = self.ctrl.active_tab_id
        self._run("Rename sub_401000 to main")

        # Swap the reverse tool for one that rejects — the real forward
        # mutation is already recorded, so the same handler covers both.
        self.ctrl._tool_registry = _make_registry(self.state, fail_rename=True)
        failed = self._run("/undo 1")
        self.assertEqual(self.state["name"], "main")
        self.assertEqual(self._undone_events(failed), [], "a failed reverse must not be announced as undone")
        self.assertEqual(len(self.ctrl.mutation_records(tab)), 1, "failed reverse stays for retry")

        self.ctrl._tool_registry = _make_registry(self.state)
        retried = self._run("/undo 1")
        self.assertEqual(self.state["name"], "sub_401000")
        self.assertEqual([e.metadata["count"] for e in self._undone_events(retried)], [1])

    def test_repeated_finish_does_not_duplicate_records(self) -> None:
        tab = self.ctrl.active_tab_id
        self._run("Rename sub_401000 to main")
        self.ctrl.on_agent_finished(tab)
        self.ctrl.on_agent_finished(tab)
        self.ctrl.on_agent_finished(tab)
        self.assertEqual(len(self.ctrl.mutation_records(tab)), 1)
        self._run("/undo 1")
        self.assertEqual(self.state["name"], "sub_401000", "exactly one undo must be needed")

    def test_cancel_then_undo_still_sees_the_cancelled_run_records(self) -> None:
        tab = self.ctrl.active_tab_id
        self._run("Rename sub_401000 to main")
        self.ctrl.cancel(tab)
        self.ctrl.on_agent_finished(tab)
        self.assertEqual(len(self.ctrl.mutation_records(tab)), 1, "cancel must not discard the run's records")
        self._run("/undo 1")
        self.assertEqual(self.state["name"], "sub_401000")

    def test_new_chat_close_reset_and_shutdown_discard_records(self) -> None:
        tab = self.ctrl.active_tab_id
        self._run("Rename sub_401000 to main")
        self.ctrl.new_chat()
        self.assertEqual(self.ctrl.mutation_records(tab), [], "new chat starts with nothing to undo")

        other = self.ctrl.create_tab()
        self._run("Rename sub_401000 to main", tab_id=other)
        self.assertEqual(len(self.ctrl.mutation_records(other)), 1)
        self.ctrl.close_tab(other)
        self.assertEqual(self.ctrl.mutation_records(other), [], "closed tab's records must not be reused")

        self.ctrl._mutation_logs["ghost-tab"] = [MutationRecord("rename_function", {}, "rename_function", {})]
        self.ctrl.reset_for_new_file("C:/tmp/other.i64")
        self.assertEqual(self.ctrl.mutation_records("ghost-tab"), [], "IDB switch drops every stack")

        third = self.ctrl.active_tab_id
        self._run("Rename sub_401000 to main", tab_id=third)
        self.assertEqual(len(self.ctrl.mutation_records(third)), 1)
        self.ctrl.shutdown()
        self.assertEqual(self.ctrl.mutation_records(third), [], "shutdown releases the stacks")

    # ------------------------------------------------------------------
    # Snapshot semantics
    # ------------------------------------------------------------------

    def test_snapshot_is_a_safe_ordered_copy_including_live_runner_records(self) -> None:
        tab = self.ctrl.active_tab_id
        session = self.ctrl.get_session(tab)
        loop = AgentLoop(
            ScriptedProvider(rename=False),
            _make_registry(self.state),
            self.ctrl.config,
            session,
            mutation_log=self.ctrl._mutation_log_for(tab),
        )
        self.ctrl._runners[tab] = BackgroundAgentRunner(loop)
        self.addCleanup(self.ctrl._runners.pop, tab, None)
        loop.record_mutations(
            [
                MutationRecord("rename_function", {}, "rename_function", {"new_name": "a"}, description="first"),
                MutationRecord("rename_address", {}, "rename_address", {"new_name": "b"}, description="second"),
            ]
        )

        snapshot = self.ctrl.mutation_records()
        self.assertEqual([r.description for r in snapshot], ["first", "second"], "ordered snapshot")
        self.assertEqual(self.ctrl.active_tab_id, tab, "zero-arg form targets the active tab")

        snapshot.clear()
        self.assertEqual(len(self.ctrl.mutation_records()), 2, "caller must not reach controller state")

    def test_started_runs_and_snapshotter_share_one_undo_lock(self) -> None:
        """Every root loop the controller starts uses the controller's lock.

        Also races an appender thread against three snapshot threads so
        the shared-mutex contract is exercised, not just asserted.
        """
        tab = self.ctrl.active_tab_id
        seen: list[tuple[AgentLoop, list[MutationRecord], threading.Lock]] = []
        real_runner = session_controller_base.BackgroundAgentRunner

        class _RecordingRunner(real_runner):  # type: ignore[misc, valid-type]
            def __init__(self, agent_loop: AgentLoop) -> None:
                super().__init__(agent_loop)
                seen.append((agent_loop, agent_loop._mutation_log, agent_loop._mutation_lock))

        with patch.object(session_controller_base, "BackgroundAgentRunner", _RecordingRunner):
            events = self._run("Rename sub_401000 to main")
            second = self._run("/undo 1")

        self.assertEqual(len(self._mutation_events(events)), 1)
        self.assertEqual([e.metadata["count"] for e in self._undone_events(second)], [1])
        self.assertEqual(len(seen), 2, "both runs went through the patched runner")
        for _loop, _log_list, lock in seen:
            self.assertIs(lock, self.ctrl._mutation_log_lock, "root loop must use the controller's undo lock")
        self.assertEqual([log_list for _l, log_list, _k in seen], [self.ctrl._mutation_log_for(tab)] * 2)

        # One list, one lock: append from a loop while the controller
        # snapshots it from other threads.
        loop = AgentLoop(
            ScriptedProvider(rename=False),
            _make_registry(self.state),
            self.ctrl.config,
            self.ctrl.get_session(tab),
            mutation_log=self.ctrl._mutation_log_for(tab),
            mutation_lock=self.ctrl._mutation_log_lock,
        )
        errors: list[BaseException] = []

        def _append() -> None:
            try:
                for i in range(50):
                    loop.record_mutations(
                        [MutationRecord("rename_address", {}, "rename_address", {}, description=f"extra-{i}")]
                    )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        def _snapshot() -> None:
            try:
                for _ in range(200):
                    for rec in self.ctrl.mutation_records(tab):
                        assert rec.description is not None
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=_append)] + [threading.Thread(target=_snapshot) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)
        self.assertEqual(errors, [], f"concurrency errors: {errors}")
        self.assertEqual(len(self.ctrl.mutation_records(tab)), 50, "50 appends, no loss and no duplicates")

    def test_unknown_tab_returns_empty_list(self) -> None:
        self.assertEqual(self.ctrl.mutation_records("does-not-exist"), [])


class TestLoopUndoPath(unittest.TestCase):
    """Loop-level behavior that does not need a controller."""

    def test_shared_list_lets_a_second_loop_undo_a_first_loop_record(self) -> None:
        shared: list[MutationRecord] = []
        state = {"name": "sub_401000"}
        registry = _make_registry(state)

        run1 = AgentLoop(ScriptedProvider(), registry, LucNhanConfig(), SessionState(), mutation_log=shared)
        list(run1.run("Rename sub_401000 to main"))
        self.assertEqual(len(shared), 1)

        run2 = AgentLoop(ScriptedProvider(rename=False), registry, LucNhanConfig(), SessionState(), mutation_log=shared)
        events = list(run2.run("/undo 1"))
        self.assertEqual(state["name"], "sub_401000")
        self.assertEqual(shared, [])
        self.assertEqual([e.metadata["count"] for e in events if e.type == TurnEventType.MUTATION_UNDONE], [1])

    def test_mutation_records_snapshot_does_not_clear(self) -> None:
        loop = AgentLoop(ScriptedProvider(), _make_registry({"name": "x"}), LucNhanConfig(), SessionState())
        rec = MutationRecord("rename_function", {}, "rename_function", {}, description="d")
        loop.record_mutations([rec])
        self.assertEqual(loop.mutation_records(), [rec])
        self.assertEqual(loop.mutation_records(), [rec], "snapshot is non-destructive")
        self.assertEqual(len(loop.drain_mutations()), 1)
        self.assertEqual(loop.mutation_records(), [])


def test_record_appended_during_reverse_survives_the_undo() -> None:
    """A record appended while the reverse tool runs must not be popped.

    The reverse executes OUTSIDE the undo lock (it can marshal to the
    main thread), so the stack can grow mid-reversal. A tail ``pop()``
    would then drop the fresh record and leave the reversed one in the
    stack — the exact bug this pins.
    """
    original = MutationRecord(
        tool_name="rename_function",
        arguments={},
        reverse_tool="rename_function",
        reverse_arguments={"new_name": "sub_401000"},
        description="original",
    )
    late = MutationRecord(
        tool_name="rename_address",
        arguments={},
        reverse_tool="rename_address",
        reverse_arguments={},
        description="late-append",
    )
    shared: list[MutationRecord] = [original]
    lock = threading.Lock()

    class _Tools:
        """A mutating reverse tool: it appends mid-flight."""

        def execute(self, name, arguments):
            with lock:
                shared.append(late)
            return "ok"

    loop = AgentLoop(
        ScriptedProvider(rename=False),
        _make_registry({"name": "x"}),
        LucNhanConfig(),
        SessionState(),
        mutation_log=shared,
        mutation_lock=lock,
    )
    loop.tools = _Tools()  # type: ignore[assignment]

    events = list(_handle_undo_command(loop, "/undo 1"))

    assert original not in shared, "the reversed record must be gone"
    assert late in shared, "a record appended during the reverse must survive"
    assert [e.metadata["count"] for e in events if e.type == TurnEventType.MUTATION_UNDONE] == [1]


class TestMutationThreadSafety(unittest.TestCase):
    """Two root loops on ONE chat share the list AND the lock.

    This is the overlap the UI actually produces: a cancelled run whose
    runner has not been popped yet, the next run started from the same
    tab, and the controller snapshotting the same list from the UI
    thread — all three must serialize on one mutex.
    """

    def setUp(self) -> None:
        self.shared: list[MutationRecord] = []
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def _root_loop(self) -> AgentLoop:
        return AgentLoop(
            ScriptedProvider(),
            _make_registry({"name": "x"}),
            LucNhanConfig(),
            SessionState(),
            mutation_log=self.shared,
            mutation_lock=self.lock,
        )

    def test_two_root_loops_and_a_snapshotter_never_lose_or_duplicate(self) -> None:
        stale_loop = self._root_loop()  # the cancelled run that is still winding down
        live_loop = self._root_loop()  # the run that replaced it
        self.assertIs(stale_loop._mutation_lock, live_loop._mutation_lock)

        def _appender(loop: AgentLoop, base: int) -> None:
            for i in range(100):
                loop.record_mutations(
                    [MutationRecord("rename_address", {}, "rename_address", {}, description=str(base * 100 + i))]
                )

        def _reader() -> None:
            while not self.stop.is_set():
                with self.lock:  # what mutation_records() does
                    for rec in list(self.shared):
                        assert rec.description is not None

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()
        writers = [
            threading.Thread(target=_appender, args=(stale_loop, 0)),
            threading.Thread(target=_appender, args=(live_loop, 1)),
        ]
        for w in writers:
            w.start()
        for w in writers:
            w.join()
        self.stop.set()
        reader.join(timeout=2.0)
        self.assertEqual(len(self.shared), 200, "every append lands exactly once")
        self.assertEqual(len({r.description for r in self.shared}), 200, "no duplicates")

    def test_concurrent_appends_and_snapshots_never_tear(self) -> None:
        loop = self._root_loop()
        stop = threading.Event()

        def _appender(base: int) -> None:
            for i in range(100):
                loop.record_mutations(
                    [MutationRecord("rename_address", {}, "rename_address", {}, description=str(base * 100 + i))]
                )

        def _reader() -> None:
            while not stop.is_set():
                for rec in loop.mutation_records():
                    assert rec.description is not None

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()
        writers = [threading.Thread(target=_appender, args=(i,)) for i in range(4)]
        for w in writers:
            w.start()
        for w in writers:
            w.join()
        stop.set()
        reader.join(timeout=2.0)
        self.assertEqual(len(self.shared), 400)


if __name__ == "__main__":
    unittest.main()
