"""Tests for the ``/knowledge`` slash command parser and handler.

Pure unit tests against a mock ``AgentLoop`` with a real knowledge
store on disk; no Qt, no IDA, no LLM calls.
"""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Generator
from unittest.mock import MagicMock

from rikugan.agent.loop import _parse_user_command
from rikugan.agent.loop_commands import _handle_knowledge_command
from rikugan.agent.turn import TurnEvent
from rikugan.core.config import RikuganConfig
from rikugan.memory.ingest import (
    ingest_exploration_finding,
    ingest_save_memory,
)
from rikugan.state.session import SessionState
from rikugan.tests.knowledge._helpers import fresh_store


class TestParser(unittest.TestCase):
    def test_plain_command(self):
        cmd = _parse_user_command("/knowledge")
        self.assertEqual(cmd.direct_command, "/knowledge")
        self.assertEqual(cmd.direct_arg, "")

    def test_with_query(self):
        cmd = _parse_user_command("/knowledge 0x401000")
        self.assertEqual(cmd.direct_command, "/knowledge")
        self.assertEqual(cmd.direct_arg, "0x401000")

    def test_with_text_query(self):
        cmd = _parse_user_command("/knowledge network communication")
        self.assertEqual(cmd.direct_command, "/knowledge")
        self.assertEqual(cmd.direct_arg, "network communication")


def _make_loop(idb_path: str, knowledge_enabled: bool = True) -> MagicMock:
    """Build a minimal AgentLoop stub for the handler."""
    session = SessionState(idb_path=idb_path)
    config = RikuganConfig()
    config.knowledge_enabled = knowledge_enabled
    loop = MagicMock()
    loop.session = session
    loop.config = config
    return loop


def _drain(gen: Generator[TurnEvent, None, None]) -> list[TurnEvent]:
    return list(gen)


class TestHandler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Canonical IDB filename via shared helper.
        store, paths = fresh_store(self.tmp)
        self.idb_path = paths.idb_path
        # Seed some knowledge
        ingest_save_memory(store, paths, fact="uses RC4 at 0x401000", category="crypto")
        ingest_save_memory(store, paths, fact="creates scheduled task for persistence", category="persistence")
        ingest_exploration_finding(
            store,
            paths,
            category="function_purpose",
            summary="RC4 KSA at 0x401000",
            address=0x401000,
            relevance="high",
            function_name="rc4_ksa",
        )

    def test_disabled(self):
        loop = _make_loop(self.idb_path, knowledge_enabled=False)
        events = _drain(_handle_knowledge_command(loop, ""))
        self.assertEqual(len(events), 1)
        self.assertIn("disabled", events[0].text.lower())

    def test_no_idb(self):
        loop = _make_loop("", knowledge_enabled=True)
        events = _drain(_handle_knowledge_command(loop, ""))
        self.assertEqual(len(events), 1)
        self.assertIn("no idb", events[0].text.lower())

    def test_overview(self):
        loop = _make_loop(self.idb_path, knowledge_enabled=True)
        events = _drain(_handle_knowledge_command(loop, ""))
        self.assertEqual(len(events), 1)
        text = events[0].text
        self.assertIn("Overview", text)
        self.assertIn("memories", text)
        # Should mention recent memory
        self.assertIn("rc4", text.lower())

    def test_search_match(self):
        loop = _make_loop(self.idb_path, knowledge_enabled=True)
        events = _drain(_handle_knowledge_command(loop, "rc4"))
        text = events[0].text
        self.assertIn("Memories", text)
        self.assertIn("Entities", text)

    def test_search_no_match(self):
        loop = _make_loop(self.idb_path, knowledge_enabled=True)
        events = _drain(_handle_knowledge_command(loop, "nonexistenttermzzz"))
        text = events[0].text
        self.assertIn("No matches", text)


if __name__ == "__main__":
    unittest.main()
