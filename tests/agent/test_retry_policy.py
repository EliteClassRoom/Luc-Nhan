"""Tests for the hardened retry policy.

The retry layer has three responsibilities that this module pins down:

* the backoff between retries is exponential, capped, and **jittered**,
  so concurrent clients that failed together do not retry in lockstep;
* an *empty* provider response (no text, no tool calls) is retried with
  a request-local suffix that asks the model to actually produce
  something, and the turn fails with a visible reason once the retries
  are exhausted;
* text that was already streamed to the user is never replayed — a
  broken stream after partial output keeps the partial result instead
  of restarting the request.
"""

from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from lucnhan.agent.loop import _EMPTY_RESPONSE_SUFFIX, AgentLoop
from lucnhan.agent.modes.turn_helpers import execute_single_turn
from lucnhan.core.config import LucNhanConfig
from lucnhan.core.errors import ProviderError
from lucnhan.core.types import (
    Message,
    ModelInfo,
    ProviderCapabilities,
    Role,
    StreamChunk,
)
from lucnhan.providers.base import LLMProvider
from lucnhan.state.session import SessionState
from lucnhan.tools.base import ParameterSchema, ToolDefinition
from lucnhan.tools.registry import ToolRegistry


class ScriptedProvider(LLMProvider):
    """Provider that replays a scripted list of responses.

    Each entry of *responses* is either

    * a list of :class:`StreamChunk` objects to yield,
    * an exception instance to raise before yielding anything, or
    * a callable returning an iterator (so a script can yield chunks
      and *then* raise).

    Every call records the effective system prompt (composed exactly the
    way the base class composes it) so a test can assert on what the
    model was actually told.
    """

    def __init__(self, responses: list[Any] | None = None):
        super().__init__(api_key="test", model="mock-model")
        self._responses = responses or []
        self.call_count = 0
        self.systems_sent: list[str] = []
        self.suffixes_sent: list[str] = []

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

    def _stream_chunks(self, client, kwargs, cancel_event=None):
        yield from ()

    def chat(self, messages, tools=None, temperature=0.3, max_tokens=4096, system="", **kwargs):
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
        effective_system, _max_tokens, _ctx = self._resolve_effective_kwargs(request_context, max_tokens, system)
        self.systems_sent.append(effective_system)
        self.suffixes_sent.append(request_context.system_suffix if request_context is not None else "")
        index = self.call_count
        self.call_count += 1
        item = self._responses[index] if index < len(self._responses) else []
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            item = item()
        yield from item


def _make_loop(provider: ScriptedProvider) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="echo",
            description="echo",
            parameters=[ParameterSchema(name="text", type="string", description="t", required=True)],
            handler=lambda text: f"Echo: {text}",
            category="test",
        )
    )
    config = LucNhanConfig()
    config.auto_context = False
    # Keep the retry sleeps short so the suite stays fast; the backoff
    # *shape* is asserted separately in test_backoff_is_jittered_and_capped.
    config.retry_base_delay_ms = 100
    config.retry_max_delay_ms = 500
    return AgentLoop(
        provider=provider,
        tool_registry=registry,
        config=config,
        session=SessionState(provider_name="mock", model_name="mock-model"),
    )


def _drain(generator: Any) -> list[Any]:
    """Consume *generator* and return the events it yielded."""
    events = []
    while True:
        try:
            events.append(next(generator))
        except StopIteration as stop:
            return events, stop.value


# ---------------------------------------------------------------------------
# Backoff shape
# ---------------------------------------------------------------------------


def test_backoff_is_jittered_and_capped() -> None:
    """The backoff grows exponentially, stays under the cap, and is jittered."""
    loop = _make_loop(ScriptedProvider())
    # ``config.retry_base_delay_ms`` is 100 here, so the expected window
    # is [0.075, 0.1] for attempt 0.
    assert 0.075 <= loop._retry_backoff(0) <= 0.1
    assert 0.15 <= loop._retry_backoff(1) <= 0.2
    # The cap wins for large attempts: 100ms * 2**10 clamped to 500ms.
    assert 0.375 <= loop._retry_backoff(10) <= 0.5

    # The default config (500ms base, 8000ms cap) has its own window.
    default_loop = _make_loop(ScriptedProvider())
    default_loop.config.retry_base_delay_ms = 500
    default_loop.config.retry_max_delay_ms = 8000
    assert 0.375 <= default_loop._retry_backoff(0) <= 0.5
    assert 6.0 <= default_loop._retry_backoff(10) <= 8.0

    # Jitter: repeated calls must not all return the same delay.
    delays = {default_loop._retry_backoff(4) for _ in range(20)}
    assert len(delays) > 1


# ---------------------------------------------------------------------------
# Empty response
# ---------------------------------------------------------------------------


def test_empty_response_retried_twice_then_errors() -> None:
    """An empty response is retried twice, then the turn fails visibly."""
    provider = ScriptedProvider(
        [
            [StreamChunk(finish_reason="stop")],
            [StreamChunk(finish_reason="stop")],
            [StreamChunk(finish_reason="stop")],
        ]
    )
    loop = _make_loop(provider)

    events, result = _drain(execute_single_turn(loop, "system", None))

    assert provider.call_count == 3
    # The first attempt carries no suffix; the two retries carry it.
    assert provider.suffixes_sent[0] == ""
    assert provider.suffixes_sent[1] == _EMPTY_RESPONSE_SUFFIX
    assert provider.suffixes_sent[2] == _EMPTY_RESPONSE_SUFFIX
    assert _EMPTY_RESPONSE_SUFFIX in provider.systems_sent[1]
    assert _EMPTY_RESPONSE_SUFFIX in provider.systems_sent[2]
    # The turn ends with a visible error, not a silently empty answer.
    assert result.error is not None
    assert result.ok is False
    assert any("empty response" in str(getattr(e, "error", "")) for e in events)


# ---------------------------------------------------------------------------
# Committed output
# ---------------------------------------------------------------------------


def test_committed_text_is_never_replayed() -> None:
    """A stream that breaks after streaming text is not restarted."""

    def text_then_raise():
        yield StreamChunk(text="hi")
        raise ProviderError("connection reset", retryable=True)

    provider = ScriptedProvider([text_then_raise])
    loop = _make_loop(provider)

    _events, result = _drain(execute_single_turn(loop, "system", None))

    # Exactly one provider call: the partial text is kept, not replayed.
    assert provider.call_count == 1
    assert result.text == "hi"
    assert result.ok is True
