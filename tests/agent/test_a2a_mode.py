"""Tests for the /a2a slash command and modes.a2a.run_a2a_mode.

Two layers of coverage:
1. Parser: ``_parse_user_command`` recognises ``/a2a <agent> <msg>``
   and sets ``use_a2a_mode=True``.
2. Mode runner: ``run_a2a_mode`` parses the body, builds an
   ``A2ADispatcher``, and streams its events to the chat.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.agent.loop import _parse_user_command
from rikugan.agent.modes.a2a import run_a2a_mode
from rikugan.agent.turn import TurnEventType


def _drain(gen) -> tuple[list, str]:
    """Consume a generator to completion; return (events, return_value)."""
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as e:
        return events, e.value
    return events, ""  # pragma: no cover


class TestParser(unittest.TestCase):
    """_parse_user_command recognises /a2a and routes correctly."""

    def test_a2a_with_agent_and_message(self) -> None:
        cmd = _parse_user_command("/a2a claude summarize the binary")
        self.assertTrue(cmd.use_a2a_mode)
        self.assertEqual(cmd.message, "claude summarize the binary")

    def test_a2a_with_quoted_message(self) -> None:
        cmd = _parse_user_command('/a2a codex "what does main() do?"')
        self.assertTrue(cmd.use_a2a_mode)
        self.assertEqual(cmd.message, 'codex "what does main() do?"')

    def test_bare_a2a_yields_empty_body(self) -> None:
        """``/a2a`` alone — the mode runner surfaces a usage error."""
        cmd = _parse_user_command("/a2a")
        self.assertTrue(cmd.use_a2a_mode)
        self.assertEqual(cmd.message, "")

    def test_a2a_with_only_agent(self) -> None:
        cmd = _parse_user_command("/a2a claude")
        self.assertTrue(cmd.use_a2a_mode)
        self.assertEqual(cmd.message, "claude")

    def test_a2a_uppercase_normalised(self) -> None:
        """The parser lowercases the prefix check; body is preserved."""
        cmd = _parse_user_command("/A2A claude hello")
        self.assertTrue(cmd.use_a2a_mode)
        self.assertEqual(cmd.message, "claude hello")

    def test_a2a_does_not_match_other_slash_commands(self) -> None:
        cmd = _parse_user_command("/a2a-different something")
        self.assertFalse(cmd.use_a2a_mode)
        # Falls through to the default branch
        self.assertEqual(cmd.message, "/a2a-different something")

    def test_other_commands_unaffected(self) -> None:
        # Sanity check: /plan / /modify / /orchestra still work.
        for prefix, attr in [
            ("/plan", "use_plan_mode"),
            ("/modify", "use_exploration_mode"),
            ("/orchestra", "use_orchestra_mode"),
        ]:
            cmd = _parse_user_command(f"{prefix} hello")
            self.assertTrue(getattr(cmd, attr), f"{prefix} did not set {attr}")
            self.assertFalse(cmd.use_a2a_mode)

    def test_orchestra_command_remains_recognized_while_runtime_is_gated(self) -> None:
        """Parser compatibility: ``/orchestra`` still sets
        ``use_orchestra_mode=True`` and the message body is preserved
        unchanged. The runtime Orchestra path is currently disabled,
        but the parser must continue to recognize the command so users
        receive a precise disabled message instead of an
        unknown-command error.
        """
        cmd = _parse_user_command("/orchestra inspect imports")
        self.assertTrue(cmd.use_orchestra_mode)
        self.assertEqual(cmd.message, "inspect imports")
        self.assertFalse(cmd.use_a2a_mode)


class TestModeRunnerValidation(unittest.TestCase):
    """The mode runner surfaces friendly errors for malformed input."""

    def _fake_loop(self) -> MagicMock:
        loop = MagicMock()
        loop._cancelled = None
        loop.config = MagicMock()
        loop.config.a2a_auto_discover = True
        loop.config.a2a_agents = []
        return loop

    def test_no_args_yields_usage_error(self) -> None:
        loop = self._fake_loop()
        events, _ = _drain(run_a2a_mode(loop, "", "", []))
        # First event is an error, last is text_done with the
        # usage hint.
        self.assertGreaterEqual(len(events), 2)
        self.assertEqual(events[0].type, TurnEventType.ERROR)
        self.assertIn("Usage", events[0].error)
        # Final text_done echoes the same hint
        text_done = next(e for e in events if e.type == TurnEventType.TEXT_DONE)
        self.assertIn("Usage", text_done.text)

    def test_only_agent_yields_usage_error(self) -> None:
        """``/a2a claude`` (no body) → usage error (treated as missing args)."""
        loop = self._fake_loop()
        events, _ = _drain(run_a2a_mode(loop, "claude", "", []))
        self.assertEqual(events[0].type, TurnEventType.ERROR)
        # The runner collapses "only agent" into the missing-args
        # path because there's no body to dispatch. The user sees
        # the usage hint either way.
        self.assertIn("Usage", events[0].error)

    def test_only_message_yields_missing_agent_error(self) -> None:
        """``/a2a hello world`` parses as agent='hello', message='world'."""
        # This is actually valid: the mode treats the first token
        # as the agent and the rest as the message. So this
        # should NOT error — it'll try to dispatch to a
        # (probably nonexistent) agent named "hello".
        # The error in that case comes from the dispatcher, not
        # the validator.
        loop = self._fake_loop()

        def fake_discover(self):
            return []  # no agents — the dispatcher will error

        with patch("rikugan.agent.a2a.dispatcher.SubprocessBridge.discover", new=fake_discover):
            events, _ = _drain(run_a2a_mode(loop, "hello world", "", []))
        # We expect an error from the dispatcher (unknown agent),
        # not from the validator.
        self.assertGreaterEqual(len(events), 2)
        # Look for either a validator error or dispatcher error
        has_error = any(e.type == TurnEventType.ERROR for e in events)
        self.assertTrue(has_error)


class TestModeRunnerDispatch(unittest.TestCase):
    """The mode runner dispatches via A2ADispatcher and streams events."""

    def _fake_loop(self) -> MagicMock:
        loop = MagicMock()
        loop._cancelled = None
        loop.config = MagicMock()
        loop.config.a2a_auto_discover = True
        loop.config.a2a_agents = []
        return loop

    def test_streams_dispatcher_events(self) -> None:
        """Successful run yields TEXT_DELTA + TEXT_DONE with the result."""
        from rikugan.agent.a2a.types import A2AEvent, ExternalAgentConfig

        loop = self._fake_loop()
        agents = [
            ExternalAgentConfig(
                name="claude",
                transport="subprocess",
                endpoint="claude",
                capabilities=["code_generation"],
            )
        ]

        def fake_run(*args, **kwargs):
            yield A2AEvent(type="stdout", text="working...\n")
            yield A2AEvent(type="completed", text="done!", done=True)

        with (
            patch(
                "rikugan.agent.a2a.dispatcher.SubprocessBridge.discover",
                return_value=agents,
            ),
            patch(
                "rikugan.agent.a2a.dispatcher.SubprocessBridge.run_task",
                new=fake_run,
            ),
        ):
            events, _ = _drain(run_a2a_mode(loop, "claude do thing", "", []))

        # First event: "Delegating to claude..." preamble
        self.assertEqual(events[0].type, TurnEventType.TEXT_DELTA)
        self.assertIn("Delegating to claude", events[0].text)

        # Subsequent: stdout chunk, completed text
        text_chunks = [e.text for e in events if e.type == TurnEventType.TEXT_DELTA]
        joined = "".join(text_chunks)
        self.assertIn("working", joined)
        self.assertIn("done!", joined)

        # Final event: TEXT_DONE with the aggregated output
        final = next(e for e in events if e.type == TurnEventType.TEXT_DONE)
        self.assertIn("done!", final.text)

    def test_unknown_agent_yields_error(self) -> None:
        loop = self._fake_loop()
        with patch(
            "rikugan.agent.a2a.dispatcher.SubprocessBridge.discover",
            return_value=[],
        ):
            events, _ = _drain(run_a2a_mode(loop, "nonexagent do thing", "", []))
        # Should yield an error event from the dispatcher.
        error_events = [e for e in events if e.type == TurnEventType.ERROR]
        self.assertGreaterEqual(len(error_events), 1)
        self.assertIn("nonexagent", error_events[0].error)

    def test_a2a_agents_config_forwarded_to_dispatcher(self) -> None:
        """The dispatcher's a2a_agents is sourced from loop.config."""
        from rikugan.agent.a2a.types import ExternalAgentConfig

        loop = self._fake_loop()
        loop.config.a2a_agents = [{"name": "x", "endpoint": "https://x"}]
        agents = [ExternalAgentConfig(name="x", transport="a2a", endpoint="https://x")]

        # Capture what the dispatcher was constructed with.
        from rikugan.agent.modes import a2a as a2a_mode

        with patch.object(a2a_mode, "A2ADispatcher") as mock_dispatcher_cls:
            mock_dispatcher_cls.return_value.discover.return_value = agents
            mock_dispatcher_cls.return_value.run_task.return_value = iter([])

            def empty_run(*a, **kw):
                return iter([])

            mock_dispatcher_cls.return_value.run_task.side_effect = empty_run

            with patch(
                "rikugan.agent.a2a.dispatcher.SubprocessBridge.discover",
                return_value=[],
            ):
                list(run_a2a_mode(loop, "x do thing", "", []))

        # The dispatcher was constructed with the loop's config
        kwargs = mock_dispatcher_cls.call_args.kwargs
        self.assertEqual(kwargs.get("a2a_agents"), [{"name": "x", "endpoint": "https://x"}])
        self.assertTrue(kwargs.get("auto_discover"))


if __name__ == "__main__":
    unittest.main()
