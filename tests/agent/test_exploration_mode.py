"""Tests for the exploration mode state machine and knowledge base."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.agent.exploration_mode import (
    ExplorationPhase,
    ExplorationState,
    Finding,
    FunctionInfo,
    KnowledgeBase,
    ModificationPlan,
    PatchRecord,
    PatchSummary,
    PlannedChange,
)


class TestKnowledgeBase(unittest.TestCase):
    """Tests for KnowledgeBase data accumulation and planning gates."""

    def test_empty_kb_not_ready_for_planning(self):
        kb = KnowledgeBase()
        self.assertFalse(kb.has_minimum_for_planning)

    def test_one_function_no_hypothesis_not_ready(self):
        kb = KnowledgeBase()
        kb.add_function(FunctionInfo(address=0x401000, name="main", summary="entry"))
        self.assertFalse(kb.has_minimum_for_planning)

    def test_one_hypothesis_no_function_not_ready(self):
        kb = KnowledgeBase()
        kb.add_finding(Finding(
            category="hypothesis", address=None,
            summary="Change constant", relevance="high",
        ))
        self.assertFalse(kb.has_minimum_for_planning)

    def test_low_relevance_hypothesis_not_sufficient(self):
        """Require at least one high-relevance hypothesis."""
        kb = KnowledgeBase()
        kb.add_function(FunctionInfo(address=0x401000, name="main", summary="entry"))
        kb.add_finding(Finding(
            category="hypothesis", address=None,
            summary="Maybe something", relevance="low",
        ))
        self.assertFalse(kb.has_minimum_for_planning)

    def test_high_relevance_hypothesis_sufficient(self):
        kb = KnowledgeBase()
        kb.add_function(FunctionInfo(address=0x401000, name="main", summary="entry"))
        kb.add_finding(Finding(
            category="hypothesis", address=None,
            summary="Change constant at 0x401248 from 3 to 6", relevance="high",
        ))
        self.assertTrue(kb.has_minimum_for_planning)

    def test_add_finding_auto_extracts_hypothesis(self):
        kb = KnowledgeBase()
        kb.add_finding(Finding(
            category="hypothesis", address=None,
            summary="Double the score", relevance="high",
        ))
        self.assertEqual(len(kb.hypotheses), 1)
        self.assertEqual(kb.hypotheses[0], "Double the score")

    def test_add_function_deduplicates_by_address(self):
        kb = KnowledgeBase()
        kb.add_function(FunctionInfo(address=0x401000, name="sub_401000", summary="first"))
        kb.add_function(FunctionInfo(address=0x401000, name="main", summary="updated"))
        self.assertEqual(len(kb.relevant_functions), 1)
        self.assertEqual(kb.relevant_functions[0x401000].name, "main")

    def test_planning_gap_description(self):
        kb = KnowledgeBase()
        gap = kb.planning_gap_description
        self.assertIn("0 relevant functions", gap)

        kb.add_function(FunctionInfo(address=0x401000, name="main", summary="entry"))
        gap = kb.planning_gap_description
        self.assertIn("0 hypotheses", gap)

        kb.add_finding(Finding(
            category="hypothesis", address=None,
            summary="low one", relevance="low",
        ))
        gap = kb.planning_gap_description
        self.assertIn("high-relevance", gap)

    def test_to_summary_includes_all_sections(self):
        kb = KnowledgeBase(user_goal="Double the score")
        kb.add_function(FunctionInfo(
            address=0x401000, name="score_handler",
            summary="Handles score updates", relevance="high",
        ))
        kb.add_finding(Finding(
            category="hypothesis", address=0x401248,
            summary="Change add [score], 10 to add [score], 20",
            relevance="high",
        ))
        summary = kb.to_summary()
        self.assertIn("Double the score", summary)
        self.assertIn("0x401000", summary)
        self.assertIn("score_handler", summary)
        self.assertIn("Hypotheses", summary)


class TestExplorationStateTransitions(unittest.TestCase):
    """Tests for ExplorationState.can_transition_to() validation."""

    def test_explore_to_plan_requires_minimum(self):
        state = ExplorationState()
        allowed, reason = state.can_transition_to(ExplorationPhase.PLAN)
        self.assertFalse(allowed)
        self.assertIn("Not enough findings", reason)

    def test_explore_to_plan_allowed_with_findings(self):
        state = ExplorationState()
        state.knowledge_base.add_function(
            FunctionInfo(address=0x401000, name="main", summary="entry")
        )
        state.knowledge_base.add_finding(Finding(
            category="hypothesis", address=None,
            summary="Test", relevance="high",
        ))
        allowed, _reason = state.can_transition_to(ExplorationPhase.PLAN)
        self.assertTrue(allowed)

    def test_cannot_skip_phases(self):
        state = ExplorationState()
        allowed, _ = state.can_transition_to(ExplorationPhase.EXECUTE)
        self.assertFalse(allowed)

        allowed, _ = state.can_transition_to(ExplorationPhase.SAVE)
        self.assertFalse(allowed)

    def test_plan_to_execute_requires_changes(self):
        state = ExplorationState()
        state.phase = ExplorationPhase.PLAN
        allowed, reason = state.can_transition_to(ExplorationPhase.EXECUTE)
        self.assertFalse(allowed)
        self.assertIn("No modification plan", reason)

    def test_plan_to_execute_allowed_with_plan(self):
        state = ExplorationState()
        state.phase = ExplorationPhase.PLAN
        state.modification_plan = ModificationPlan(
            changes=[PlannedChange(index=0, target_address=0x401000,
                                   current_behavior="jz", proposed_behavior="jnz",
                                   patch_strategy="change opcode")]
        )
        allowed, _ = state.can_transition_to(ExplorationPhase.EXECUTE)
        self.assertTrue(allowed)

    def test_execute_to_save_requires_patches(self):
        state = ExplorationState()
        state.phase = ExplorationPhase.EXECUTE
        allowed, reason = state.can_transition_to(ExplorationPhase.SAVE)
        self.assertFalse(allowed)
        self.assertIn("No patches", reason)

    def test_execute_to_save_allowed_with_patches(self):
        state = ExplorationState()
        state.phase = ExplorationPhase.EXECUTE
        state.patches_applied.append(PatchRecord(
            address=0x401000, original_bytes=b"\x74", new_bytes=b"\x75",
            description="JZ -> JNZ",
        ))
        allowed, _ = state.can_transition_to(ExplorationPhase.SAVE)
        self.assertTrue(allowed)

    def test_cannot_transition_to_same_phase(self):
        state = ExplorationState()
        allowed, reason = state.can_transition_to(ExplorationPhase.EXPLORE)
        self.assertFalse(allowed)
        self.assertIn("Already in", reason)

    def test_total_turns_counter(self):
        state = ExplorationState()
        self.assertEqual(state.total_turns, 0)
        state.total_turns += 1
        state.explore_turns += 1
        state.total_turns += 1
        state.explore_turns += 1
        self.assertEqual(state.total_turns, 2)
        self.assertEqual(state.explore_turns, 2)


class TestPatchSummary(unittest.TestCase):
    """Tests for PatchSummary.compute()."""

    def test_empty_summary(self):
        ps = PatchSummary()
        ps.compute()
        self.assertEqual(ps.total_bytes_modified, 0)
        self.assertFalse(ps.all_verified)

    def test_computes_totals(self):
        ps = PatchSummary(patches=[
            PatchRecord(address=0x1000, original_bytes=b"\x74", new_bytes=b"\x75",
                        verified=True),
            PatchRecord(address=0x2000, original_bytes=b"\x00\x00", new_bytes=b"\x01\x02",
                        verified=True),
        ])
        ps.compute()
        self.assertEqual(ps.total_bytes_modified, 3)  # 1 + 2
        self.assertTrue(ps.all_verified)

    def test_not_all_verified(self):
        ps = PatchSummary(patches=[
            PatchRecord(address=0x1000, original_bytes=b"\x74", new_bytes=b"\x75",
                        verified=True),
            PatchRecord(address=0x2000, original_bytes=b"\x00", new_bytes=b"\x01",
                        verified=False),
        ])
        ps.compute()
        self.assertFalse(ps.all_verified)


if __name__ == "__main__":
    unittest.main()
