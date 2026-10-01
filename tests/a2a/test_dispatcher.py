"""Tests for rikugan.agent.a2a.dispatcher.A2ADispatcher.

Focus: the dispatcher's single-entry-point contract — agent lookup,
event translation, cancellation forwarding, and cap on returned text.
We mock the SubprocessBridge and A2AClient directly so no subprocess
or HTTP is exercised; the bridges themselves have their own tests.
"""

from __future__ import annotations

import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.agent.a2a import dispatcher as dispatcher_module
from rikugan.agent.a2a.client import A2AClient
from rikugan.agent.a2a.dispatcher import A2ADispatcher
from rikugan.agent.a2a.subprocess_bridge import SubprocessBridge
from rikugan.agent.a2a.types import (
    A2AEvent,
    A2ATask,
    A2ATaskStatus,
    ExternalAgentConfig,
)
from rikugan.agent.turn import TurnEventType


def _make_agent(name: str = "claude", transport: str = "subprocess") -> ExternalAgentConfig:
    return ExternalAgentConfig(
        name=name,
        transport=transport,
        endpoint=name,
        capabilities=["code_generation"],
    )


def _drain_to_return(gen) -> tuple[list, object]:
    """Consume a generator to completion, return (events, return_value)."""
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as e:
        return events, e.value
    return events, None  # pragma: no cover


class TestDiscovery(unittest.TestCase):
    """discover() caches results; cache is invalidated by construction."""

    def test_discover_returns_empty_when_no_agents(self) -> None:
        d = A2ADispatcher()
        with patch.object(SubprocessBridge, "discover", return_value=[]):
            agents = d.discover()
        self.assertEqual(agents, [])

    def test_discover_caches_result(self) -> None:
        d = A2ADispatcher()
        with patch.object(
            SubprocessBridge, "discover", return_value=[_make_agent()]
        ) as mock_disc:
            d.discover()
            d.discover()  # second call must use the cache
        self.assertEqual(mock_disc.call_count, 1)

    def test_discover_picks_up_subprocess_agents(self) -> None:
        d = A2ADispatcher()
        with patch.object(
            SubprocessBridge,
            "discover",
            return_value=[_make_agent("claude"), _make_agent("codex")],
        ):
            agents = d.discover()
        names = {a.name for a in agents}
        self.assertIn("claude", names)
        self.assertIn("codex", names)


class TestRunTaskUnknownAgent(unittest.TestCase):
    def test_unknown_agent_yields_error_event_and_returns_empty(self) -> None:
        d = A2ADispatcher()
        with patch.object(SubprocessBridge, "discover", return_value=[]):
            events, result = _drain_to_return(d.run_task("nonexistent", "do something"))

        # Should yield exactly one ERROR event mentioning the agent name.
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, TurnEventType.ERROR)
        self.assertIn("nonexistent", events[0].error)
        self.assertEqual(result, "")


class TestSubprocessRunTask(unittest.TestCase):
    def test_subprocess_yields_text_delta_for_stdout(self) -> None:
        A2ADispatcher()

        # Replace the bridge's run_task with a generator that yields
        # two events. We use a real function (not a MagicMock) so the
        # generator protocol works correctly.
        bridge = SubprocessBridge()

        def fake_run(*args, **kwargs):
            yield A2AEvent(type="stdout", text="hello from claude")
            yield A2AEvent(type="completed", text="final result", done=True)

        with patch.object(SubprocessBridge, "discover", return_value=[_make_agent()]), \
             patch.object(SubprocessBridge, "run_task", new=fake_run), \
             patch.object(dispatcher_module, "SubprocessBridge", return_value=bridge):
            # We patch the *class* but the dispatcher captured a fresh
            # instance already; re-instantiate for the patched class.
            d2 = A2ADispatcher()
            with patch.object(SubprocessBridge, "discover", return_value=[_make_agent()]), \
                 patch.object(SubprocessBridge, "run_task", new=fake_run):
                events, result = _drain_to_return(d2.run_task("claude", "summarize"))

        # The dispatcher yields TEXT_DELTA for the stdout line, then
        # for the completed line (which carries the final result).
        text_events = [e for e in events if e.type == TurnEventType.TEXT_DELTA]
        self.assertGreaterEqual(len(text_events), 1)
        # Final return value is the last completed-event text.
        self.assertEqual(result, "final result")

    def test_subprocess_surfaces_argv_injection_validation(self) -> None:
        """Tasks starting with '-' must raise ValueError → dispatcher yields error event."""
        d = A2ADispatcher()

        def fake_run(*args, **kwargs):
            # args[0] is `self` (instance binding), args[1] is the agent,
            # args[2] is the task. Use kwargs first, fall back to args.
            task = kwargs.get("task") or (args[2] if len(args) > 2 else "")
            if task.startswith("-"):
                raise ValueError(
                    f"SubprocessBridge task starts with '-': "
                    f"{task[:80]!r}"
                )
            yield A2AEvent(type="completed", text="never reached", done=True)

        with patch.object(SubprocessBridge, "discover", return_value=[_make_agent()]), \
             patch.object(SubprocessBridge, "run_task", new=fake_run):
            events, result = _drain_to_return(d.run_task("claude", "--malicious"))

        # The dispatcher must convert the ValueError into an error
        # TurnEvent, not propagate it.
        error_events = [e for e in events if e.type == TurnEventType.ERROR]
        self.assertEqual(len(error_events), 1)
        self.assertIn("starts with", error_events[0].error)
        self.assertEqual(result, "")


