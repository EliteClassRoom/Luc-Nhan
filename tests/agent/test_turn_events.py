"""Tests for TurnEvent factory methods and new event types."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from lucnhan.agent.turn import TurnEvent, TurnEventType


class TestTurnEventFactories(unittest.TestCase):
    """Verify all event factory methods produce correct type and metadata."""

    def test_text_delta(self):
        e = TurnEvent.text_delta("hello")
        self.assertEqual(e.type, TurnEventType.TEXT_DELTA)
        self.assertEqual(e.text, "hello")

    def test_text_done(self):
        e = TurnEvent.text_done("full text")
        self.assertEqual(e.type, TurnEventType.TEXT_DONE)
        self.assertEqual(e.text, "full text")

    def test_tool_call_start(self):
        e = TurnEvent.tool_call_start("id1", "decompile_function")
        self.assertEqual(e.type, TurnEventType.TOOL_CALL_START)
        self.assertEqual(e.tool_call_id, "id1")
        self.assertEqual(e.tool_name, "decompile_function")

    def test_turn_start_end(self):
        s = TurnEvent.turn_start(5)
        self.assertEqual(s.type, TurnEventType.TURN_START)
        self.assertEqual(s.turn_number, 5)

        e = TurnEvent.turn_end(5)
        self.assertEqual(e.type, TurnEventType.TURN_END)
        self.assertEqual(e.turn_number, 5)

    def test_error_event(self):
        e = TurnEvent.error_event("something broke")
        self.assertEqual(e.type, TurnEventType.ERROR)
        self.assertEqual(e.error, "something broke")

    def test_cancelled_event(self):
        e = TurnEvent.cancelled_event()
        self.assertEqual(e.type, TurnEventType.CANCELLED)

    def test_user_question(self):
        e = TurnEvent.user_question("Pick one?", ["A", "B"], "tc_1")
        self.assertEqual(e.type, TurnEventType.USER_QUESTION)
        self.assertEqual(e.text, "Pick one?")
        self.assertEqual(e.tool_call_id, "tc_1")
        self.assertEqual(e.metadata["options"], ["A", "B"])

    def test_plan_generated(self):
        e = TurnEvent.plan_generated(["step1", "step2"])
        self.assertEqual(e.type, TurnEventType.PLAN_GENERATED)
        self.assertEqual(e.plan_steps, ["step1", "step2"])

    def test_plan_step_start_done(self):
        s = TurnEvent.plan_step_start(0, "Do something")
        self.assertEqual(s.type, TurnEventType.PLAN_STEP_START)
        self.assertEqual(s.plan_step_index, 0)
        self.assertEqual(s.text, "Do something")

        d = TurnEvent.plan_step_done(0, "completed")
        self.assertEqual(d.type, TurnEventType.PLAN_STEP_DONE)

    def test_tool_approval_request(self):
        e = TurnEvent.tool_approval_request("tc1", "execute_python", "{}", "Run code")
        self.assertEqual(e.type, TurnEventType.TOOL_APPROVAL_REQUEST)
        self.assertEqual(e.tool_name, "execute_python")
        self.assertEqual(e.text, "Run code")

    def test_exploration_phase_change(self):
        e = TurnEvent.exploration_phase_change("explore", "plan", "Ready")
        self.assertEqual(e.type, TurnEventType.EXPLORATION_PHASE_CHANGE)
        self.assertEqual(e.metadata["from_phase"], "explore")
        self.assertEqual(e.metadata["to_phase"], "plan")
        self.assertEqual(e.text, "Ready")

    def test_exploration_finding(self):
        e = TurnEvent.exploration_finding(
            "function_purpose",
            "Main entry point",
            address=0x401000,
            relevance="high",
        )
        self.assertEqual(e.type, TurnEventType.EXPLORATION_FINDING)
        self.assertEqual(e.text, "Main entry point")
        self.assertEqual(e.metadata["category"], "function_purpose")
        self.assertEqual(e.metadata["address"], "0x401000")
        self.assertEqual(e.metadata["relevance"], "high")

    def test_exploration_finding_no_address(self):
        e = TurnEvent.exploration_finding("hypothesis", "Something", relevance="medium")
        self.assertIsNone(e.metadata["address"])

    def test_patch_applied(self):
        e = TurnEvent.patch_applied(0x401248, "JZ -> JNZ", "74 05", "75 05")
        self.assertEqual(e.type, TurnEventType.PATCH_APPLIED)
        self.assertEqual(e.metadata["address"], "0x401248")
        self.assertEqual(e.metadata["original"], "74 05")
        self.assertEqual(e.metadata["new"], "75 05")

    def test_patch_verified(self):
        e = TurnEvent.patch_verified(0x401248, True, "Decompilation confirmed")
        self.assertEqual(e.type, TurnEventType.PATCH_VERIFIED)
        self.assertTrue(e.metadata["success"])

    def test_save_approval_request(self):
        e = TurnEvent.save_approval_request(
            patch_count=3,
            total_bytes=12,
            all_verified=True,
            patches_detail=[{"address": "0x1000", "description": "test"}],
        )
        self.assertEqual(e.type, TurnEventType.SAVE_APPROVAL_REQUEST)
        self.assertEqual(e.metadata["patch_count"], 3)
        self.assertEqual(e.metadata["total_bytes"], 12)
        self.assertTrue(e.metadata["all_verified"])
        self.assertEqual(len(e.metadata["patches"]), 1)

    def test_save_completed(self):
        e = TurnEvent.save_completed(3, 12)
        self.assertEqual(e.type, TurnEventType.SAVE_COMPLETED)
        self.assertIn("3 patches", e.text)

    def test_save_discarded_with_rollback(self):
        e = TurnEvent.save_discarded(2, rolled_back=True)
        self.assertEqual(e.type, TurnEventType.SAVE_DISCARDED)
        self.assertIn("restored", e.text)
        self.assertTrue(e.metadata["rolled_back"])

    def test_save_discarded_without_rollback(self):
        e = TurnEvent.save_discarded(2, rolled_back=False)
        self.assertIn("persist", e.text)
        self.assertFalse(e.metadata["rolled_back"])

    def test_mutation_recorded(self):
        e = TurnEvent.mutation_recorded(
            tool_name="rename_function",
            description="Renamed sub_401000 → main",
            reversible=True,
            reverse_tool="rename_function",
            reverse_args={"old_name": "main", "new_name": "sub_401000"},
        )
        self.assertEqual(e.type, TurnEventType.MUTATION_RECORDED)
        self.assertEqual(e.tool_name, "rename_function")
        self.assertEqual(e.text, "Renamed sub_401000 → main")
        self.assertTrue(e.metadata["reversible"])
        self.assertEqual(e.metadata["reverse_tool"], "rename_function")
        self.assertEqual(e.metadata["reverse_args"]["old_name"], "main")

    def test_mutation_recorded_not_reversible(self):
        e = TurnEvent.mutation_recorded(
            tool_name="execute_python",
            description="Ran custom code",
            reversible=False,
        )
        self.assertEqual(e.type, TurnEventType.MUTATION_RECORDED)
        self.assertFalse(e.metadata["reversible"])
        self.assertEqual(e.metadata["reverse_tool"], "")
        self.assertEqual(e.metadata["reverse_args"], {})


class TestTurnEventTypeEnum(unittest.TestCase):
    """Verify all expected event types exist."""

    def test_all_event_types_present(self):
        expected = {
            "text_delta",
            "text_done",
            "tool_call_start",
            "tool_call_args_delta",
            "tool_call_done",
            "tool_result",
            "turn_start",
            "turn_end",
            "error",
            "cancelled",
            "usage_update",
            "user_question",
            "plan_generated",
            "plan_step_start",
            "plan_step_done",
            "tool_approval_request",
            "exploration_phase_change",
            "exploration_finding",
            "patch_applied",
            "patch_verified",
            "save_approval_request",
            "save_completed",
            "save_discarded",
            "mutation_recorded",
        }
        actual = {e.value for e in TurnEventType}
        self.assertTrue(expected.issubset(actual), f"Missing: {expected - actual}")


class TestReasoningRecoveryEvents(unittest.TestCase):
    """GLM reasoning resilience events: reasoning channel, recovery boundary,
    and discarded (partial) tool calls."""

    def test_reasoning_event_serializes_distinct_payload(self):
        e = TurnEvent.reasoning_event("private chain")
        self.assertEqual(e.type, TurnEventType.REASONING_DELTA)
        self.assertEqual(e.reasoning, "private chain")
        self.assertEqual(e.text, "")
        self.assertEqual(e.to_dict()["reasoning"], "private chain")

    def test_recovery_start_serializes_boundary_metadata(self):
        e = TurnEvent.recovery_start(
            attempt=2,
            reason="reasoning_degenerated",
            discard_transient_reasoning=True,
        )
        self.assertEqual(e.type, TurnEventType.RECOVERY_START)
        self.assertEqual(
            e.metadata,
            {
                "attempt": 2,
                "reason": "reasoning_degenerated",
                "discard_transient_reasoning": True,
            },
        )

    def test_tool_call_discarded_closes_started_call(self):
        e = TurnEvent.tool_call_discarded("call_1", "read_bytes", "truncated_arguments")
        self.assertEqual(e.type, TurnEventType.TOOL_CALL_DISCARDED)
        self.assertEqual(e.tool_call_id, "call_1")
        self.assertEqual(e.tool_name, "read_bytes")
        self.assertEqual(e.metadata["reason"], "truncated_arguments")


class TestDocsGateStatusEvent(unittest.TestCase):
    def test_factory_sets_metadata_and_tool_call_id(self):
        ev = TurnEvent.docs_gate_status(
            tool_call_id="abc123",
            state="running",
            reasons=("2 IDA modules", "14 non-comment lines"),
        )
        self.assertEqual(ev.type, TurnEventType.DOCS_GATE_STATUS)
        self.assertEqual(ev.tool_call_id, "abc123")
        self.assertEqual(ev.metadata["docs_gate_state"], "running")
        self.assertEqual(
            ev.metadata["docs_gate_reasons"],
            ["2 IDA modules", "14 non-comment lines"],
        )
        self.assertEqual(ev.metadata["docs_gate_summary"], "")

    def test_factory_defaults_empty_reasons_and_summary(self):
        ev = TurnEvent.docs_gate_status(tool_call_id="x", state="approved")
        self.assertEqual(ev.metadata["docs_gate_reasons"], [])
        self.assertEqual(ev.metadata["docs_gate_summary"], "")

    def test_factory_blocked_with_summary(self):
        ev = TurnEvent.docs_gate_status(
            tool_call_id="x",
            state="blocked",
            summary="ida_bytes.patch_qword does not exist",
        )
        self.assertEqual(ev.metadata["docs_gate_state"], "blocked")
        self.assertEqual(
            ev.metadata["docs_gate_summary"],
            "ida_bytes.patch_qword does not exist",
        )


if __name__ == "__main__":
    unittest.main()
