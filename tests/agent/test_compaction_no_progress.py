"""Tests for the compaction no-progress suspension.

Compaction is supposed to shrink the conversation.  When it does not —
because the history is dominated by messages the compactor cannot
summarise — running it again and again is pure waste: the same
messages are rewritten, the same tokens are spent, and the context
never gets smaller.

The guard counts compaction passes that reclaimed less than 10 % of the
tracked usage and suspends automatic compaction after two of them.  The
suspension lifts once the history grew enough for a fresh attempt.
"""

from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from lucnhan.agent.loop import AgentLoop
from lucnhan.core.config import LucNhanConfig
from lucnhan.core.types import Message, ModelInfo, ProviderCapabilities, Role
from lucnhan.providers.base import LLMProvider
from lucnhan.state.session import SessionState
from lucnhan.tools.registry import ToolRegistry


class _Provider(LLMProvider):
    """A provider that is never actually called by these tests."""

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


def _make_loop() -> AgentLoop:
    config = LucNhanConfig()
    config.auto_context = False
    # A tiny window so compaction is always due.
    config.provider.context_window = 1000
    loop = AgentLoop(
        provider=_Provider(),
        tool_registry=ToolRegistry(),
        config=config,
        session=SessionState(provider_name="mock", model_name="mock-model"),
    )
    loop._context_manager.max_tokens = 1000
    loop._context_manager.compaction_threshold = 0.8
    return loop


def _fill(loop: AgentLoop, count: int, tag: str = "m") -> None:
    """Add *count* bulky messages so the context is over the threshold."""
    for i in range(count):
        loop.session.add_message(Message(role=Role.USER, content=f"{tag} {i} " + "x" * 200))


def test_compaction_that_reclaims_nothing_is_suspended() -> None:
    loop = _make_loop()
    _fill(loop, 40)

    calls = {"n": 0}

    def no_op_compact(messages: list[Message]) -> list[Message]:
        # A *new* list with the same messages: compaction "ran" but
        # reclaimed nothing.
        calls["n"] += 1
        return list(messages)

    loop._context_manager.compact_messages = no_op_compact  # type: ignore[method-assign]

    _messages, _tokens, _usage, warning = loop._prepare_provider_messages("system")
    assert warning is None
    assert loop._compaction_no_progress == 1
    assert loop._compaction_suspended is False

    _messages, _tokens, _usage, warning = loop._prepare_provider_messages("system")
    assert warning is not None
    assert "compaction" in warning.lower()
    assert loop._compaction_suspended is True
    assert calls["n"] == 2


def test_suspended_compaction_is_skipped_until_the_history_grows() -> None:
    loop = _make_loop()
    _fill(loop, 40)

    def no_op_compact(messages: list[Message]) -> list[Message]:
        return list(messages)

    loop._context_manager.compact_messages = no_op_compact  # type: ignore[method-assign]

    for _ in range(2):
        loop._prepare_provider_messages("system")
    assert loop._compaction_suspended is True
    suspended_at = loop._compaction_suspend_at

    calls = {"n": 0}

    def counting_compact(messages: list[Message]) -> list[Message]:
        calls["n"] += 1
        return list(messages)

    loop._context_manager.compact_messages = counting_compact  # type: ignore[method-assign]

    # A small growth is not enough to resume.
    _fill(loop, 1, tag="small")
    loop._prepare_provider_messages("system")
    assert loop._compaction_suspended is True
    assert calls["n"] == 0

    # A 20 %+ growth resumes compaction.
    target = max(suspended_at + 1, int(suspended_at * 1.2))
    _fill(loop, target - len(loop.session.messages), tag="big")
    loop._prepare_provider_messages("system")
    assert loop._compaction_suspended is False
    assert calls["n"] == 1


def test_compaction_that_shrinks_the_history_keeps_the_counter_at_zero() -> None:
    loop = _make_loop()
    _fill(loop, 40)

    def shrinking_compact(messages: list[Message]) -> list[Message]:
        # Keep only the head and the tail: the context really shrinks.
        return [*messages[:1], *messages[-2:]]

    loop._context_manager.compact_messages = shrinking_compact  # type: ignore[method-assign]

    loop._prepare_provider_messages("system")
    assert loop._compaction_no_progress == 0
    assert loop._compaction_suspended is False

    # The compacted history is small, so the next pass is not even due —
    # the counter must still be zero.
    loop._prepare_provider_messages("system")
    assert loop._compaction_no_progress == 0
    assert loop._compaction_suspended is False
