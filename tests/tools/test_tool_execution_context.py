"""Worker execution context: host/worker split, isolation, cancellation.

A real queued dispatcher and a real host thread stand in for IDA: the
point is that the handler genuinely runs somewhere other than where it
was submitted, and that two contexts cannot bleed into each other.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from lucnhan.core.errors import ToolError
from lucnhan.tools.base import ParameterSchema, ToolDefinition, tool
from lucnhan.tools.execution import (
    ToolExecutionContext,
    get_execution_context,
    run_on_host_thread,
    tool_execution_context,
)
from lucnhan.tools.registry import ToolRegistry

_HOST_WAIT = 5.0


class _QueuedHost(threading.Thread):
    """Stand-in for the IDA main thread: pumps a queue of jobs, forever."""

    daemon = True

    def __init__(self) -> None:
        super().__init__(name="fake-ida-main")
        self.jobs: queue.Queue = queue.Queue()
        self.pumped: list[str] = []
        self.started = threading.Event()

    def run(self) -> None:
        self.started.set()
        while True:
            job = self.jobs.get()
            if job is None:
                return
            name, func, args, kwargs, box = job
            self.pumped.append(name)
            try:
                box["result"] = func(*args, **kwargs)
            except BaseException as exc:
                box["error"] = exc
            finally:
                box["event"].set()

    def wrap(self, func):
        """Registry ``dispatch_wrapper`` seam: ``wrap(handler) -> handler``."""
        name = getattr(func, "__name__", repr(func))

        def wrapped(*args, **kwargs):
            if threading.current_thread() is self:
                return func(*args, **kwargs)
            box: dict = {"event": threading.Event(), "result": None, "error": None}
            self.jobs.put((name, func, args, kwargs, box))
            if not box["event"].wait(_HOST_WAIT):
                raise TimeoutError(f"host never pumped {name!r}")
            if box["error"] is not None:
                raise box["error"]
            return box["result"]

        return wrapped


class _HostTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.host = _QueuedHost()
        self.host.start()
        self.assertTrue(self.host.started.wait(_HOST_WAIT))
        self.addCleanup(self.host.jobs.put, None)

    def assertFinished(self, t: threading.Thread) -> None:
        t.join(_HOST_WAIT)
        self.assertFalse(t.is_alive(), "caller thread never finished")


def _defn(
    name: str,
    handler,
    *,
    main_thread: bool = True,
    mutating: bool = False,
    params: list[ParameterSchema] | None = None,
    timeout: float | None = None,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=name,
        parameters=params or [],
        handler=handler,
        main_thread=main_thread,
        mutating=mutating,
        timeout=timeout,
    )


def _call_later(reg: ToolRegistry, name: str, args: dict, box: list, **kwargs) -> threading.Thread:
    """Start ``reg.execute(...)`` on its own caller thread."""
    t = threading.Thread(target=lambda: box.append(reg.execute(name, args, **kwargs)))
    t.start()
    return t


class TestHostWorkerSplit(_HostTestCase):
    """A main_thread=False tool dispatches only its host section."""

    def setUp(self) -> None:
        super().setUp()
        # (host section result, did the CPU phase run off the host thread?)
        self.sections: list[tuple[str, bool]] = []
        # Did the CPU phase see a cancel event object at all?
        self.events: list[bool] = []
        self.in_cpu_phase = threading.Event()
        self.release = threading.Event()

        @tool(name="split", description="split", main_thread=False)
        def handler(text: str) -> str:
            ctx = get_execution_context()
            snapshot = run_on_host_thread(lambda: f"{text}-host")
            # The CPU phase must stay on this (worker) thread.
            off_host = threading.current_thread() is not self.host
            self.in_cpu_phase.set()
            # Poll the way a long-running tool would, so a cancel that
            # arrives *after* the CPU phase started is observed here.
            self.release.wait(_HOST_WAIT)
            cancel = ctx.cancel_event
            cancelled = cancel is not None and cancel.is_set()
            self.sections.append((snapshot, off_host))
            self.events.append(cancel is not None)
            return f"{text}|{snapshot}|{'cancelled' if cancelled else 'completed'}"

        self.registry = ToolRegistry(dispatch_wrapper=self.host.wrap)
        self.registry.register_function(handler)

    def test_host_section_runs_on_host_thread_and_cpu_section_does_not(self):
        box: list[str] = []
        t = _call_later(self.registry, "split", {"text": "abc"}, box)

        self.assertTrue(self.in_cpu_phase.wait(_HOST_WAIT), "handler never reached its CPU phase")
        self.assertEqual(self.host.pumped, ["<lambda>"], "whole handler was dispatched instead of its host section")

        self.release.set()
        self.assertFinished(t)
        self.assertEqual(box, ["abc|abc-host|completed"])
        snapshot, cpu_phase_off_host = self.sections[0]
        self.assertEqual(snapshot, "abc-host", "host section did not run through the host queue")
        self.assertTrue(cpu_phase_off_host, "CPU phase ran on the host thread")
        self.assertEqual(self.events, [False], "unexpected cancel event")

    def test_cancel_reaches_a_running_worker(self):
        cancel = threading.Event()
        box: list[str] = []
        t = _call_later(self.registry, "split", {"text": "abc"}, box, cancel_event=cancel)

        self.assertTrue(self.in_cpu_phase.wait(_HOST_WAIT), "handler never reached its CPU phase")
        cancel.set()
        self.release.set()
        self.assertFinished(t)

        self.assertEqual(box, ["abc|abc-host|cancelled"], "worker did not observe the run's cancellation")
        self.assertEqual(self.events, [True], "worker never received the cancel event")

    def test_next_invocation_is_unaffected_by_a_cancelled_one(self):
        cancel = threading.Event()
        cancel.set()
        box: list[str] = []
        t = _call_later(self.registry, "split", {"text": "first"}, box, cancel_event=cancel)
        self.assertTrue(self.in_cpu_phase.wait(_HOST_WAIT))
        self.release.set()
        self.assertFinished(t)
        self.assertEqual(box, ["first|first-host|cancelled"])

        self.sections.clear()
        self.events.clear()
        self.in_cpu_phase.clear()
        out = self.registry.execute("split", {"text": "second"})
        self.assertEqual(out, "second|second-host|completed")
        self.assertEqual(self.events, [False], "cancelled event leaked into the next call")


class TestOrdinaryToolDispatchUnchanged(_HostTestCase):
    def test_main_thread_tool_runs_entirely_on_the_host(self):
        seen: list[bool] = []

        def handler() -> str:
            seen.append(threading.current_thread() is self.host)
            return "ok"

        registry = ToolRegistry(dispatch_wrapper=self.host.wrap)
        registry.register(_defn("ordinary", handler))

        box: list[str] = []
        self.assertFinished(_call_later(registry, "ordinary", {}, box))

        self.assertEqual(box, ["ok"])
        self.assertEqual(seen, [True], "default tool was no longer dispatched to the host thread")
        self.assertEqual(self.host.pumped, ["handler"])

    def test_mutating_tool_without_host_dispatch_still_serializes(self):
        """A main_thread=False tool keeps mutation serialization and caching."""
        order: list[str] = []
        entered = threading.Semaphore(0)
        done = threading.Event()

        def first() -> str:
            order.append("first-in")
            entered.release()
            done.wait(_HOST_WAIT)
            return "first"

        def second() -> str:
            order.append("second-in")
            return "second"

        registry = ToolRegistry(dispatch_wrapper=self.host.wrap)
        registry.register(_defn("mutate", first, main_thread=False, mutating=True))
        registry.register(_defn("mutate2", second, main_thread=False, mutating=True))

        box: list[str] = []
        t1 = _call_later(registry, "mutate", {}, box)
        self.assertTrue(entered.acquire(timeout=_HOST_WAIT), "first mutator never started")
        t2 = _call_later(registry, "mutate2", {}, box)
        done.set()
        self.assertFinished(t1)
        self.assertFinished(t2)

        self.assertEqual(box, ["first", "second"])
        self.assertEqual(order, ["first-in", "second-in"], "mutating tools interleaved on the pool")
        self.assertEqual(self.host.pumped, [], "main_thread=False tool was host-dispatched anyway")
        # Mutating tools flush the read-only cache.
        self.assertEqual(registry._result_cache.size, 0)


class TestConcurrentContextIsolation(_HostTestCase):
    def test_overlapping_calls_keep_their_own_cancel_event(self):
        both_inside = threading.Barrier(2, timeout=_HOST_WAIT)
        observed: dict[str, bool] = {}
        cancel_a = threading.Event()
        cancel_a.set()

        def handler(tag: str) -> str:
            # Both handlers must reach this line at the same time — that
            # only holds if two executions really do overlap.
            both_inside.wait()
            cancel = get_execution_context().cancel_event
            observed[tag] = cancel is cancel_a
            return tag

        registry = ToolRegistry(dispatch_wrapper=self.host.wrap)
        registry.register(
            _defn(
                "probe",
                handler,
                main_thread=False,
                params=[ParameterSchema(name="tag", type="string")],
                # Two pool workers: a shorter timeout would fire while the
                # second call waits for a free one.
                timeout=_HOST_WAIT,
            )
        )

        boxes: dict[str, list[str]] = {"a": [], "b": []}
        t1 = _call_later(registry, "probe", {"tag": "a"}, boxes["a"], cancel_event=cancel_a)
        t2 = _call_later(registry, "probe", {"tag": "b"}, boxes["b"])
        self.assertFinished(t1)
        self.assertFinished(t2)

        self.assertEqual(boxes, {"a": ["a"], "b": ["b"]})
        self.assertEqual(observed, {"a": True, "b": False}, "contexts bled between concurrent calls")


class TestContextCleanup(_HostTestCase):
    def test_context_is_gone_after_a_failing_tool(self):
        def handler() -> str:
            raise RuntimeError("boom")

        registry = ToolRegistry(dispatch_wrapper=self.host.wrap)
        registry.register(_defn("boom", handler, main_thread=False))
        with self.assertRaises(ToolError):
            registry.execute("boom", {})

        seen: list[object] = []

        def survivor() -> str:
            seen.append(get_execution_context().cancel_event)
            return "ok"

        registry.register(_defn("survivor", survivor, main_thread=False))
        self.assertEqual(registry.execute("survivor", {}), "ok")
        self.assertEqual(seen, [None], "a failed call left its context on the pool thread")

    def test_caller_thread_does_not_adopt_the_tool_context(self):
        cancel = threading.Event()
        cancel.set()
        holding = threading.Event()
        release = threading.Event()
        observed: list[object] = []

        def handler() -> str:
            holding.set()
            release.wait(_HOST_WAIT)
            return "ok"

        registry = ToolRegistry(dispatch_wrapper=self.host.wrap)
        registry.register(_defn("hold", handler, main_thread=False))
        t = _call_later(registry, "hold", {}, [], cancel_event=cancel)
        self.assertTrue(holding.wait(_HOST_WAIT))
        observed.append(get_execution_context().cancel_event)
        release.set()
        self.assertFinished(t)

        self.assertEqual(observed, [None], "the submitting thread adopted the tool's context")


class TestRunOnHostThread(_HostTestCase):
    def test_direct_call_on_main_thread(self):
        self.assertTrue(run_on_host_thread(lambda: threading.current_thread() is threading.main_thread()))

    def test_worker_call_goes_through_the_context_dispatcher(self):
        box: list[str] = []

        def run() -> None:
            with tool_execution_context(ToolExecutionContext(dispatch_wrapper=self.host.wrap)):
                box.append(run_on_host_thread(lambda: threading.current_thread().name))

        t = threading.Thread(target=run)
        t.start()
        self.assertFinished(t)

        self.assertEqual(box, [self.host.name], "worker call bypassed the context dispatcher")
        self.assertEqual(self.host.pumped, ["<lambda>"])

    def test_missing_dispatcher_runs_inline_and_keeps_the_context(self):
        """No dispatcher installed: the call runs inline on the worker.

        The host queue stays empty (nothing was dispatched anywhere) and
        the surrounding context is still the one the caller installed.
        """
        box: list[tuple[str, object]] = []
        cancel = threading.Event()

        def run() -> None:
            with tool_execution_context(ToolExecutionContext(cancel_event=cancel)):
                box.append(
                    (
                        threading.current_thread().name,
                        get_execution_context().cancel_event,
                    )
                )
                run_on_host_thread(lambda: None)

        t = threading.Thread(target=run, name="no-dispatcher-worker")
        t.start()
        self.assertFinished(t)

        self.assertEqual(box, [("no-dispatcher-worker", cancel)])
        self.assertEqual(self.host.pumped, [], "an undispatched call still reached the host queue")


class TestDeadline(_HostTestCase):
    def test_deadline_is_the_calls_own_timeout_bound(self):
        observed: list[float] = []

        def handler() -> str:
            deadline = get_execution_context().deadline
            self.assertIsNotNone(deadline)
            observed.append(time.monotonic() - deadline)
            return "ok"

        registry = ToolRegistry(dispatch_wrapper=self.host.wrap)
        registry.register(_defn("timed", handler, main_thread=False, timeout=2.0))
        self.assertEqual(registry.execute("timed", {}), "ok")
        # -2.0 <= elapsed <= 0 — the deadline is set before submission.
        self.assertTrue(-2.0 <= observed[0] <= 0.0, f"deadline not derived from tool timeout: {observed[0]}")

    def test_current_thread_context_has_no_deadline(self):
        observed: list[object] = []

        def handler() -> str:
            observed.append(get_execution_context().deadline)
            return "ok"

        registry = ToolRegistry(dispatch_wrapper=self.host.wrap)
        registry.register(_defn("direct", handler, main_thread=False))
        self.assertEqual(registry.execute_current_thread("direct", {}), "ok")
        self.assertEqual(observed, [None])


if __name__ == "__main__":
    unittest.main()
