"""Tests for the SubagentRunner -> AgentLoop cancellation/model wiring."""

from __future__ import annotations

import os
import queue
import sys
import threading
import unittest
from typing import ClassVar
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.agent.subagent import SubagentRunner
from rikugan.core.config import RikuganConfig
from rikugan.core.types import ProviderCapabilities, StreamChunk
from rikugan.providers.base import LLMProvider, ModelInfo
from rikugan.state.session import SessionState
from rikugan.tools.registry import ToolRegistry


class _StubProvider(LLMProvider):
    """Provider stub with a scriptable ``chat_stream`` for runner tests."""

    def __init__(self, model: str = "stub-model") -> None:
        super().__init__(api_key="test", model=model)
        self.scripted_packets: list = []
        self.last_cancel_event: threading.Event | None = None

    @property
    def name(self) -> str:
        return "stub"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    def _get_client(self):  # pragma: no cover - never invoked
        return None

    def _fetch_models_live(self):  # pragma: no cover
        return [ModelInfo(id="stub-model", name="Stub", provider="stub")]

    @staticmethod
    def _builtin_models():  # pragma: no cover
        return [ModelInfo(id="stub-model", name="Stub", provider="stub")]

    def _format_messages(self, messages):  # pragma: no cover
        return messages

    def _normalize_response(self, raw):  # pragma: no cover
        return raw

    def _build_request_kwargs(self, messages, tools, temperature, max_tokens, system, **kwargs):
        return {}

    def _call_api(self, client, kwargs):  # pragma: no cover
        return {}

    def _handle_api_error(self, e):  # pragma: no cover
        raise e

    def _stream_chunks(self, client, kwargs, cancel_event=None):
        for c in self.scripted_packets:
            yield c
            if cancel_event is not None and cancel_event.is_set():
                return


class _FakeAgentLoop:
    """Captures constructor kwargs; exposes a minimal run() that drains one event."""

    captures: ClassVar[list[dict]] = []

    def __init__(self, *args, **kwargs):
        self._cancelled = kwargs.get("cancel_event") or threading.Event()
        self.provider = kwargs["provider"]
        self.config = kwargs["config"]
        self.tools = kwargs["tool_registry"]
        self._always_allow_scripts = False
        _FakeAgentLoop.captures.append(kwargs)

    def run(self, user_message: str):
        from rikugan.agent.turn import TurnEvent, TurnEventType

        yield TurnEvent(type=TurnEventType.TEXT_DONE, text="done")
        return None


class TestRunnerCancelEvent(unittest.TestCase):
    def _runner(self) -> SubagentRunner:
        return SubagentRunner(
            provider=_StubProvider(),
            tool_registry=ToolRegistry(),
            config=RikuganConfig(),
            host_name="test",
        )

    def test_build_loop_forwards_cancel_event(self) -> None:
        runner = self._runner()
        ev = threading.Event()
        runner._cancel_event = ev
        with patch("rikugan.agent.loop.AgentLoop", _FakeAgentLoop):
            loop = runner._build_loop(SessionState())
        assert isinstance(loop, _FakeAgentLoop)
        assert _FakeAgentLoop.captures[-1]["cancel_event"] is ev

    def test_independent_runs_get_independent_fallback_events(self) -> None:
        runner = self._runner()
        with patch("rikugan.agent.loop.AgentLoop", _FakeAgentLoop):
            a = runner._build_loop(SessionState())
            b = runner._build_loop(SessionState())
        assert a._cancelled is not b._cancelled


class TestRunnerModelOverride(unittest.TestCase):
    def test_no_override_preserves_provider_identity(self) -> None:
        provider = _StubProvider(model="parent-model")
        runner = SubagentRunner(
            provider=provider,
            tool_registry=ToolRegistry(),
            config=RikuganConfig(),
            host_name="test",
        )
        with patch("rikugan.agent.loop.AgentLoop", _FakeAgentLoop):
            loop = runner._build_loop(SessionState())
        assert loop.provider is provider
        assert loop.provider.model == "parent-model"
        assert provider.model == "parent-model"

    def test_override_uses_copy_and_does_not_mutate_parent(self) -> None:
        provider = _StubProvider(model="parent-model")
        cfg = RikuganConfig()
        cfg_before_model = cfg.provider.model
        runner = SubagentRunner(
            provider=provider,
            tool_registry=ToolRegistry(),
            config=cfg,
            host_name="test",
            model_override="child-model",
        )
        with patch("rikugan.agent.loop.AgentLoop", _FakeAgentLoop):
            loop = runner._build_loop(SessionState())
        assert loop.provider is not provider
        assert loop.provider.model == "child-model"
        assert provider.model == "parent-model"
        assert loop.config is not runner.config
        assert loop.config.provider.model == "child-model"
        assert runner.config.provider.model == cfg_before_model


