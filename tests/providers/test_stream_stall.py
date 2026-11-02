"""Tests for the provider stream stall watchdog.

A provider that stops delivering data mid-stream leaves the agent
waiting forever.  The watchdog lives in the provider base class: a
progress gate is handed to the provider as its ``cancel_event``, so
every existing provider (which all honour that event) is covered
without touching a single provider implementation.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from lucnhan.core.errors import StreamStallError
from lucnhan.core.types import (
    Message,
    ModelInfo,
    ProviderCapabilities,
    StreamChunk,
)
from lucnhan.providers.base import LLMProvider, _ProgressGate


class _BaseProvider(LLMProvider):
    """Minimal concrete provider used by the stall tests."""

    @property
    def name(self) -> str:
        return "mock"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    def _get_client(self):
        return None

    def _fetch_models_live(self) -> list[ModelInfo]:
        return []

    @staticmethod
    def _builtin_models() -> list[ModelInfo]:
        return []

    def _format_messages(self, messages: list[Message]) -> Any:
        return messages

    def _normalize_response(self, raw: Any) -> Any:
        return raw

    def _build_request_kwargs(self, messages, tools, temperature, max_tokens, system, **kwargs):
        return {}

    def _call_api(self, client, kwargs):
        return None

    def _handle_api_error(self, e):
        raise e


# ---------------------------------------------------------------------------
# Gate unit tests
# ---------------------------------------------------------------------------


def test_gate_trips_when_no_data_arrives() -> None:
    gate = _ProgressGate(None, 0.1, 0.1)
    time.sleep(0.15)
    assert gate.poll_stall() is True
    assert gate.stalled is True
    assert gate.stalled_after >= 0.1
    assert gate.is_set() is True


def test_gate_does_not_trip_while_data_arrives() -> None:
    gate = _ProgressGate(None, 0.1, 0.1)
    for _ in range(10):
        gate.touch()
        time.sleep(0.02)
    assert gate.poll_stall() is False
    assert gate.stalled is False
    assert gate.is_set() is False


def test_gate_never_trips_when_both_timeouts_are_zero() -> None:
    gate = _ProgressGate(None, 0, 0)
    time.sleep(0.2)
    assert gate.poll_stall() is False
    assert gate.is_set() is False


def test_gate_mirrors_the_upstream_cancel_event() -> None:
    import threading

    upstream = threading.Event()
    gate = _ProgressGate(upstream, 0, 0)
    assert gate.is_set() is False

    upstream.set()
    assert gate.is_set() is True
    assert gate.wait(timeout=0.1) is True


def test_gate_uses_the_idle_timeout_after_the_first_chunk() -> None:
    gate = _ProgressGate(None, 10.0, 0.1)
    gate.touch()
    time.sleep(0.15)
    assert gate.poll_stall() is True


# ---------------------------------------------------------------------------
# Provider wrapper
# ---------------------------------------------------------------------------


class StallingProvider(_BaseProvider):
    """A provider whose stream never delivers anything."""

    def _stream_chunks(self, client, kwargs, cancel_event=None):
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return
            time.sleep(0.01)
        yield StreamChunk(text="never reached")

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
    ):
        yield from LLMProvider.chat_stream(
            self,
            messages,
            tools,
            temperature,
            max_tokens,
            system,
            cancel_event,
            request_context=request_context,
        )


def test_stalling_stream_raises_stream_stall_error() -> None:
    provider = StallingProvider()
    provider.stream_first_progress_timeout = 0.2
    provider.stream_idle_timeout = 0.2

    try:
        list(provider.chat_stream([], cancel_event=None))
    except StreamStallError as exc:
        assert "stalled" in str(exc)
        assert exc.retryable is True
    else:  # pragma: no cover — the stall must always trip
        raise AssertionError("expected StreamStallError")


class PartialThenStallingProvider(_BaseProvider):
    """Delivers one chunk, then goes silent."""

    def _stream_chunks(self, client, kwargs, cancel_event=None):
        yield StreamChunk(text="partial")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return
            time.sleep(0.01)

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
    ):
        yield from LLMProvider.chat_stream(
            self,
            messages,
            tools,
            temperature,
            max_tokens,
            system,
            cancel_event,
            request_context=request_context,
        )


def test_stall_after_partial_output_raises() -> None:
    provider = PartialThenStallingProvider()
    provider.stream_first_progress_timeout = 0.2
    provider.stream_idle_timeout = 0.2

    chunks = []
    try:
        for chunk in provider.chat_stream([], cancel_event=None):
            chunks.append(chunk)
    except StreamStallError:
        pass
    else:  # pragma: no cover — the stall must always trip
        raise AssertionError("expected StreamStallError")

    assert [c.text for c in chunks] == ["partial"]


def test_disabled_timeouts_never_stall() -> None:
    provider = StallingProvider()
    provider.stream_first_progress_timeout = 0
    provider.stream_idle_timeout = 0

    # The provider eventually yields; no stall error is raised.
    chunks = list(provider.chat_stream([], cancel_event=None))
    assert [c.text for c in chunks] == ["never reached"]


# ---------------------------------------------------------------------------
# Agent loop integration
# ---------------------------------------------------------------------------


class ScriptedStallProvider(_BaseProvider):
    """Provider that stalls on the scripted calls and answers otherwise.

    A *stalling* call yields nothing at all; a *partial* call yields one
    text chunk and then goes silent.
    """

    def __init__(self, script: list[str]) -> None:
        super().__init__(api_key="test", model="mock-model")
        self._script = script
        self.call_count = 0
        self._mode = "done"

    def _stream_chunks(self, client, kwargs, cancel_event=None):
        mode = self._mode
        if mode == "done":
            yield StreamChunk(text="done")
            return
        if mode == "partial":
            yield StreamChunk(text="partial")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return
            time.sleep(0.01)

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
    ):
        index = self.call_count
        self.call_count += 1
        self._mode = self._script[index] if index < len(self._script) else "done"
        yield from LLMProvider.chat_stream(
            self,
            messages,
            tools,
            temperature,
            max_tokens,
            system,
            cancel_event,
            request_context=request_context,
        )


def _make_loop(provider: ScriptedStallProvider):
    from lucnhan.agent.loop import AgentLoop
    from lucnhan.core.config import LucNhanConfig
    from lucnhan.state.session import SessionState
    from lucnhan.tools.registry import ToolRegistry

    config = LucNhanConfig()
    config.auto_context = False
    config.stream_first_progress_timeout_seconds = 0.2
    config.stream_idle_timeout_seconds = 0.2
    config.retry_base_delay_ms = 100
    config.retry_max_delay_ms = 200
    loop = AgentLoop(
        provider=provider,
        tool_registry=ToolRegistry(),
        config=config,
        session=SessionState(provider_name="mock", model_name="mock-model"),
    )
    return loop


def test_stalled_turn_continues_and_keeps_the_partial_text() -> None:
    from lucnhan.agent.reminders import STREAM_STALL_REMINDER

    provider = ScriptedStallProvider(["partial", "done"])
    loop = _make_loop(provider)

    list(loop.run("hello"))

    # Two turns: the stalled one and the one that finished the answer.
    assert provider.call_count == 2

    # The partial text was persisted, not thrown away.  Roles are
    # compared by value: a sibling test can purge ``lucnhan.core.types``
    # from ``sys.modules``, so the enum identity is not stable across a
    # whole-suite run.
    assistant_texts = [m.content for m in loop.session.messages if m.role == "assistant"]
    assert any("partial" in text for text in assistant_texts)

    # Exactly one stall reminder was injected.
    reminders = [m for m in loop.session.messages if m.role == "user" and STREAM_STALL_REMINDER in (m.content or "")]
    assert len(reminders) == 1


def test_repeated_stalls_end_the_run_with_an_error() -> None:
    from lucnhan.agent.turn import TurnEventType

    provider = ScriptedStallProvider(["stall"] * 10)
    loop = _make_loop(provider)

    events = list(loop.run("hello"))

    # The run terminates instead of looping forever, and the failure is
    # reported to the user.
    assert events, "expected events"
    assert any(e.type is TurnEventType.ERROR for e in events)
    assert provider.call_count < 10


def test_turn_result_reports_the_stall() -> None:
    """The turn result carries ``stalled`` so the mode runner can continue."""
    from lucnhan.agent.modes.turn_helpers import execute_single_turn

    provider = ScriptedStallProvider(["partial", "done"])
    loop = _make_loop(provider)
    # ``AgentLoop.run`` refreshes these from the config; driving a
    # single turn directly means setting them here.
    provider.stream_first_progress_timeout = 0.2
    provider.stream_idle_timeout = 0.2

    generator = execute_single_turn(loop, "system", None)
    result = None
    while True:
        try:
            next(generator)
        except StopIteration as stopped:
            result = stopped.value
            break

    assert result is not None
    assert result.stalled is True
    assert result.text == "partial"
    assert result.ok is True
