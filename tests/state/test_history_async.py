"""Tests for off-main-thread session saving.

Background: ``SessionHistory.save_session`` serialises the entire transcript
to JSON and writes it to disk synchronously. When called from a Qt main-thread
handler (end-of-turn auto-save, tab close, new chat), this blocks the IDA Pro
event loop for the duration of the dump — a visible "freeze" spike that grows
with conversation size.

``save_session_async`` runs the same work on a background thread and returns a
``Future`` so the caller is not blocked. These tests assert that:
  1. The call returns immediately without blocking on disk I/O.
  2. The persisted file matches what ``save_session`` would have written.
  3. The work happens on a different thread than the caller.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from concurrent.futures import Future

# ``core.host`` snapshots the host at first import. Install IDA mocks before
# importing Rikugan so collection order cannot freeze it in standalone mode.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.core.config import RikuganConfig
from rikugan.core.types import Message, Role
from rikugan.state.history import SessionHistory
from rikugan.state.session import SessionState


def _make_session(message_count: int = 3) -> SessionState:
    """Build a minimal session with a few messages for persistence tests."""
    session = SessionState(
        id="test-session-async",
        idb_path="C:/fake/sample.idb",
        provider_name="anthropic",
        model_name="claude-test",
    )
    for i in range(message_count):
        session.add_message(Message(role=Role.USER if i % 2 == 0 else Role.ASSISTANT, content=f"msg {i}"))
    return session


class TestFlushSaves(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.mkdtemp(prefix="rikugan-hist-flush-")
        self._config = RikuganConfig()
        self._config._config_dir = self._tmp
        self._history = SessionHistory(self._config)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_blocks_until_all_saves_persisted(self) -> None:
        import json

        for i in range(5):
            session = _make_session(message_count=2)
            session.id = f"flush-test-{i}"
            self._history.save_session_async(session)

        SessionHistory.flush_saves(timeout=10.0)

        for i in range(5):
            path = os.path.join(self._history._dir, f"flush-test-{i}.json")
            self.assertTrue(os.path.exists(path), f"session {i} was not persisted before flush returned")
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["id"], f"flush-test-{i}")


class TestSaveSessionAsync(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.mkdtemp(prefix="rikugan-hist-test-")
        self._config = RikuganConfig()
        self._config._config_dir = self._tmp
        self._history = SessionHistory(self._config)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_returns_future_without_blocking(self) -> None:
        # The async call must return a Future immediately. If it blocks on
        # disk I/O synchronously, the end-of-turn main-thread freeze
        # regresses.
        session = _make_session()
        start = time.monotonic()
        future = self._history.save_session_async(session)
        elapsed = time.monotonic() - start

        self.assertIsInstance(future, Future)
        # Returning should be near-instant (well under 100ms). The actual
        # write happens in the background.
        self.assertLess(elapsed, 0.1, "save_session_async must not block the caller")
        future.result(timeout=5)

    def test_persisted_file_matches_sync_save(self) -> None:
        # The async path must produce the same file as save_session for the
        # SAME session object. We save one session synchronously, capture
        # its JSON, then re-save the same session asynchronously and
        # compare. ``current_turn`` is bumped to force a content change so
        # the second write is not a no-op on the on-disk mtime.
        import json
        import os

        session = _make_session(message_count=2)
        path_sync = self._history.save_session(session)
        with open(path_sync) as f:
            data_sync = json.load(f)

        session.current_turn += 1
        future = self._history.save_session_async(session)
        path_async = future.result(timeout=5)

        self.assertEqual(path_async, path_sync, "async save must target the same path")
        self.assertTrue(os.path.exists(path_async))
        with open(path_async) as f:
            data_async = json.load(f)

        # All top-level fields except the bumped current_turn must match.
        # This proves the async path serialises the whole transcript, not
        # a stub.
        self.assertEqual(data_async["messages"], data_sync["messages"])
        self.assertEqual(data_async["provider_name"], data_sync["provider_name"])
        self.assertEqual(data_async["model_name"], data_sync["model_name"])
        self.assertEqual(data_async["current_turn"], session.current_turn)

    def test_runs_on_different_thread_than_caller(self) -> None:
        # The whole point: the write must NOT happen on the caller's thread
        # (which, in production, is the IDA main thread).
        session = _make_session()
        caller_thread = threading.current_thread().ident

        captured: dict[str, int] = {}
        original = self._history.save_session

        def _spy(s, description=""):
            captured["worker_thread"] = threading.current_thread().ident or -1
            return original(s, description)

        self._history.save_session = _spy  # type: ignore[method-assign]
        try:
            future = self._history.save_session_async(session)
            future.result(timeout=5)
        finally:
            self._history.save_session = original  # type: ignore[method-assign]

        self.assertNotEqual(
            captured.get("worker_thread"),
            caller_thread,
            "save_session_async must execute on a worker thread, not the caller's thread",
        )


if __name__ == "__main__":
    unittest.main()