class TestRunnerRespectsCancelEvent(unittest.TestCase):
    def test_cancel_event_reaches_provider_stream(self) -> None:
        """The cancel event must be forwarded into the chat_stream cancel_event slot."""
        from rikugan.agent.turn import TurnEvent, TurnEventType

        provider = _StubProvider()
        provider.scripted_packets = [
            StreamChunk(text="a"),
            StreamChunk(text="b"),
        ]

        captured_stream_cancel: dict = {}

        class _ShortLoop(_FakeAgentLoop):
            def run(self, user_message: str):
                ev = self._cancelled
                for chunk in self.provider.chat_stream([], cancel_event=ev):
                    captured_stream_cancel["event"] = ev
                    if chunk.text == "a":
                        ev.set()
                yield TurnEvent(type=TurnEventType.TEXT_DONE, text="ok")
                return None

        cancel = threading.Event()
        runner = SubagentRunner(
            provider=provider,
            tool_registry=ToolRegistry(),
            config=RikuganConfig(),
            host_name="test",
            cancel_event=cancel,
        )
        with patch("rikugan.agent.loop.AgentLoop", _ShortLoop):
            events = list(runner.run_task("do thing", max_turns=1))
        assert any(e.type == TurnEventType.TEXT_DONE and e.text == "ok" for e in events)


class TestRunnerMaxTurnsPlumbing(unittest.TestCase):
    """``SubagentRunner`` must forward ``max_turns`` to the constructed
    ``AgentLoop`` so the per-run budget is honoured as a hard ceiling.
    """

    def test_run_task_forwards_max_turns_to_agent_loop(self) -> None:
        captured: dict = {}

        class _Loop(_FakeAgentLoop):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                captured["max_turns"] = kwargs.get("max_turns")

        provider = _StubProvider()
        provider.scripted_packets = [StreamChunk(text="ok")]

        runner = SubagentRunner(
            provider=provider,
            tool_registry=ToolRegistry(),
            config=RikuganConfig(),
            host_name="test",
        )
        with patch("rikugan.agent.loop.AgentLoop", _Loop):
            list(runner.run_task("task", max_turns=7))
        assert captured["max_turns"] == 7

    def test_run_mode_forwards_max_turns_to_agent_loop(self) -> None:
        captured: dict = {}

        class _Loop(_FakeAgentLoop):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                captured["max_turns"] = kwargs.get("max_turns")

        provider = _StubProvider()
        provider.scripted_packets = [StreamChunk(text="ok")]

        runner = SubagentRunner(
            provider=provider,
            tool_registry=ToolRegistry(),
            config=RikuganConfig(),
            host_name="test",
        )
        with patch("rikugan.agent.loop.AgentLoop", _Loop):
            list(runner.run_mode("task", mode="normal", max_turns=12))
        assert captured["max_turns"] == 12

    def test_run_exploration_forwards_max_turns_to_agent_loop(self) -> None:
        captured: dict = {}

        class _Loop(_FakeAgentLoop):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                captured["max_turns"] = kwargs.get("max_turns")
                self.last_knowledge_base = None  # run_exploration reads this

            def run(self, user_message):
                return
                yield  # pragma: no cover - generator marker

        runner = SubagentRunner(
            provider=_StubProvider(),
            tool_registry=ToolRegistry(),
            config=RikuganConfig(),
            host_name="test",
        )
        with patch("rikugan.agent.loop.AgentLoop", _Loop):
            list(runner.run_exploration("goal", max_turns=9))
        assert captured["max_turns"] == 9