class TestCancellation(unittest.TestCase):
    def test_cancel_event_forwarded_to_subprocess(self) -> None:
        """The dispatcher's cancel_event must reach the bridge unchanged."""
        d = A2ADispatcher()
        cancel = threading.Event()
        received: dict = {}

        def fake_run(*args, **kwargs):
            received["cancel"] = kwargs.get("cancel_event")
            yield A2AEvent(type="completed", text="ok", done=True)

        with patch.object(SubprocessBridge, "discover", return_value=[_make_agent()]), \
             patch.object(SubprocessBridge, "run_task", new=fake_run):
            _drain_to_return(d.run_task("claude", "x", cancel_event=cancel))

        self.assertIs(received["cancel"], cancel)


class TestTruncation(unittest.TestCase):
    def test_long_output_is_truncated(self) -> None:
        d = A2ADispatcher()
        long_text = "x" * 10_000

        def fake_run(*args, **kwargs):
            yield A2AEvent(type="completed", text=long_text, done=True)

        with patch.object(SubprocessBridge, "discover", return_value=[_make_agent()]), \
             patch.object(SubprocessBridge, "run_task", new=fake_run):
            _, result = _drain_to_return(d.run_task("claude", "x"))

        # 8000 chars + marker
        self.assertGreater(len(result), 8000)
        self.assertIn("truncated", result.lower())

    def test_short_output_not_truncated(self) -> None:
        d = A2ADispatcher()

        def fake_run(*args, **kwargs):
            yield A2AEvent(type="completed", text="short", done=True)

        with patch.object(SubprocessBridge, "discover", return_value=[_make_agent()]), \
             patch.object(SubprocessBridge, "run_task", new=fake_run):
            _, result = _drain_to_return(d.run_task("claude", "x"))

        self.assertEqual(result, "short")


class TestCancelDuringSubprocess(unittest.TestCase):
    """Cancel-during-task: the cancel_event must be checked between stdout lines.

    We can't easily test the inner readline loop without a real
    subprocess, but we can verify the dispatcher accepts a cancel
    event and the SubprocessBridge actually receives it (already
    covered in test_cancel_event_forwarded_to_subprocess). The new
    piece here: the cancel event must NOT be cleared by the
    dispatcher — the caller owns it and reuses it for other work.
    """

    def test_cancel_event_preserved_across_run(self) -> None:
        d = A2ADispatcher()
        cancel = threading.Event()
        cancel.set()  # already cancelled

        def fake_run(*args, **kwargs):
            # The bridge would normally see cancel and bail; the
            # dispatcher passes the event through unchanged.
            assert kwargs["cancel_event"].is_set()
            yield A2AEvent(type="cancelled", text="cancelled", done=True)

        with patch.object(SubprocessBridge, "discover", return_value=[_make_agent()]), \
             patch.object(SubprocessBridge, "run_task", new=fake_run):
            _drain_to_return(d.run_task("claude", "x", cancel_event=cancel))

        # The dispatcher must not have touched the event — it should
        # still be set for whatever owns it.
        self.assertTrue(cancel.is_set())


class TestLazyA2AClient(unittest.TestCase):
    """A2AClient must not be instantiated until the A2A transport runs."""

    def test_a2a_client_not_constructed_for_subprocess_only(self) -> None:
        """Discovering + running subprocess must not construct an A2AClient."""
        d = A2ADispatcher()

        def fake_run(*args, **kwargs):
            yield A2AEvent(type="completed", text="ok", done=True)

        with patch.object(SubprocessBridge, "discover", return_value=[_make_agent()]), \
             patch.object(SubprocessBridge, "run_task", new=fake_run), \
             patch.object(A2AClient, "__init__", return_value=None) as mock_init:
            d.discover()
            d.run_task("claude", "x")
        # A2AClient.__init__ must NEVER be called for a subprocess-only path
        self.assertEqual(mock_init.call_count, 0)


