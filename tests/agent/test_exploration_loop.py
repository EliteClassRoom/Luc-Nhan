"""End-to-end tests for exploration mode event sequence with mock provider."""

from __future__ import annotations

import json
import os
import sys
import unittest
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.agent.loop import AgentLoop
from rikugan.agent.turn import TurnEvent, TurnEventType
from rikugan.core.config import RikuganConfig
from rikugan.core.types import (
    Message,
    ModelInfo,
    ProviderCapabilities,
    Role,
    StreamChunk,
    TokenUsage,
)
from rikugan.providers.base import LLMProvider
from rikugan.state.session import SessionState
from rikugan.tools.base import ParameterSchema, ToolDefinition
from rikugan.tools.registry import ToolRegistry


class MockProvider(LLMProvider):
    """Mock LLM provider that returns scripted responses."""

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


def _tool_call_response(tool_name: str, args: dict[str, Any], call_id: str = "call_1") -> list[StreamChunk]:
    return [
        StreamChunk(is_tool_call_start=True, tool_call_id=call_id, tool_name=tool_name),
        StreamChunk(tool_args_delta=json.dumps(args), tool_call_id=call_id),
        StreamChunk(is_tool_call_end=True, tool_call_id=call_id, tool_name=tool_name),
        StreamChunk(usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)),
    ]


def _make_registry() -> ToolRegistry:
    """Create a minimal tool registry for tests."""
    registry = ToolRegistry()
    # Register a dummy read-only tool
    defn = ToolDefinition(
        name="decompile_function",
        description="Decompile a function",
        parameters=[ParameterSchema(name="name", type="string")],
        handler=lambda name="": f"int {name}(void) {{ return 0; }}",
    )
    registry.register(defn)
    return registry


class TestExplorationModeEvents(unittest.TestCase):
    """Verify exploration mode emits events in the correct order."""

    def _run_loop(self, loop: AgentLoop, message: str) -> list[TurnEvent]:
        """Consume the generator, collecting all events."""
        events = []
        for event in loop.run(message):
            events.append(event)
        return events

    def test_explore_only_emits_phase_change(self):
        """Explore-only mode should emit exploration_phase_change at start."""
        provider = MockProvider(
            [
                # Turn 1: agent calls exploration_report
                _tool_call_response(
                    "exploration_report",
                    {
                        "category": "function_purpose",
                        "summary": "main() is the entry point",
                        "address": 4198400,  # 0x401000
                        "function_name": "main",
                        "relevance": "high",
                    },
                ),
                # Turn 2: text-only response, agent is done
                _text_response("I found that main() is the entry point."),
            ]
        )

        loop = AgentLoop(
            provider=provider,
            tool_registry=_make_registry(),
            config=RikuganConfig(),
            session=SessionState(),
        )

        events = self._run_loop(loop, "/explore Find the entry point")

        # Should have exploration_phase_change event at start
        phase_events = [e for e in events if e.type == TurnEventType.EXPLORATION_PHASE_CHANGE]
        self.assertTrue(len(phase_events) >= 1)
        self.assertEqual(phase_events[0].metadata["to_phase"], "explore")

        # Should have exploration_finding event
        finding_events = [e for e in events if e.type == TurnEventType.EXPLORATION_FINDING]
        self.assertEqual(len(finding_events), 1)
        self.assertEqual(finding_events[0].metadata["category"], "function_purpose")

        # Should have turn_start and turn_end
        starts = [e for e in events if e.type == TurnEventType.TURN_START]
        ends = [e for e in events if e.type == TurnEventType.TURN_END]
        self.assertTrue(len(starts) >= 1)
        self.assertTrue(len(ends) >= 1)

    def test_explore_only_no_plan_phase(self):
        """Explore-only mode should NOT enter plan phase."""
        provider = MockProvider(
            [
                _text_response("Here's what I found about the binary."),
            ]
        )

        loop = AgentLoop(
            provider=provider,
            tool_registry=_make_registry(),
            config=RikuganConfig(),
            session=SessionState(),
        )

        events = self._run_loop(loop, "/explore Analyze this binary")

        # Should NOT have plan or execute phases
        phase_events = [e for e in events if e.type == TurnEventType.EXPLORATION_PHASE_CHANGE]
        to_phases = [e.metadata.get("to_phase") for e in phase_events]
        self.assertNotIn("plan", to_phases)
        self.assertNotIn("execute", to_phases)

    def test_knowledge_base_populated_from_findings(self):
        """exploration_report should populate the knowledge base."""
        provider = MockProvider(
            [
                _tool_call_response(
                    "exploration_report",
                    {
                        "category": "hypothesis",
                        "summary": "Change constant at 0x401248",
                        "relevance": "high",
                    },
                    "c1",
                ),
                _tool_call_response(
                    "exploration_report",
                    {
                        "category": "function_purpose",
                        "summary": "Score handler",
                        "address": 4198400,
                        "function_name": "score_handler",
                        "relevance": "high",
                    },
                    "c2",
                ),
                _text_response("Done exploring."),
            ]
        )

        loop = AgentLoop(
            provider=provider,
            tool_registry=_make_registry(),
            config=RikuganConfig(),
            session=SessionState(),
        )

        self._run_loop(loop, "/explore Find score functions")

        # Knowledge base should have findings
        kb = loop.last_knowledge_base
        self.assertIsNotNone(kb)
        self.assertTrue(len(kb.findings) >= 2)
        self.assertTrue(len(kb.hypotheses) >= 1)

    def test_phase_transition_denied_without_findings(self):
        """phase_transition to plan should be denied without sufficient findings."""
        provider = MockProvider(
            [
                _tool_call_response(
                    "phase_transition",
                    {
                        "to_phase": "plan",
                        "reason": "Ready to plan",
                    },
                ),
                _text_response("OK, I'll keep exploring."),
            ]
        )

        loop = AgentLoop(
            provider=provider,
            tool_registry=_make_registry(),
            config=RikuganConfig(),
            session=SessionState(),
        )

        events = self._run_loop(loop, "/explore Find something")

        # The phase transition should be denied
        tool_results = [e for e in events if e.type == TurnEventType.TOOL_RESULT]
        denied = any("Cannot transition" in (e.tool_result or "") for e in tool_results)
        self.assertTrue(denied)


