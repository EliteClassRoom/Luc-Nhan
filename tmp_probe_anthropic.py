"""Temporary probe: Anthropic payload shape after compaction. Deleted after use."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lucnhan.agent.context_window import ContextWindowManager  # noqa: E402
from lucnhan.core.types import Message, Role, ToolCall, ToolResult  # noqa: E402
from lucnhan.providers.anthropic_provider import AnthropicProvider  # noqa: E402
from lucnhan.state.session import SessionState  # noqa: E402


def build_session(n_turns: int) -> SessionState:
    s = SessionState()
    s.add_message(Message(role=Role.USER, content="u0"))
    for i in range(n_turns):
        tc = ToolCall(id=f"tc{i}", name="decompile_function", arguments={"address": "0x401000"})
        s.add_message(Message(role=Role.ASSISTANT, content=f"a{i}", tool_calls=[tc]))
        s.add_message(
            Message(
                role=Role.TOOL,
                tool_results=[ToolResult(tool_call_id=f"tc{i}", name="decompile_function", content="x" * 50)],
            )
        )
    return s


class _Dummy:
    def _is_valid_anthropic_raw_parts(self, raw):
        return False
for label, msgs in (
    ("no compaction ", build_session(3).messages),
    ("after compact ", None),
):
    if msgs is None:
        mgr = ContextWindowManager(max_tokens=1000, compaction_threshold=0.8)
        s = build_session(4)
        msgs = mgr.compact_messages(s.messages)
    out = AnthropicProvider._format_messages(_Dummy(), msgs)
    roles = [b["role"] for b in out]
    print(f"{label}: anthropic roles = {roles}")
    bad = [i for i in range(1, len(roles)) if roles[i] == roles[i - 1] == "user"]
    print(f"   consecutive-user index pairs: {bad}")
    for i in bad:
        print(f"   -> block {i - 1}: {str(out[i - 1])[:90]}")
        print(f"   -> block {i}: {str(out[i])[:90]}")
