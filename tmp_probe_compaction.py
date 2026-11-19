"""Temporary probe: compaction orphan-pair check + threshold behaviour. Deleted after use."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lucnhan.agent.context_window import ContextWindowManager  # noqa: E402
from lucnhan.core.types import Message, Role, ToolCall, ToolResult  # noqa: E402
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


def orphans(messages: list[Message]) -> tuple[list[str], list[str]]:
    """Return (tool_calls_without_result, tool_results_without_call)."""
    call_ids: set[str] = set()
    result_ids: set[str] = set()
    for m in messages:
        for tc in m.tool_calls:
            call_ids.add(tc.id)
        for tr in m.tool_results:
            result_ids.add(tr.tool_call_id)
    return sorted(call_ids - result_ids), sorted(result_ids - call_ids)


def report(label: str, messages: list[Message]) -> None:
    missing_res, orphan_res = orphans(messages)
    print(f"{label}: n={len(messages)} tool_use_without_result={missing_res} tool_result_without_tool_use={orphan_res}")


print("== A. compaction of a normal tool-use history ==")
s = build_session(4)
mgr = ContextWindowManager(max_tokens=1000, compaction_threshold=0.8)
compacted = mgr.compact_messages(s.messages)
report("session   ", s.messages)
report("compacted ", compacted)
print("  roles:", [m.role.value for m in compacted])
print("  summary:", repr(compacted[1].content[:220]))

print()
print("== B. compaction when the FIRST message is an assistant tool_use ==")
s2 = SessionState()
s2.add_message(
    Message(
        role=Role.ASSISTANT,
        content="",
        tool_calls=[ToolCall(id="tcX", name="x", arguments={})],
    )
)
s2.add_message(Message(role=Role.TOOL, tool_results=[ToolResult(tool_call_id="tcX", name="x", content="y")]))
s2.add_message(Message(role=Role.USER, content="u"))
for i in range(3):
    s2.add_message(Message(role=Role.ASSISTANT, content=f"a{i}"))
    s2.add_message(Message(role=Role.TOOL, tool_results=[ToolResult(tool_call_id=f"t{i}", name="n", content="z")]))
comp2 = mgr.compact_messages(s2.messages)
report("compactedB", comp2)
print("  roles:", [m.role.value for m in comp2])

print()
print("== C. provider payload after compaction (sanitize/trim) ==")
s3 = build_session(6)
s3.replace_messages(mgr.compact_messages(s3.messages))
payload = s3.get_messages_for_provider(context_window=0)
report("payloadC  ", payload)
print("  roles:", [m.role.value for m in payload])

print()
print("== D. dead path: <=6 messages, huge content ==")
s4 = SessionState()
s4.add_message(Message(role=Role.USER, content="u"))
for i in range(2):
    s4.add_message(Message(role=Role.ASSISTANT, content="a" * 200000))
    s4.add_message(
        Message(role=Role.TOOL, tool_results=[ToolResult(tool_call_id=f"t{i}", name="n", content="z" * 200000)])
    )
print("  len(messages) =", len(s4.messages))
out = mgr.compact_messages(s4.messages)
print("  compacted len =", len(out), "-> unchanged:", len(out) == len(s4.messages))
print("  usage_ratio before:", mgr.usage_ratio(), "should_compact:", mgr.should_compact())
mgr.update_usage(900)
print("  after update_usage(900): ratio", mgr.usage_ratio(), "should_compact:", mgr.should_compact())