class TestMutationTracking(unittest.TestCase):
    """Verify mutation log is populated on mutating tool calls."""

    @staticmethod
    def _register_rename_tools(registry: ToolRegistry, old_name: str = "sub_401000") -> None:
        """Register rename_function + get_function_name helpers for mutation tests.

        Uses a mutable dict to track the current name so that post-state
        verification in AgentLoop._verify_mutation() succeeds.
        """
        state = {"name": old_name}

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

    @staticmethod
    def _register_set_comment_tools(
        registry: ToolRegistry,
        comment_tracker: dict[str, str],
        *,
        update_visible_state: bool = True,
    ) -> None:
        """Register set_comment + get_comment helpers for comment mutation tests.

        *comment_tracker* is a mutable dict that stores the current comment
        value returned by get_comment.

        When *update_visible_state* is True (the default), set_comment writes
        to the same ``"comment"`` key that get_comment reads, so post-state
        verification matches.  When False, set_comment writes to a different
        key (``"_stored"``) so get_comment deliberately returns a mismatched
        value, useful for whitespace-mismatch tests.
        """
        _store_key = "comment" if update_visible_state else "_stored"

        registry.register(
            ToolDefinition(
                name="set_comment",
                description="Set a comment at an address",
                parameters=[
                    ParameterSchema(name="address", type="string"),
                    ParameterSchema(name="comment", type="string"),
                    ParameterSchema(name="repeatable", type="boolean"),
                ],
                mutating=True,
                handler=lambda address="", comment="", repeatable=False, t=comment_tracker, k=_store_key: (
                    t.__setitem__(k, comment) or "Comment set."
                ),
            )
        )
        registry.register(
            ToolDefinition(
                name="get_comment",
                description="Get the comment at an address",
                parameters=[
                    ParameterSchema(name="address", type="string"),
                    ParameterSchema(name="repeatable", type="boolean"),
                ],
                mutating=False,
                handler=lambda address="", repeatable=False, t=comment_tracker: t.get("comment", ""),
            )
        )

    @staticmethod
    def _register_pseudocode_comment_tools(
        registry: ToolRegistry,
        getter_state_results: list[str],
    ) -> None:
        """Register set_pseudocode_comment + get_pseudocode_comment_state tools.

        *getter_state_results* is a list of JSON strings returned sequentially
        by get_pseudocode_comment_state — first for pre-state capture, then
        for post-state verification.
        """
        results_iter = iter(getter_state_results)
        _getter_calls: list[str] = []

        registry.register(
            ToolDefinition(
                name="set_pseudocode_comment",
                description="Set a pseudocode comment",
                parameters=[
                    ParameterSchema(name="func_address", type="string"),
                    ParameterSchema(name="target_address", type="string"),
                    ParameterSchema(name="comment", type="string"),
                ],
                mutating=True,
                handler=lambda func_address="", target_address="", comment="": "Pseudocode comment set.",
            )
        )

        def _nested_getter(
            func_address: str = "",
            target_address: str = "",
        ) -> str:
            try:
                val = next(results_iter)
            except StopIteration:
                raise AssertionError("get_pseudocode_comment_state called more times than expected") from None
            _getter_calls.append(val)
            return val

        registry.register(
            ToolDefinition(
                name="get_pseudocode_comment_state",
                description="Get pseudocode comment state",
                parameters=[
                    ParameterSchema(name="func_address", type="string"),
                    ParameterSchema(name="target_address", type="string"),
                ],
                mutating=False,
                handler=_nested_getter,
            )
        )

    def _assert_no_mutation_event_or_log_entry(self, loop: AgentLoop, events: list[TurnEvent]) -> None:
        """Assert that no mutation event was emitted and mutation log is empty."""
        mutation_events = [e for e in events if e.type == TurnEventType.MUTATION_RECORDED]
        self.assertEqual(len(mutation_events), 0, "unexpected MUTATION_RECORDED event")
        self.assertEqual(loop._mutation_log, [], "mutation log should be empty")

    def _assert_tool_result_without_error(self, events: list[TurnEvent], tool_name: str) -> None:
        """Assert exactly one TOOL_RESULT event for *tool_name* exists, it is
        not an error, and no ERROR event was emitted."""
        tool_results = [e for e in events if e.type == TurnEventType.TOOL_RESULT and e.tool_name == tool_name]
        self.assertEqual(
            len(tool_results),
            1,
            f"expected exactly one TOOL_RESULT for {tool_name}",
        )
        self.assertFalse(
            tool_results[0].tool_is_error,
            f"TOOL_RESULT for {tool_name} must not be an error",
        )
        self.assertEqual(
            [e for e in events if e.type == TurnEventType.ERROR],
            [],
            "no ERROR event must be emitted",
        )

    def test_rename_function_recorded(self):
        """rename_function should be recorded in mutation log."""
        provider = MockProvider(
            [
                _tool_call_response(
                    "rename_function",
                    {
                        "address": "0x401000",
                        "new_name": "main",
                    },
                ),
                _text_response("Renamed the function."),
            ]
        )

        registry = ToolRegistry()
        self._register_rename_tools(registry)

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=RikuganConfig(),
            session=SessionState(),
        )

        list(loop.run("Rename function at 0x401000 to main"))

        self.assertEqual(len(loop._mutation_log), 1)
        rec = loop._mutation_log[0]
        self.assertTrue(rec.reversible)
        self.assertEqual(rec.reverse_tool, "rename_function")
        self.assertEqual(rec.reverse_arguments["address"], "0x401000")
        self.assertEqual(rec.reverse_arguments["new_name"], "sub_401000")

    def test_mutation_emits_event(self):
        """Mutating tool should emit MUTATION_RECORDED event."""
        provider = MockProvider(
            [
                _tool_call_response(
                    "rename_function",
                    {
                        "address": "0x401000",
                        "new_name": "main",
                    },
                ),
                _text_response("Done."),
            ]
        )

        registry = ToolRegistry()
        self._register_rename_tools(registry)

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=RikuganConfig(),
            session=SessionState(),
        )

        events = list(loop.run("Rename sub_401000 to main"))

        mutation_events = [e for e in events if e.type == TurnEventType.MUTATION_RECORDED]
        self.assertEqual(len(mutation_events), 1)
        self.assertEqual(mutation_events[0].tool_name, "rename_function")
        self.assertTrue(mutation_events[0].metadata["reversible"])
        self.assertEqual(mutation_events[0].metadata["reverse_tool"], "rename_function")

    def test_failed_or_missing_getter_no_reversible_record(self):
        """Failed tool or missing verification getter must NOT emit a reversible record.

        Strengthened to cover both:
        - A mutating tool whose result indicates failure.
        - A mutating tool whose post-state verification getter is not registered.
        In both cases the mutation log must remain empty.
        """
        # --- Case 1: tool returns a failure result string ---
        registry = ToolRegistry()
        state = {"name": "sub_401000"}
        registry.register(
            ToolDefinition(
                name="rename_function",
                description="Rename a function",
                parameters=[
                    ParameterSchema(name="address", type="string"),
                    ParameterSchema(name="new_name", type="string"),
                ],
                mutating=True,
                handler=lambda address="", new_name="", s=state: "Failed to set name: segment not writable",
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

        provider = MockProvider(
            [
                _tool_call_response(
                    "rename_function",
                    {"address": "0x401000", "new_name": "main"},
                ),
                _text_response("Done."),
            ]
        )

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=RikuganConfig(),
            session=SessionState(),
        )

        events = list(loop.run("Rename sub_401000 to main"))

        self._assert_tool_result_without_error(events, "rename_function")
        mutation_events = [e for e in events if e.type == TurnEventType.MUTATION_RECORDED]
        self.assertEqual(len(mutation_events), 0, "failed tool should not produce mutation events")
        self.assertEqual(loop._mutation_log, [], "failed tool should not pollute mutation log")

        # --- Case 2: tool succeeds but getter is missing (verification fails) ---
        registry2 = ToolRegistry()
        state2 = {"name": "sub_401000"}
        registry2.register(
            ToolDefinition(
                name="rename_function",
                description="Rename a function",
                parameters=[
                    ParameterSchema(name="address", type="string"),
                    ParameterSchema(name="new_name", type="string"),
                ],
                mutating=True,
                handler=lambda address="", new_name="", s=state2: (
                    s.update(name=new_name) or f"Renamed {address} to {new_name}"
                ),
            )
        )
        # get_function_name is intentionally NOT registered

        provider2 = MockProvider(
            [
                _tool_call_response(
                    "rename_function",
                    {"address": "0x401000", "new_name": "main"},
                ),
                _text_response("Done."),
            ]
        )

        loop2 = AgentLoop(
            provider=provider2,
            tool_registry=registry2,
            config=RikuganConfig(),
            session=SessionState(),
        )

        events2 = list(loop2.run("Rename sub_401000 to main"))

        self._assert_tool_result_without_error(events2, "rename_function")
        mutation_events2 = [e for e in events2 if e.type == TurnEventType.MUTATION_RECORDED]
        self.assertEqual(len(mutation_events2), 0, "missing getter should not produce mutation events")
        self.assertEqual(loop2._mutation_log, [], "missing getter should not pollute mutation log")

    def test_pseudocode_post_state_detects_failures(self):
        """Malformed/non-dict/ok=false/non-string pseudocode post-state must not
        create reversible undo records.

        Each subcase uses a different getter result for pre-state capture (a
        valid reversible JSON) and post-state verification (the bad value).
        The mutating tool must return a TOOL_RESULT, no ERROR event, no
        MUTATION_RECORDED event, and _mutation_log must be empty.

        Covers:
        - getter returns malformed JSON.
        - getter returns a JSON list (not a dict).
        - getter returns ok=false.
        - getter returns ok=true with non-string comment (int).
        """
        for post_state, label in (
            ("not json {{{", "malformed JSON"),
            ('["not", "a", "dict"]', "non-dict JSON list"),
            ('{"ok": false, "comment": ""}', "ok=false"),
            ('{"ok": true, "comment": 42}', "non-string comment"),
        ):
            with self.subTest(case=label):
                registry = ToolRegistry()
                # First call (pre-state capture) returns valid JSON; second
                # call (post-state verification) returns the bad value.
                self._register_pseudocode_comment_tools(
                    registry,
                    ['{"ok": true, "comment": "old"}', post_state],
                )

                provider = MockProvider(
                    [
                        _tool_call_response(
                            "set_pseudocode_comment",
                            {
                                "func_address": "0x401000",
                                "target_address": "0x401010",
                                "comment": "test",
                            },
                        ),
                        _text_response("Done."),
                    ]
                )

                loop = AgentLoop(
                    provider=provider,
                    tool_registry=registry,
                    config=RikuganConfig(),
                    session=SessionState(),
                )
                events = list(loop.run("Set pseudocode comment"))

                # The mutating tool must have produced a TOOL_RESULT.
                tool_results = [e for e in events if e.type == TurnEventType.TOOL_RESULT]
                self.assertTrue(
                    any(e.tool_name == "set_pseudocode_comment" for e in tool_results),
                    "set_pseudocode_comment must produce a TOOL_RESULT",
                )

                # No ERROR event must have been emitted.
                error_events = [e for e in events if e.type == TurnEventType.ERROR]
                self.assertEqual(len(error_events), 0, "no ERROR event must be emitted")

                # No MUTATION_RECORDED event and empty mutation log.
                self._assert_no_mutation_event_or_log_entry(loop, events)

    def test_non_string_pseudocode_comment_matches_coerces_to_fail(self):
        """When get_pseudocode_comment_state returns non-string comment=42
        and the requested comment is '42', post-state verification must fail
        (exact string comparison, not str()-coerced).
        """
        registry = ToolRegistry()
        # Pre-state capture gets valid JSON; post-state gets int 42.
        self._register_pseudocode_comment_tools(
            registry,
            ['{"ok": true, "comment": "old"}', '{"ok": true, "comment": 42}'],
        )
        # The LLM sends comment="42" (a string), but getter returns int 42.
        # Verification must reject non-string actual.

        provider = MockProvider(
            [
                _tool_call_response(
                    "set_pseudocode_comment",
                    {
                        "func_address": "0x401000",
                        "target_address": "0x401010",
                        "comment": "42",
                    },
                ),
                _text_response("Done."),
            ]
        )

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=RikuganConfig(),
            session=SessionState(),
        )
        events = list(loop.run("Set pseudocode comment"))
        self._assert_tool_result_without_error(events, "set_pseudocode_comment")
        self._assert_no_mutation_event_or_log_entry(loop, events)

    def test_comment_whitespace_mismatch_no_reversible_record(self):
        """set_comment with ' hello ' succeeds but getter returns 'hello'
        (no spaces). Post-state verification must detect the mismatch and
        not create an undo record.
        """
        comment_tracker: dict[str, str] = {"comment": "hello"}
        registry = ToolRegistry()
        # update_visible_state=False means set_comment writes to "_stored"
        # while get_comment reads from "comment", so get_comment
        # deliberately returns "hello" while set_comment receives " hello ".
        self._register_set_comment_tools(registry, comment_tracker, update_visible_state=False)

        provider = MockProvider(
            [
                _tool_call_response(
                    "set_comment",
                    {
                        "address": "0x401000",
                        "comment": " hello ",
                        "repeatable": False,
                    },
                ),
                _text_response("Done."),
            ]
        )

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=RikuganConfig(),
            session=SessionState(),
        )
        events = list(loop.run("Set comment"))
        self._assert_tool_result_without_error(events, "set_comment")
        self._assert_no_mutation_event_or_log_entry(loop, events)

    def test_comment_whitespace_exact_match_is_reversible(self):
        """set_comment with ' hello ' succeeds and getter returns ' hello '.
        Post-state verification must match exactly and create a reversible record.
        The reverse_arguments must reference the old comment, not the new one.
        """
        comment_tracker: dict[str, str] = {"comment": "old comment"}
        registry = ToolRegistry()
        self._register_set_comment_tools(registry, comment_tracker, update_visible_state=True)

        provider = MockProvider(
            [
                _tool_call_response(
                    "set_comment",
                    {
                        "address": "0x401000",
                        "comment": " hello ",
                        "repeatable": False,
                    },
                ),
                _text_response("Done."),
            ]
        )

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=RikuganConfig(),
            session=SessionState(),
        )
        events = list(loop.run("Set comment"))
        mutation_events = [e for e in events if e.type == TurnEventType.MUTATION_RECORDED]
        self.assertEqual(len(mutation_events), 1, "exactly one MUTATION_RECORDED event")
        self.assertTrue(mutation_events[0].metadata["reversible"])
        self.assertEqual(mutation_events[0].metadata["reverse_tool"], "set_comment")
        self.assertEqual(mutation_events[0].metadata["reverse_args"]["comment"], "old comment")
        self.assertIs(mutation_events[0].metadata["reverse_args"]["repeatable"], False)
        self.assertEqual(len(loop._mutation_log), 1, "exactly one mutation log entry")
        self.assertEqual(
            loop._mutation_log[0].reverse_arguments["comment"],
            "old comment",
            "reverse_arguments must restore the old comment",
        )

    def test_non_high_confidence_mutation_failure_no_undo(self):
        """Non-high-confidence mutating tools that return clear failure strings
        (e.g. 'Variable ... not found') must not create undo records.
        """
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="rename_variable",
                description="Rename a local variable",
                parameters=[
                    ParameterSchema(name="func_address", type="string"),
                    ParameterSchema(name="old_name", type="string"),
                    ParameterSchema(name="new_name", type="string"),
                ],
                mutating=True,
                handler=lambda func_address="", old_name="", new_name="": (
                    "Variable 'old' not found in function at 0x401000"
                ),
            )
        )

        provider = MockProvider(
            [
                _tool_call_response(
                    "rename_variable",
                    {
                        "func_address": "0x401000",
                        "old_name": "old",
                        "new_name": "new",
                    },
                ),
                _text_response("Done."),
            ]
        )

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=RikuganConfig(),
            session=SessionState(),
        )
        events = list(loop.run("Rename variable"))
        self._assert_tool_result_without_error(events, "rename_variable")
        self._assert_no_mutation_event_or_log_entry(loop, events)

    def test_non_reversible_does_not_pollute_mutation_log(self):
        """Successful but non-reversible mutations must emit a UI-only event
        with reversible=False but must NOT be appended to _mutation_log.
        """
        registry = ToolRegistry()
        # A tool that has no reverse builder in mutation.py — build_reverse_record
        # returns a non-reversible record.
        registry.register(
            ToolDefinition(
                name="custom_mutation",
                description="A custom mutating tool with no reverse builder",
                parameters=[ParameterSchema(name="value", type="string")],
                mutating=True,
                handler=lambda value="": "OK",
            )
        )

        provider = MockProvider(
            [
                _tool_call_response(
                    "custom_mutation",
                    {"value": "test"},
                ),
                _text_response("Done."),
            ]
        )

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=RikuganConfig(),
            session=SessionState(),
        )
        events = list(loop.run("Run custom mutation"))

        # The mutation log must be empty — non-reversible records must not
        # consume /undo stack slots.
        self.assertEqual(loop._mutation_log, [], "non-reversible record should not pollute mutation log")

        # The implementation emits a UI-only MUTATION_RECORDED event for
        # successful non-reversible mutations.  Assert exactly one exists
        # with the expected metadata.
        mutation_events = [e for e in events if e.type == TurnEventType.MUTATION_RECORDED]
        self.assertEqual(
            len(mutation_events),
            1,
            "exactly one MUTATION_RECORDED event for non-reversible mutation",
        )
        self.assertFalse(mutation_events[0].metadata["reversible"])
        self.assertEqual(mutation_events[0].metadata["reverse_tool"], "")
        self.assertEqual(mutation_events[0].metadata["reverse_args"], {})


class TestSpawnSubagentPseudoTool(unittest.TestCase):
    """Verify spawn_subagent pseudo-tool works."""

    def test_subagent_returns_summary(self):
        """spawn_subagent should return text from the subagent."""
        # The subagent will get its own MockProvider, but we're testing the
        # pseudo-tool handler which creates a SubagentRunner.
        # For this test we just verify the tool is recognized and handled.
        provider = MockProvider(
            [
                _tool_call_response(
                    "spawn_subagent",
                    {
                        "task": "Analyze the main function",
                        "max_turns": 5,
                    },
                ),
                _text_response("The subagent found the main function."),
            ]
        )

        loop = AgentLoop(
            provider=provider,
            tool_registry=_make_registry(),
            config=RikuganConfig(),
            session=SessionState(),
        )

        events = list(loop.run("Use a subagent to analyze main"))

        # Should have tool_result event for spawn_subagent
        tool_results = [e for e in events if e.type == TurnEventType.TOOL_RESULT]
        subagent_results = [e for e in tool_results if e.tool_name == "spawn_subagent"]
        self.assertTrue(len(subagent_results) >= 1)


if __name__ == "__main__":
    unittest.main()
