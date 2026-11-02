"""Tests for the one-shot goal continuation.

When an active goal is set and the agent stops without producing any
verifiable progress, it is easy to end the run with the goal half
done.  The continuation injects exactly one reminder and gives the
agent one more turn.
"""

from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from lucnhan.agent.loop import AgentLoop
from lucnhan.agent.loop_commands import ACTIVE_GOAL_METADATA_KEY
from lucnhan.agent.reminders import GOAL_PENDING_REMINDER
from lucnhan.core.config import LucNhanConfig
from lucnhan.core.types import Message, ModelInfo, ProviderCapabilities, StreamChunk
from lucnhan.providers.base import LLMProvider
from lucnhan.state.session import SessionState
from lucnhan.tools.registry import ToolRegistry


class TextOnlyProvider(LLMProvider):
    """Provider that always answers with plain text and no tool calls."""

    def __init__(self) -> None:
        super().__init__(api_key="test", model="mock-model")
        self.call_count = 0

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

    def _stream_chunks(self, client, kwargs, cancel_event=None):
        yield from ()

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
        self.call_count += 1
        yield StreamChunk(text="still thinking about it")


def _make_loop(*, goal_continuation: bool = True) -> AgentLoop:
    config = LucNhanConfig()
    config.auto_context = False
    config.goal_continuation_enabled = goal_continuation
    return AgentLoop(
        provider=TextOnlyProvider(),
        tool_registry=ToolRegistry(),
        config=config,
        session=SessionState(provider_name="mock", model_name="mock-model"),
    )


def _goal_reminders(loop: AgentLoop) -> list[Message]:
    return [m for m in loop.session.messages if m.role == "user" and GOAL_PENDING_REMINDER in (m.content or "")]


def test_goal_pending_triggers_one_extra_turn() -> None:
    loop = _make_loop()
    loop.session.metadata[ACTIVE_GOAL_METADATA_KEY] = "rename sub_401000"

    list(loop.run("do the thing"))

    # One extra turn: the original plus the continuation.
    assert loop.provider.call_count == 2
    assert len(_goal_reminders(loop)) == 1


def test_goal_continuation_fires_again_in_a_new_run() -> None:
    loop = _make_loop()
    loop.session.metadata[ACTIVE_GOAL_METADATA_KEY] = "rename sub_401000"

    list(loop.run("do the thing"))
    first = len(_goal_reminders(loop))
    assert first == 1

    list(loop.run("do it again"))

    # The reminder budget is per run, so a new request can fire again.
    assert len(_goal_reminders(loop)) == 2
    assert loop.provider.call_count == 4


def test_no_goal_means_no_continuation() -> None:
    loop = _make_loop()

    list(loop.run("just answer"))

    assert loop.provider.call_count == 1
    assert _goal_reminders(loop) == []


def test_disabled_goal_continuation_makes_a_single_turn() -> None:
    loop = _make_loop(goal_continuation=False)
    loop.session.metadata[ACTIVE_GOAL_METADATA_KEY] = "rename sub_401000"

    list(loop.run("do the thing"))

    assert loop.provider.call_count == 1
    assert _goal_reminders(loop) == []
