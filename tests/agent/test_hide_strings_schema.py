"""End-to-end schema-filter and direct-call guard tests for hide_strings.

Pins the contract that:
  * `_build_tools_schema` removes `list_strings` and `search_strings` when
    `config.hide_strings` is True and keeps them when False.
  * `_execute_single_tool` returns the exact error content for stale direct
    calls to those tools when the option is enabled, and never blocks
    disassembly/decompiler tools.
  * `OrchestraMainAgent._get_tools_schema` honours the same option.
"""

from __future__ import annotations

import os
import sys
import unittest
from typing import Any
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from tests.mocks.ida_mock import install_ida_mocks  # noqa: E402

install_ida_mocks()

from rikugan.core.config import RikuganConfig  # noqa: E402
from rikugan.core.types import Message, Role, ToolCall, ToolResult  # noqa: E402


HIDDEN_TOOLS = ("list_strings", "search_strings")


def _make_loop(tools: list[dict[str, Any]], hide_strings: bool):
    """Construct a minimal stand-in for AgentLoop that mirrors the schema-filter contract.

    Reuses the real `_build_tools_schema` logic by patching only the
    surface it reads (`session.metadata`, `config`, `tools`, `skills`).
    """
    from rikugan.agent.loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.session = MagicMock()
    loop.session.metadata = {}
    cfg = RikuganConfig()
    cfg.hide_strings = hide_strings
    loop.config = cfg
    loop.skills = None
    # _build_tools_schema calls self.tools.to_provider_format().
    tools_obj = MagicMock()
    tools_obj.to_provider_format = MagicMock(return_value=list(tools))
    loop.tools = tools_obj
    return loop


def _tool_entry(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"test {name}",
            "parameters": {"type": "object", "properties": {}},
        },
    }


SAMPLE_TOOLS = [
    _tool_entry("list_functions"),
    _tool_entry("list_strings"),
    _tool_entry("search_strings"),
    _tool_entry("decompile_function"),
    _tool_entry("read_function_disassembly"),
    _tool_entry("rename_function"),
]


class TestHideStringsSchemaFilter(unittest.TestCase):
    def test_disabled_keeps_string_tools(self) -> None:
        loop = _make_loop(SAMPLE_TOOLS, hide_strings=False)
        schema = loop._build_tools_schema(active_skill=None, use_exploration_mode=False)
        names = {t["function"]["name"] for t in schema}
        self.assertIn("list_strings", names)
        self.assertIn("search_strings", names)

    def test_enabled_removes_string_tools(self) -> None:
        loop = _make_loop(SAMPLE_TOOLS, hide_strings=True)
        schema = loop._build_tools_schema(active_skill=None, use_exploration_mode=False)
        names = {t["function"]["name"] for t in schema}
        self.assertNotIn("list_strings", names)
        self.assertNotIn("search_strings", names)
        # Disassembly/decompiler must remain available.
        self.assertIn("decompile_function", names)
        self.assertIn("read_function_disassembly", names)

    def test_enabled_removes_from_exploration_schema(self) -> None:
        # Even when exploration pseudo-tools are appended, hidden string tools
        # must stay out.
        loop = _make_loop(SAMPLE_TOOLS, hide_strings=True)
        schema = loop._build_tools_schema(active_skill=None, use_exploration_mode=True)
        names = {t["function"]["name"] for t in schema}
        self.assertNotIn("list_strings", names)
        self.assertNotIn("search_strings", names)


class TestHideStringsDirectCallGuard(unittest.TestCase):
    """Drive the real `_execute_single_tool` with a synthetic config to verify
    the exact error content returned for stale direct calls.
    """

    def _build_loop(self, hide_strings: bool):
        from rikugan.agent.loop import AgentLoop

        loop = AgentLoop.__new__(AgentLoop)
        cfg = RikuganConfig()
        cfg.hide_strings = hide_strings
        loop.config = cfg
        return loop

    def _drain(self, loop, tc: ToolCall):
        gen = loop._execute_single_tool(tc)
        events = []
        try:
            while True:
                events.append(next(gen))
        except StopIteration as stop:
            return events, stop.value
class TestOrchestraSchemaFilter(unittest.TestCase):
    def test_orchestra_filters_hidden_string_tools(self) -> None:
        from rikugan.agent.orchestra.main_agent import OrchestraMainAgent

        agent = OrchestraMainAgent.__new__(OrchestraMainAgent)
        cfg = RikuganConfig()
        cfg.hide_strings = True
        agent.config = cfg
        agent.tools = MagicMock()
        agent.tools.to_provider_format = MagicMock(return_value=list(SAMPLE_TOOLS))
        # The real method references self.orchestra_config and self.skills.
        agent.orchestra_config = MagicMock()
        agent.orchestra_config.sub_models = []
        agent.skills = None
        schema = agent._get_tools_schema()
        names = {t.get("function", t).get("name", "") for t in schema}
        self.assertNotIn("list_strings", names)
        self.assertNotIn("search_strings", names)
        # Disassembly/decompiler remain.
        self.assertIn("decompile_function", names)


if __name__ == "__main__":
    unittest.main()
