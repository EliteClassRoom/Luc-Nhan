"""Tests for superseded / duplicate tool-result elision.

A long reverse-engineering session reads the same function over and
over.  Once a *newer* read of the same target exists, the older result
is dead weight: the model already has the newer content.  The session
layer elides those results so the context keeps only what is still
current — without breaking the assistant/tool-call pairing the provider
requires.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from lucnhan.core.types import Message, Role, ToolCall, ToolResult
from lucnhan.state.session import SessionState


def _read_pair(call_id: str, address: str, content: str) -> tuple[Message, Message]:
    """Return the (assistant, tool) message pair of one read."""
    assistant = Message(
        role=Role.ASSISTANT,
        content="",
        tool_calls=[ToolCall(id=call_id, name="decompile_function", arguments={"address": address})],
    )
    tool = Message(
        role=Role.TOOL, tool_results=[ToolResult(tool_call_id=call_id, name="decompile_function", content=content)]
    )
    return assistant, tool


def _filler(index: int) -> tuple[Message, Message]:
    """A neutral read used to pad the conversation."""
    return _read_pair(f"filler_{index}", f"0x40{index:04x}", f"filler body {index}")


def _build_session() -> SessionState:
    """A 12-message session with two reads of the same address."""
    session = SessionState()
    session.add_message(Message(role=Role.USER, content="analyze it"))
    assistant, tool = _read_pair("read_1", "0x401000", "old pseudocode")
    session.add_message(assistant)
    session.add_message(tool)
    for i in range(3):
        a, t = _filler(i)
        session.add_message(a)
        session.add_message(t)
    assistant, tool = _read_pair("read_2", "0x401000", "new pseudocode")
    session.add_message(assistant)
    session.add_message(tool)
    for i in range(3, 5):
        a, t = _filler(i)
        session.add_message(a)
        session.add_message(tool)
    return session


def _tool_contents(messages: list[Message]) -> list[str]:
    return [tr.content for m in messages if m.role == "tool" for tr in m.tool_results]


def test_superseded_read_is_blanked_and_the_newer_one_kept() -> None:
    session = _build_session()
    messages = session.get_messages_for_provider(drop_stale=True)
    contents = _tool_contents(messages)

    assert "[Superseded by later read]" in contents
    assert "new pseudocode" in contents
    assert "old pseudocode" not in contents


def test_tool_call_pairing_survives_elision() -> None:
    session = _build_session()
    messages = session.get_messages_for_provider(drop_stale=True)

    requested: set[str] = set()
    for msg in messages:
        for tc in msg.tool_calls:
            requested.add(tc.id)
    answered = {tr.tool_call_id for msg in messages if msg.role is Role.TOOL for tr in msg.tool_results}

    assert requested == answered


def test_identical_results_are_marked_as_duplicates() -> None:
    session = SessionState()
    session.add_message(Message(role=Role.USER, content="go"))
    assistant, tool = _read_pair("s_1", "0x401000", "same body")
    session.add_message(assistant)
    session.add_message(tool)
    for i in range(4):
        a, t = _filler(i)
        session.add_message(a)
        session.add_message(t)
    # A different target, but byte-identical content.
    assistant, tool = _read_pair("s_2", "0x402000", "same body")
    session.add_message(assistant)
    session.add_message(tool)
    # Push the second read out of the keep-tail window.
    for i in range(4, 6):
        a, t = _filler(i)
        session.add_message(a)
        session.add_message(t)

    contents = _tool_contents(session.get_messages_for_provider(drop_stale=True))
    assert "[Duplicate of a later result]" in contents
    assert contents.count("same body") == 1


def test_elision_is_opt_in() -> None:
    session = _build_session()
    contents = _tool_contents(session.get_messages_for_provider(drop_stale=False))
    assert "old pseudocode" in contents
    assert "[Superseded by later read]" not in contents


def test_preserve_context_keeps_every_result() -> None:
    session = _build_session()
    contents = _tool_contents(session.get_messages_for_provider(preserve_context=True, drop_stale=True))
    assert "old pseudocode" in contents
    assert "[Superseded by later read]" not in contents


def test_error_results_are_never_blanked() -> None:
    session = SessionState()
    session.add_message(Message(role=Role.USER, content="go"))
    filler_index = 0
    for call_id in ("e_1", "e_2"):
        assistant = Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id=call_id, name="decompile_function", arguments={"address": "0x401000"})],
        )
        session.add_message(assistant)
        session.add_message(
            Message(
                role=Role.TOOL,
                tool_results=[
                    ToolResult(tool_call_id=call_id, name="decompile_function", content="Error: boom", is_error=True)
                ],
            )
        )
        for _ in range(4):
            a, t = _filler(filler_index)
            filler_index += 1
            session.add_message(a)
            session.add_message(t)

    contents = _tool_contents(session.get_messages_for_provider(drop_stale=True))
    assert contents.count("Error: boom") == 2
    assert "[Superseded by later read]" not in contents


def test_results_inside_the_keep_tail_are_never_blanked() -> None:
    """The newest results must survive verbatim — the model needs them."""
    session = SessionState()
    session.add_message(Message(role=Role.USER, content="go"))
    for i in range(6):
        a, t = _filler(i)
        session.add_message(a)
        session.add_message(t)
    # Two reads of the same target inside the keep-tail window.
    for call_id, body in (("k_1", "first body"), ("k_2", "second body")):
        assistant, tool = _read_pair(call_id, "0x401000", body)
        session.add_message(assistant)
        session.add_message(tool)

    contents = _tool_contents(session.get_messages_for_provider(drop_stale=True))
    assert "first body" in contents
    assert "second body" in contents
