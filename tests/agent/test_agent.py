"""Tests for agent loop and turn events."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.agent.context_window import ContextWindowManager
from rikugan.agent.plan_mode import create_plan_from_text, parse_plan
from rikugan.agent.turn import TurnEvent, TurnEventType
from rikugan.core.config import RikuganConfig
from rikugan.core.types import Message, Role, TokenUsage
from rikugan.state.session import SessionState


class TestTurnEvents(unittest.TestCase):
    def test_text_delta(self):
        ev = TurnEvent.text_delta("hello")
        self.assertEqual(ev.type, TurnEventType.TEXT_DELTA)
        self.assertEqual(ev.text, "hello")

    def test_tool_call_start(self):
        ev = TurnEvent.tool_call_start("call_1", "test_tool")
        self.assertEqual(ev.type, TurnEventType.TOOL_CALL_START)
        self.assertEqual(ev.tool_name, "test_tool")

    def test_error_event(self):
        ev = TurnEvent.error_event("something broke")
        self.assertEqual(ev.type, TurnEventType.ERROR)
        self.assertEqual(ev.error, "something broke")


class TestPlanMode(unittest.TestCase):
    def test_parse_plan(self):
        text = """Here's my plan:

1. Analyze the function at 0x1000
2. Identify the struct fields
3. Create the struct definition
4. Apply the struct type

END_PLAN"""
        steps = parse_plan(text)
        self.assertEqual(len(steps), 4)
        self.assertEqual(steps[0], "Analyze the function at 0x1000")

    def test_create_plan(self):
        text = "1. Step one\n2. Step two\n3. Step three\nEND_PLAN"
        plan = create_plan_from_text(text)
        self.assertEqual(len(plan.steps), 3)
        self.assertFalse(plan.approved)
        self.assertEqual(plan.current_step, 0)
        self.assertFalse(plan.is_complete)

    def test_plan_advance(self):
        text = "1. Step one\n2. Step two\nEND_PLAN"
        plan = create_plan_from_text(text)
        plan.advance()
        self.assertEqual(plan.current_step, 1)
        plan.advance()
        self.assertTrue(plan.is_complete)


class TestContextWindow(unittest.TestCase):
    def test_usage_tracking(self):
        cw = ContextWindowManager(max_tokens=1000)
        cw.update_usage(TokenUsage(total_tokens=500))
        self.assertAlmostEqual(cw.usage_ratio, 0.5)
        self.assertFalse(cw.should_compact())

    def test_near_limit(self):
        cw = ContextWindowManager(max_tokens=1000, compaction_threshold=0.8)
        cw.update_usage(TokenUsage(total_tokens=850))
        self.assertTrue(cw.should_compact())

    def test_compact_messages(self):
        cw = ContextWindowManager(max_tokens=1000)
        messages = [
            Message(role=Role.SYSTEM, content="System"),
            Message(role=Role.USER, content="Q1"),
            Message(role=Role.ASSISTANT, content="A1"),
            Message(role=Role.USER, content="Q2"),
            Message(role=Role.ASSISTANT, content="A2"),
            Message(role=Role.USER, content="Q3"),
            Message(role=Role.ASSISTANT, content="A3"),
            Message(role=Role.USER, content="Q4"),
            Message(role=Role.ASSISTANT, content="A4"),
        ]
        compacted = cw.compact_messages(messages)
        self.assertLess(len(compacted), len(messages))
        # First message preserved
        self.assertEqual(compacted[0].role, Role.SYSTEM)
        # Last messages preserved
        self.assertEqual(compacted[-1].content, "A4")


class TestSessionState(unittest.TestCase):
    def test_add_message(self):
        session = SessionState()
        msg = Message(
            role=Role.ASSISTANT,
            content="test",
            token_usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )
        session.add_message(msg)
        self.assertEqual(session.message_count(), 1)
        self.assertEqual(session.total_usage.total_tokens, 15)

    def test_clear(self):
        session = SessionState()
        session.add_message(Message(role=Role.USER, content="hi"))
        session.clear()
        self.assertEqual(session.message_count(), 0)


class TestSessionHistory(unittest.TestCase):
    def test_save_and_load(self):
        import tempfile

        from rikugan.state.history import SessionHistory

        cfg = RikuganConfig()
        cfg._config_dir = tempfile.mkdtemp()

        history = SessionHistory(cfg)
        session = SessionState(provider_name="test", model_name="test-model")
        session.add_message(Message(role=Role.USER, content="hello"))

        path = history.save_session(session, description="test checkpoint")
        self.assertTrue(len(path) > 0)

        loaded = history.load_session(session.id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.message_count(), 1)
        self.assertEqual(loaded.messages[0].content, "hello")

    def test_list_sessions(self):
        import tempfile

        from rikugan.state.history import SessionHistory

        cfg = RikuganConfig()
        cfg._config_dir = tempfile.mkdtemp()
        history = SessionHistory(cfg)

        session1 = SessionState()
        session2 = SessionState()
        # Spec §7.3 — an empty draft is not history; both sessions must have
        # at least one message to survive the zero-message exclusion that
        # Task 3 of the on-demand history feature added.
        session1.add_message(Message(role=Role.USER, content="one"))
        session2.add_message(Message(role=Role.USER, content="two"))
        history.save_session(session1, description="s1")
        history.save_session(session2, description="s2")

        sessions = history.list_sessions()
        self.assertEqual(len(sessions), 2)


if __name__ == "__main__":
    unittest.main()
