"""Per-chat "allow" for scripts must never widen to other tools, and vice versa."""

from __future__ import annotations

import unittest

from lucnhan import constants
from lucnhan.core.types import ToolCall
from tests.agent.test_idapython_docs_gate import _make_loop


def _drive(loop, tool_name: str, decision: str | None):
    """Run the approval gate; return (approved, prompt_count)."""
    if decision is not None:
        loop._tool_approval_queue.put(decision)
    gen = loop._wait_for_approval(ToolCall(id="t", name=tool_name, arguments={}))
    prompts = 0
    try:
        while True:
            next(gen)
            prompts += 1
    except StopIteration as stop:
        return stop.value, prompts


class TestChatAllow(unittest.TestCase):
    def test_script_allow_all_skips_later_script_prompts(self):
        loop = _make_loop(gate_enabled=False)
        self.assertEqual(_drive(loop, constants.EXECUTE_PYTHON_TOOL_NAME, "allow_all"), (True, 1))
        self.assertEqual(_drive(loop, constants.EXECUTE_PYTHON_TOOL_NAME, None), (True, 0))

    def test_script_allow_all_does_not_allow_other_tools(self):
        loop = _make_loop(gate_enabled=False)
        _drive(loop, constants.EXECUTE_PYTHON_TOOL_NAME, "allow_all")
        self.assertEqual(_drive(loop, "patch_bytes", "deny"), (False, 1))

    def test_tool_allow_all_does_not_allow_scripts(self):
        loop = _make_loop(gate_enabled=False)
        _drive(loop, "patch_bytes", "allow_all")
        self.assertEqual(_drive(loop, constants.EXECUTE_PYTHON_TOOL_NAME, "deny"), (False, 1))


if __name__ == "__main__":
    unittest.main()