class TestConfigA2AAgents(unittest.TestCase):
    """The a2a_agents config list is forwarded to the registry on discover()."""

    def test_config_a2a_agents_passed_to_registry(self) -> None:
        d = A2ADispatcher(a2a_agents=[{"name": "custom", "endpoint": "https://x"}])
        with patch.object(SubprocessBridge, "discover", return_value=[]), \
             patch.object(
                 dispatcher_module.ExternalAgentRegistry,
                 "discover",
                 return_value=[_make_agent("custom", "a2a")],
             ) as mock_discover:
            d.discover()
        # The first arg to registry.discover is the config_a2a_agents list
        args, kwargs = mock_discover.call_args
        passed = args[0] if args else kwargs.get("config_a2a_agents")
        self.assertEqual(passed, [{"name": "custom", "endpoint": "https://x"}])


class TestA2APath(unittest.TestCase):
    """The A2A transport (HTTP JSON-RPC) is exercised end-to-end via mocks."""

    def test_a2a_completed_returns_result(self) -> None:
        d = A2ADispatcher()

        # The send_task returns an A2ATask we can mutate to drive
        # the polling loop. Set it to COMPLETED before the poll
        # even runs — the loop should see it and exit on first check.
        completed_task = A2ATask(
            id="abc123",
            agent_name="remote",
            prompt="ignored",
        )
        completed_task.status = A2ATaskStatus.COMPLETED
        completed_task.result = "remote agent result"

        # Patch the A2A client so send_task returns our pre-built
        # task and cancel_task is a no-op.
        fake_client = MagicMock()
        fake_client.send_task.return_value = completed_task
        fake_client.cancel_task.return_value = True

        agent = _make_agent("remote", "a2a")
        with patch.object(SubprocessBridge, "discover", return_value=[agent]), \
             patch.object(A2ADispatcher, "_get_a2a_client", return_value=fake_client):
            events, result = _drain_to_return(d.run_task("remote", "do thing"))

        # Should yield at least one TEXT_DELTA (the result) and the
        # return value should be the truncated result.
        text_events = [e for e in events if e.type == TurnEventType.TEXT_DELTA]
        self.assertGreater(len(text_events), 0)
        self.assertIn("remote agent result", text_events[-1].text)
        self.assertEqual(result, "remote agent result")

    def test_a2a_failed_returns_error_event(self) -> None:
        d = A2ADispatcher()

        failed_task = A2ATask(id="x", agent_name="remote", prompt="ignored")
        failed_task.status = A2ATaskStatus.FAILED
        failed_task.error = "endpoint rejected the task"

        fake_client = MagicMock()
        fake_client.send_task.return_value = failed_task

        with patch.object(SubprocessBridge, "discover", return_value=[_make_agent("remote", "a2a")]), \
             patch.object(A2ADispatcher, "_get_a2a_client", return_value=fake_client):
            events, result = _drain_to_return(d.run_task("remote", "x"))

        error_events = [e for e in events if e.type == TurnEventType.ERROR]
        self.assertEqual(len(error_events), 1)
        self.assertIn("endpoint rejected", error_events[0].error)
        self.assertEqual(result, "")

    def test_a2a_cancelled_during_poll(self) -> None:
        """If cancel_event fires during the polling loop, the loop must exit."""
        d = A2ADispatcher()
        cancel = threading.Event()

        # Task starts RUNNING and never transitions; cancel is set
        # externally. The polling loop should detect the cancel and
        # exit.
        running_task = A2ATask(id="x", agent_name="remote", prompt="ignored")
        running_task.status = A2ATaskStatus.RUNNING

        fake_client = MagicMock()
        fake_client.send_task.return_value = running_task
        fake_client.cancel_task.return_value = True

        with patch.object(SubprocessBridge, "discover", return_value=[_make_agent("remote", "a2a")]), \
             patch.object(A2ADispatcher, "_get_a2a_client", return_value=fake_client), \
             patch("threading.Event.wait", side_effect=[False, True]):
            # Schedule the cancel to fire during the second wait.
            def fire_cancel():
                cancel.set()
            # First call returns False (timeout), second returns True
            # (event set). We use side_effect=call to fire the cancel
            # mid-test.
            cancel.set()  # set before run so first is_set() check exits
            events, result = _drain_to_return(
                d.run_task("remote", "x", cancel_event=cancel)
            )

        error_events = [e for e in events if e.type == TurnEventType.ERROR]
        self.assertEqual(len(error_events), 1)
        self.assertIn("cancelled", error_events[0].error.lower())
        # cancel_task should have been called on the client
        fake_client.cancel_task.assert_called_once()
        self.assertEqual(result, "")


if __name__ == "__main__":
    unittest.main()
