"""Tests for SubAgentFactory.spec -> SubagentManager.spawn propagation."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.agent.orchestra.orchestra_config import SubAgentSpec
from rikugan.agent.orchestra.subagent_factory import SubAgentFactory


class _FakeManager:
    """Captures SubagentManager.spawn / register calls without running anything."""

    def __init__(self) -> None:
        self.spawn_calls: list[dict] = []
        self.register_calls: list[dict] = []

    def spawn(self, **kwargs):  # noqa: ANN001 - signature mirrors SubagentManager.spawn
        self.spawn_calls.append(kwargs)
        return "fake-id"

    def register(self, **kwargs):
        self.register_calls.append(kwargs)
        return "fake-id"

    def update_external(self, *args, **kwargs):
        return None


class TestSubAgentFactoryPropagation(unittest.TestCase):
    def _factory(self) -> tuple[SubAgentFactory, _FakeManager]:
        mgr = _FakeManager()
        return SubAgentFactory(manager=mgr, config=None), mgr  # type: ignore[arg-type]

    def test_spawn_forwards_tools_and_model(self) -> None:
        factory, mgr = self._factory()
        spec = SubAgentSpec(
            instruction="do thing",
            tools=["alpha_tool", "beta_tool"],
            model="child-model",
            max_steps=10,
            name="worker",
            mode="exploration",
        )
        agent_id = factory.spawn(spec)
        assert agent_id == "fake-id"
        assert len(mgr.spawn_calls) == 1
        call = mgr.spawn_calls[0]
        assert call["tools"] == ["alpha_tool", "beta_tool"]
        assert call["model"] == "child-model"
        assert call["mode"] == "exploration"
        assert call["max_turns"] == 10
        assert call["task"] == "do thing"
        assert call["name"] == "worker"
        assert call["agent_type"] == "orchestra"

    def test_spawn_with_context_forwards_tools_and_model(self) -> None:
        factory, mgr = self._factory()
        spec = SubAgentSpec(
            instruction="do thing",
            tools=["alpha_tool"],
            model="other-model",
            max_steps=7,
            name="worker",
            mode="",
        )
        factory.spawn_with_context(spec, full_context="ctx body")
        assert len(mgr.spawn_calls) == 1
        call = mgr.spawn_calls[0]
        assert call["tools"] == ["alpha_tool"]
        assert call["model"] == "other-model"
        assert "## Context" in call["task"]
        assert "ctx body" in call["task"]
        assert call["max_turns"] == 7

    def test_default_spec_forwards_empty_lists(self) -> None:
        factory, mgr = self._factory()
        spec = SubAgentSpec(instruction="do thing", name="worker")
        factory.spawn(spec)
        assert mgr.spawn_calls[0]["tools"] == []
        assert mgr.spawn_calls[0]["model"] == ""

    def test_register_external_does_not_forward_exec_settings(self) -> None:
        """register_external only registers display metadata; tools/model are not used."""
        factory, mgr = self._factory()
        spec = SubAgentSpec(
            instruction="do thing",
            tools=["alpha_tool"],
            model="child-model",
            name="worker",
        )
        factory.register_external(spec)
        # register_external must not call spawn.
        assert mgr.spawn_calls == []
        assert len(mgr.register_calls) == 1


if __name__ == "__main__":
    unittest.main()