class TestRunnerMaxTurnsValidation(unittest.TestCase):
    """``max_turns`` validation at the runner boundary.

    ``max_turns=0`` is **invalid** (immediate-stop is not a supported
    mode and would otherwise be silently promoted to the 100-turn
    default via Python truthiness). The constructor rejects it with
    ``ValueError`` so the bug surfaces immediately instead of
    degrading silently.
    """

    def _runner(self) -> SubagentRunner:
        return SubagentRunner(
            provider=_StubProvider(),
            tool_registry=ToolRegistry(),
            config=RikuganConfig(),
            host_name="test",
        )

    def test_constructor_max_turns_zero_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            SubagentRunner(
                provider=_StubProvider(),
                tool_registry=ToolRegistry(),
                config=RikuganConfig(),
                host_name="test",
                max_turns=0,
            )
        assert "max_turns must be None or a positive int" in str(ctx.exception)

    def test_constructor_max_turns_negative_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            SubagentRunner(
                provider=_StubProvider(),
                tool_registry=ToolRegistry(),
                config=RikuganConfig(),
                host_name="test",
                max_turns=-1,
            )
        assert "max_turns must be None or a positive int" in str(ctx.exception)

    def test_constructor_max_turns_none_accepted(self) -> None:
        runner = SubagentRunner(
            provider=_StubProvider(),
            tool_registry=ToolRegistry(),
            config=RikuganConfig(),
            host_name="test",
            max_turns=None,
        )
        assert runner._max_turns is None

    def test_constructor_max_turns_positive_accepted(self) -> None:
        runner = SubagentRunner(
            provider=_StubProvider(),
            tool_registry=ToolRegistry(),
            config=RikuganConfig(),
            host_name="test",
            max_turns=12,
        )
        assert runner._max_turns == 12


class TestRunnerInheritsCentralMemory(unittest.TestCase):
    """A child loop must inherit the parent's central-memory wiring.

    ``AgentLoop.__init__`` advertises ``save_memory`` to the LLM whenever
    ``session.idb_path`` is set (exploration subagents get the parent's
    path), so a child without ``memory_service`` makes the LLM call a tool
    that can only ever answer "Central memory is not available in this
    context." Child loops therefore inherit both the service and the
    write authority from ``parent_loop``. The parallel
    ``SubagentManager`` workers have no ``parent_loop`` and stay unwired —
    they never advertise the tool.
    """

    def _parent(self, wired: bool) -> _FakeAgentLoop:
        parent = _FakeAgentLoop.__new__(_FakeAgentLoop)
        parent._cancelled = threading.Event()
        # Queues the real AgentLoop constructor inherits from parent_loop.
        for attr in ("_user_answer_queue", "_tool_approval_queue", "_approval_queue"):
            setattr(parent, attr, queue.Queue(maxsize=1))
        parent._always_allow_scripts = False
        parent.memory_service = object() if wired else None
        parent._memory_authority = object() if wired else None
        return parent

    def _build(self, parent: _FakeAgentLoop | None, session: SessionState):
        """Build a real child AgentLoop through the production constructor."""
        runner = SubagentRunner(
            provider=_StubProvider(),
            tool_registry=ToolRegistry(),
            config=RikuganConfig(),
            host_name="test",
            parent_loop=parent,
        )
        return runner._build_loop(session)

    def test_child_inherits_service_and_authority(self) -> None:
        parent = self._parent(wired=True)
        child = self._build(parent, SessionState(idb_path="/tmp/x.i64"))
        assert child.memory_service is parent.memory_service
        assert child._memory_authority is parent._memory_authority

    def test_child_does_not_inherit_case_manager(self) -> None:
        """``_memory_manager`` backs /case; that stays controller-owned."""
        parent = self._parent(wired=True)
        parent._memory_manager = object()
        child = self._build(parent, SessionState(idb_path="/tmp/x.i64"))
        assert not hasattr(child, "_memory_manager")

    def test_parentless_child_stays_unwired(self) -> None:
        """Parallel SubagentManager workers get no service and no tool."""
        child = self._build(None, SessionState())
        assert child.memory_service is None
        assert child._memory_authority is None


if __name__ == "__main__":
    unittest.main()
