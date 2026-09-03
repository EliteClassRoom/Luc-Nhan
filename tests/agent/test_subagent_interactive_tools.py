"""Tests for unattended-subagent interactive-tool filtering + mutation propagation.

Two deadlock/consistency contracts:

1. Subagents spawned WITHOUT a ``parent_loop`` (bulk-renamer deep workers,
   ``SubagentManager`` background threads) have nobody answering their
   approval/question queues. The child's tool surface must therefore exclude
   interactive gates — ``execute_python``, every ``requires_approval`` tool,
   ``ask_user`` — and a hallucinated call must get an immediate error result,
   never a blocking wait in ``AgentLoop._wait_for_queue``.

2. When a ``parent_loop`` exists, the child run's mutation records must be
   copied into the parent's ``_mutation_log`` after the run so ``/undo``
   reverses subagent mutations too.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan import constants
from rikugan.agent.loop import AgentLoop
from rikugan.agent.subagent import SubagentRunner
from rikugan.agent.turn import TurnEventType
from rikugan.agent.mutation import MutationRecord
from rikugan.core.config import RikuganConfig
from rikugan.core.types import (
    ModelInfo,
    ProviderCapabilities,
    StreamChunk,
    TokenUsage,
)
from rikugan.providers.base import LLMProvider
from rikugan.state.session import SessionState
from rikugan.tools.base import ParameterSchema, ToolDefinition
from rikugan.tools.registry import ToolRegistry

#: Wall-clock budget for a child run. Unattended + interactive tool used to
#: deadlock forever in _wait_for_queue; the budget turns that hang into a
#: clean test failure instead of a stuck CI run.
_RUN_DEADLINE_SECONDS = 15.0


class ScriptedProvider(LLMProvider):
    """Provider stub scripting chat_stream responses turn by turn."""

    def __init__(self, responses: list[list[StreamChunk]] | None = None):
        super().__init__(api_key="test", model="mock-model")
        self._responses = responses or []
        self._call_count = 0

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

    def _format_messages(self, messages):
        return messages

    def _normalize_response(self, raw):
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
        self, messages, tools=None, temperature=0.3, max_tokens=4096, system="", cancel_event=None, **kwargs
    ):
        if self._call_count < len(self._responses):
            chunks = self._responses[self._call_count]
            self._call_count += 1
            yield from chunks
        else:
            yield StreamChunk(text="No more scripted responses.")


def _text_response(text: str) -> list[StreamChunk]:
    return [
        StreamChunk(text=text),
        StreamChunk(usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)),
    ]


def _tool_call_response(tool_name: str, args: dict[str, Any], call_id: str) -> list[StreamChunk]:
    return [
        StreamChunk(is_tool_call_start=True, tool_call_id=call_id, tool_name=tool_name),
        StreamChunk(tool_args_delta=json.dumps(args), tool_call_id=call_id),
        StreamChunk(is_tool_call_end=True, tool_call_id=call_id, tool_name=tool_name),
        StreamChunk(usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)),
    ]


def _interactive_registry(executed: list[str]) -> ToolRegistry:
    """Registry with one tool per interactive-filter branch, plus a safe tool."""
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name=constants.EXECUTE_PYTHON_TOOL_NAME,
            description="Executes agent-authored code",
            parameters=[ParameterSchema(name="code", type="string", description="code", required=True)],
            handler=lambda code: (executed.append(code), f"ran {code}")[1],
        )
    )
    registry.register(
        ToolDefinition(
            name="guarded_tool",
            description="Gated by the requires_approval flag",
            parameters=[ParameterSchema(name="code", type="string", description="code", required=True)],
            handler=lambda code: (executed.append(code), f"ran {code}")[1],
            requires_approval=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="echo_tool",
            description="Safe read-only tool",
            parameters=[ParameterSchema(name="text", type="string", description="text", required=True)],
            handler=lambda text: f"echo {text}",
        )
    )
    return registry


def _rename_registry(state: dict) -> ToolRegistry:
    """Registry with rename_function + its getter so the mutation is reversible."""
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="rename_function",
            description="Rename a function",
            parameters=[
                ParameterSchema(name="address", type="string"),
                ParameterSchema(name="new_name", type="string"),
            ],
            mutating=True,
            handler=lambda address="", new_name="", s=state: (
                s.update(name=new_name) or f"Renamed {address} to {new_name}"
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="get_function_name",
            description="Get the current function name",
            parameters=[ParameterSchema(name="address", type="string")],
            mutating=False,
            handler=lambda address="", s=state: s["name"],
        )
    )
    return registry


def _config() -> RikuganConfig:
    config = RikuganConfig()
    config.auto_context = False  # Skip IDA API calls
    return config


class _DeadlineTimeout(AssertionError):
    """Raised when a child run blows the wall-clock deadline (deadlock)."""


def _run_with_deadline(generator, timeout: float = _RUN_DEADLINE_SECONDS) -> list:
    """Consume *generator* on a helper thread; fail cleanly if it deadlocks."""
    outcome: dict[str, Any] = {}

    def _consume() -> None:
        try:
            outcome["events"] = list(generator)
        except BaseException as e:
            outcome["error"] = e

    worker = threading.Thread(target=_consume, daemon=True, name="subagent-test-run")
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise _DeadlineTimeout(
            f"subagent run did not finish within {timeout:.0f}s — "
            "it blocked on an interactive gate with nobody attached to answer"
        )
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("events", [])


class TestUnattendedInteractiveFiltering(unittest.TestCase):
    """Children spawned without parent_loop must never block on a gate."""

    def test_unattended_child_errors_on_gated_tool_and_completes(self):
        executed: list[str] = []
        provider = ScriptedProvider(
            responses=[
                _tool_call_response("guarded_tool", {"code": "boom"}, call_id="call_1"),
                _tool_call_response(constants.EXECUTE_PYTHON_TOOL_NAME, {"code": "2+2"}, call_id="call_2"),
                _text_response("Finished without the gated tools."),
            ]
        )
        runner = SubagentRunner(
            provider=provider,
            tool_registry=_interactive_registry(executed),
            config=_config(),
            host_name="IDA Pro",
        )

        events = _run_with_deadline(runner.run_task("try the gated tools"))

        # The gated tools must never execute.
        self.assertEqual(executed, [], "approval-gated tools must never run in an unattended subagent")

        # Each attempt gets an immediate, clear error result — not an approval
        # prompt and not a hang. Two layered rejections are acceptable:
        # the loop guard names the unattended policy (execute_python, whose
        # gate is name-based), and the registry view rejects the filtered
        # requires_approval tool as unknown before any queue wait.
        for name, expected_fragment in (
            (constants.EXECUTE_PYTHON_TOOL_NAME, "unattended"),
            ("guarded_tool", "unknown tool"),
        ):
            results = [e for e in events if e.type == TurnEventType.TOOL_RESULT and e.tool_name == name]
            self.assertEqual(len(results), 1, f"expected one TOOL_RESULT for {name}, got {len(results)}")
            self.assertTrue(results[0].tool_is_error, f"{name} result must be an error")
            self.assertIn(expected_fragment, results[0].tool_result.lower())

        # The worker still completes: the final scripted text arrives.
        text_done = [e for e in events if e.type == TurnEventType.TEXT_DONE]
        self.assertTrue(any("Finished without the gated tools." in (e.text or "") for e in text_done))

    def test_unattended_child_loop_construction_filters_registry_and_schema(self):
        runner = SubagentRunner(
            provider=ScriptedProvider(),
            tool_registry=_interactive_registry([]),
            config=_config(),
            host_name="IDA Pro",
        )
        child = runner._build_loop(SessionState())

        self.assertTrue(child.unattended, "parentless runner must produce an unattended child loop")

        # Registry seam: interactive tools are gone at construction.
        self.assertIsNone(child.tools.get(constants.EXECUTE_PYTHON_TOOL_NAME))
        self.assertIsNone(child.tools.get("guarded_tool"))
        self.assertIsNotNone(child.tools.get("echo_tool"), "safe tools must survive the filter")

        # Schema seam: pseudo-tool gates are not advertised either.
        schema_names = {t.get("function", {}).get("name") for t in child._build_tools_schema(None, False)}
        self.assertNotIn(constants.EXECUTE_PYTHON_TOOL_NAME, schema_names)
        self.assertNotIn("guarded_tool", schema_names)
        self.assertNotIn("ask_user", schema_names)
        self.assertNotIn("delegate_external_task", schema_names)
        self.assertIn("spawn_subagent", schema_names)

    def test_attended_child_keeps_interactive_tools(self):
        """With a parent loop the gates stay available (someone can answer)."""
        parent = AgentLoop(
            provider=ScriptedProvider(),
            tool_registry=_interactive_registry([]),
            config=_config(),
            session=SessionState(),
        )
        runner = SubagentRunner(
            provider=ScriptedProvider(),
            tool_registry=_interactive_registry([]),
            config=_config(),
            host_name="IDA Pro",
            parent_loop=parent,
        )
        child = runner._build_loop(SessionState())

        self.assertFalse(child.unattended)
        self.assertIsNotNone(child.tools.get(constants.EXECUTE_PYTHON_TOOL_NAME))
        self.assertIsNotNone(child.tools.get("guarded_tool"))
        schema_names = {t.get("function", {}).get("name") for t in child._build_tools_schema(None, False)}
        self.assertIn("ask_user", schema_names)


class TestMutationPropagation(unittest.TestCase):
    """Child mutations must land in the parent's /undo log."""

    def test_subagent_rename_propagates_to_parent_mutation_log(self):
        parent = AgentLoop(
            provider=ScriptedProvider(),
            tool_registry=ToolRegistry(),
            config=_config(),
            session=SessionState(),
        )
        child_state = {"name": "sub_401000"}
        provider = ScriptedProvider(
            responses=[
                _tool_call_response(
                    "rename_function",
                    {"address": "0x401000", "new_name": "main"},
                    call_id="call_rename",
                ),
                _text_response("Renamed the function."),
            ]
        )
        runner = SubagentRunner(
            provider=provider,
            tool_registry=_rename_registry(child_state),
            config=_config(),
            host_name="IDA Pro",
            parent_loop=parent,
        )

        events = _run_with_deadline(runner.run_task("rename 0x401000 to main"))

        self.assertEqual(child_state["name"], "main", "child must have performed the rename")
        mutation_events = [e for e in events if e.type == TurnEventType.MUTATION_RECORDED]
        self.assertEqual(len(mutation_events), 1, "child run should record exactly one mutation")

        self.assertEqual(len(parent._mutation_log), 1, "parent /undo log must contain the child mutation")
        record = parent._mutation_log[0]
        self.assertIsInstance(record, MutationRecord)
        self.assertTrue(record.reversible)
        self.assertEqual(record.tool_name, "rename_function")
        self.assertEqual(record.reverse_tool, "rename_function")
        self.assertEqual(record.reverse_arguments["address"], "0x401000")
        self.assertEqual(record.reverse_arguments["new_name"], "sub_401000")

    def test_drain_and_record_mutations_roundtrip(self):
        loop = AgentLoop(
            provider=ScriptedProvider(),
            tool_registry=ToolRegistry(),
            config=_config(),
            session=SessionState(),
        )
        rec_a = MutationRecord(
            tool_name="rename_function",
            arguments={},
            reverse_tool="rename_function",
            reverse_arguments={},
            description="a",
        )
        rec_b = MutationRecord(
            tool_name="rename_function",
            arguments={},
            reverse_tool="rename_function",
            reverse_arguments={},
            description="b",
        )

        loop.record_mutations([rec_a])
        loop.record_mutations([rec_b])
        self.assertEqual(len(loop._mutation_log), 2)

        drained = loop.drain_mutations()
        self.assertEqual([r.description for r in drained], ["a", "b"])
        self.assertEqual(loop._mutation_log, [], "drain must clear the log")
        self.assertEqual(loop.drain_mutations(), [], "second drain returns nothing")

    def test_record_mutations_is_thread_safe(self):
        loop = AgentLoop(
            provider=ScriptedProvider(),
            tool_registry=ToolRegistry(),
            config=_config(),
            session=SessionState(),
        )
        threads: list[threading.Thread] = []
        for _ in range(8):
            records = [
                MutationRecord(
                    tool_name="rename_function",
                    arguments={},
                    reverse_tool="rename_function",
                    reverse_arguments={},
                    description=f"m{i}",
                )
                for i in range(25)
            ]
            threads.append(threading.Thread(target=loop.record_mutations, args=(records,)))
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        self.assertEqual(len(loop._mutation_log), 200, "all concurrent records must land exactly once")


if __name__ == "__main__":
    unittest.main()
