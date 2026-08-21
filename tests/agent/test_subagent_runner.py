"""Tests for the SubagentRunner -> AgentLoop cancellation/model wiring."""
from __future__ import annotations

import os
import sys
import threading
import unittest
from typing import ClassVar
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.core.config import RikuganConfig
from rikugan.core.types import ProviderCapabilities, StreamChunk
from rikugan.providers.base import LLMProvider, ModelInfo
from rikugan.state.session import SessionState
from rikugan.tools.registry import ToolRegistry
from rikugan.agent.subagent import SubagentRunner


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
        assert captured_stream_cancel["event"] is cancel


if __name__ == "__main__":
    unittest.main()
