"""Tests for the default render cap in async restore.

Root cause (production log, 677-message session, 6.3s main-thread freeze):
``restore_from_messages_async`` builds a real widget for every message in
the session. A 677-message session with ~800 tool widgets costs ~6 seconds
of Qt widget creation on the main thread — inherent cost, not a bug.

The fix caps the number of messages that get a real widget: only the most
recent ``restore_max_rendered_messages`` (default 100) are materialised;
older messages keep their lightweight placeholder so the scrollbar stays
accurate. This turns a 6.3s restore into ~1s for the common case while
preserving scroll geometry.

These tests assert the cap contract on ``RestoreWorker``:
  1. Without a cap (``max_rendered=None``) the worker builds every spec
     (legacy behaviour, used by callers that opt in).
  2. With a cap, the worker skips specs whose index falls outside the
     most-recent ``max_rendered`` window — those placeholders are never
     replaced by real widgets.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.qt_stubs import ensure_pyside6_stubs

ensure_pyside6_stubs()

from rikugan.core.types import Message, Role  # noqa: E402
from rikugan.ui.chat_view import RestoreWorker  # noqa: E402


def _assistant_messages(count: int) -> list[Message]:
    return [Message(role=Role.ASSISTANT, content=f"msg {i}", id=f"m{i}") for i in range(count)]


class _ChunkCollector:
    """Drain the worker's queue synchronously after ``run()`` completes."""

    def __init__(self) -> None:
        self.specs: list = []
        self.finished = False

    def drain(self, worker: RestoreWorker) -> None:
        while True:
            try:
                kind, payload = worker.queue.get_nowait()
            except Exception:
                break
            if kind == "chunk":
                self.specs.extend(payload.specs)
            elif kind == "finished":
                self.finished = True


def _run_worker(worker: RestoreWorker) -> _ChunkCollector:
    collector = _ChunkCollector()
    worker.run()  # synchronous in-test: no QThread.start()
    collector.drain(worker)
    return collector



class TestRestoreWorkerCap(unittest.TestCase):
    def test_no_cap_builds_every_spec(self) -> None:
        # Legacy behaviour: max_rendered=None materialises every message.
        messages = _assistant_messages(5)
        worker = RestoreWorker(messages, max_rendered=None)
        collector = _run_worker(worker)
        self.assertEqual(len(collector.specs), 5)
        self.assertTrue(collector.finished)

    def test_cap_builds_only_most_recent_window(self) -> None:
        # With max_rendered=3 on 5 messages, only the last 3 specs should
        # be emitted. The first 2 stay as placeholders in the chat view
        # (their msg_ids are never produced here).
        messages = _assistant_messages(5)
        worker = RestoreWorker(messages, max_rendered=3)
        collector = _run_worker(worker)

        self.assertEqual(len(collector.specs), 3, "Only the most recent 3 specs must be built.")
        # The built specs must be the last 3 — verify by id.
        built_ids = {s.msg_id for s in collector.specs}
        self.assertEqual(built_ids, {"m2", "m3", "m4"})

    def test_cap_larger_than_count_builds_all(self) -> None:
        # Cap greater than the message count must not drop anything.
        messages = _assistant_messages(3)
        worker = RestoreWorker(messages, max_rendered=100)
        collector = _run_worker(worker)
        self.assertEqual(len(collector.specs), 3)

    def test_default_cap_applied_when_omitted(self) -> None:
        # The constructor default applies a sane cap so the hot path
        # (session restore) does not silently render 677 widgets.
        messages = _assistant_messages(5)
        worker = RestoreWorker(messages)  # no explicit cap
        self.assertIsNotNone(worker._max_rendered)
        self.assertGreater(worker._max_rendered, 0)


if __name__ == "__main__":
    unittest.main()
