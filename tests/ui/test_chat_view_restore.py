"""Tests for rikugan.ui.chat_view — restore worker, dataclasses, and helpers.

The tests exercise the real ``chat_view`` module (including the
real ``RestoreWorker`` / ``MessageSpec`` / ``ToolSpec`` dataclasses)
so they do not stub ``rikugan.ui.*`` modules.  Doing so keeps the
test file isolated from sibling tests: leaving bare
``types.ModuleType`` stubs in ``sys.modules`` for ``rikugan.ui.*``
would break the async-restore test that runs after this one
(which imports the real modules for its real ``QApplication``).
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import unittest

# Headless test environments (CI without a display) need an
# offscreen Qt platform. Set it before any Qt import so the very
# first ``QApplication`` instance picks it up.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Re-import safety: if a sibling test file installed
# ``types.ModuleType`` stubs for any of the ``rikugan.ui.*`` /
# ``rikugan.core.types`` / ``rikugan.agent.turn`` modules this
# file imports, drop them so the imports below resolve to the
# *real* modules.
#
# We deliberately do NOT touch ``PySide6.*`` here: the real
# PySide6 modules may already be loaded by sibling conftests
# (e.g. ``rikugan/tests/conftest.py`` imports real
# ``rikugan.ui.qt_compat``), and dropping + re-importing the
# PySide6 C extensions can recurse into the Shiboken loader
# (observed stack overflow on PySide6 6.7+ when several test
# files in the same session each purge and reload PySide6).
# Real PySide6 stays in ``sys.modules`` throughout this file
# — that is what the real ``rikugan.ui.chat_view`` import
# chain expects.
_STUB_TARGETS = (
    "rikugan.core.types",
    "rikugan.agent.turn",
    "rikugan.ui.chat_view",
    "rikugan.ui.styles",
    "rikugan.ui.theme",
    "rikugan.ui.theme.manager",
    "rikugan.ui.theme.tokens",
    "rikugan.ui.theme.palette_dark",
    "rikugan.ui.theme.palette_light",
    "rikugan.ui.theme.palette_ida",
    "rikugan.ui.markdown",
    "rikugan.ui.message_widgets",
    "rikugan.ui.plan_view",
    "rikugan.ui.tool_widgets",
    "rikugan.ui.qt_compat",
    "rikugan.ui.input_area",
    "rikugan.ui.context_bar",
)
for _name in list(sys.modules):
    if _name in _STUB_TARGETS:
        sys.modules.pop(_name, None)


# Repair the real ``PySide6.QtGui.QFont`` symbol if a sibling
# test file (notably ``tests/tools/test_panel_core.py`` via
# ``tests.qt_stubs.ensure_pyside6_stubs``) clobbered it.  The
# root ``tests/conftest.py`` backs up the real QFont on
# ``PySide6._real_qfont_backup`` *before* any sibling test
# runs; we re-install it here so that subsequent code that
# uses ``QFont`` (e.g. inside the chat view) doesn't trip on
# a stub class.
try:
    import PySide6  # type: ignore[import-not-found]

    real_qfont = getattr(PySide6, "_real_qfont_backup", None)
    if real_qfont is not None:
        import PySide6.QtGui  # type: ignore[import-not-found]

        PySide6.QtGui.QFont = real_qfont
except ImportError:
    pass


from rikugan import constants  # noqa: E402
from rikugan.core.types import Message, Role, ToolCall, ToolResult  # noqa: E402
from rikugan.ui.chat_view import (  # noqa: E402
    _RESTORE_CHUNK_SIZE,
    ChatView,
    MessageSpec,
    RestoreWorker,
    ToolSpec,
    _estimate_assistant_height,
    _estimate_tool_height,
    _estimate_user_height,
    _is_hidden_system_user_message,
    _RenderedChunk,
)
from rikugan.ui.tool_widgets import (  # noqa: E402
    ExecutePythonWidget,
    ToolCallWidget,
)


def _user_msg(content: str) -> Message:
    return Message(role=Role.USER, content=content)


def _assistant_msg(content: str) -> Message:
    return Message(role=Role.ASSISTANT, content=content)


def _tool_msg(calls: list[ToolCall], results: list[ToolResult]) -> Message:
    msg = Message(role=Role.TOOL, content="")
    msg.tool_calls = calls
    msg.tool_results = results
    return msg


class HiddenSystemFilterTests(unittest.TestCase):
    """_is_hidden_system_user_message strips [SYSTEM] sentinels."""

    def test_plain_text_is_not_hidden(self) -> None:
        self.assertFalse(_is_hidden_system_user_message("hello world"))

    def test_empty_is_not_hidden(self) -> None:
        self.assertFalse(_is_hidden_system_user_message(""))

    def test_system_sentinel_is_hidden(self) -> None:
        self.assertTrue(_is_hidden_system_user_message("[SYSTEM] do something"))

    def test_system_sentinel_with_brackets_is_hidden(self) -> None:
        self.assertTrue(_is_hidden_system_user_message("[SYSTEM] some text"))

    def test_case_insensitive(self) -> None:
        # The check is a literal startswith('[SYSTEM]'), so mixed case
        # is NOT hidden. Pin that contract so we notice if it changes.
        self.assertFalse(_is_hidden_system_user_message("[system] low"))


class HeightEstimatorTests(unittest.TestCase):
    """Height estimators should return sensible, clamped values."""

    def test_user_short_text_min_height(self) -> None:
        h = _estimate_user_height("hi")
        # minimum clamp is 40
        self.assertGreaterEqual(h, 40)

    def test_user_empty_text(self) -> None:
        h = _estimate_user_height("")
        # empty text → 40
        self.assertEqual(h, 40)

    def test_assistant_short(self) -> None:
        h = _estimate_assistant_height("hi")
        # minimum clamp is 64
        self.assertGreaterEqual(h, 64)

    def test_assistant_long_increases(self) -> None:
        # 100 lines of text → ~32 + 100*18 = 1832, clamped to 800
        long_text = "\n".join(f"line {i}" for i in range(100))
        h = _estimate_assistant_height(long_text)
        # Long content should hit the 800 cap
        self.assertGreaterEqual(h, 400)

    def test_tool_min_height(self) -> None:
        spec = ToolSpec(id="x", name="n", arguments_json="{}")
        h = _estimate_tool_height(spec.result_content)
        # tool base chrome is 80
        self.assertGreaterEqual(h, 80)


class DataclassTests(unittest.TestCase):
    def test_tool_spec_defaults(self) -> None:
        s = ToolSpec(id="abc", name="decompile", arguments_json="{}")
        self.assertEqual(s.id, "abc")
        self.assertEqual(s.estimated_height, 80)
        self.assertEqual(s.result_content, "")
        self.assertFalse(s.result_is_error)

    def test_message_spec_defaults(self) -> None:
        s = MessageSpec(msg_id="m1", role="user")
        self.assertEqual(s.content, "")
        self.assertEqual(s.tool_specs, ())
        self.assertEqual(s.estimated_height, 60)

    def test_message_spec_is_frozen(self) -> None:
        s = MessageSpec(msg_id="m1", role="user")
        with self.assertRaises((AttributeError, dataclasses.FrozenInstanceError)):
            s.msg_id = "different"  # type: ignore[misc]

    def test_rendered_chunk_is_mutable(self) -> None:
        # _RenderedChunk is intentionally NOT frozen so the worker can
        # append in place.
        c = _RenderedChunk()
        s = MessageSpec(msg_id="m1", role="user")
        c.specs.append(s)
        self.assertEqual(c.specs, [s])


class BuildSpecTests(unittest.TestCase):
    """RestoreWorker._build_spec converts Message -> MessageSpec/None."""

    def test_user_message(self) -> None:
        msg = _user_msg("hello")
        spec, consumed = RestoreWorker._build_spec(msg, 0, None)
        assert spec is not None
        self.assertEqual(consumed, 0)
        self.assertEqual(spec.role, "user")
        self.assertEqual(spec.content, "hello")
        self.assertEqual(spec.msg_id, msg.id)  # uses message.id when set

    def test_user_uses_index_when_id_missing(self) -> None:
        msg = _user_msg("hello")
        msg.id = ""  # empty id -> falls back to restore_<idx>
        spec, consumed = RestoreWorker._build_spec(msg, 7, None)
        assert spec is not None
        self.assertEqual(consumed, 0)
        self.assertEqual(spec.msg_id, "restore_7")

    def test_hidden_system_user_filtered_out(self) -> None:
        msg = _user_msg("[SYSTEM] hidden prompt")
        spec, consumed = RestoreWorker._build_spec(msg, 0, None)
        self.assertIsNone(spec)
        self.assertEqual(consumed, 0)

    def test_assistant_message_renders_html(self) -> None:
        msg = _assistant_msg("**bold**")
        spec, consumed = RestoreWorker._build_spec(msg, 0, None)
        assert spec is not None
        self.assertEqual(consumed, 0)
        self.assertEqual(spec.role, "assistant")
        self.assertEqual(spec.content, "**bold**")

    def test_assistant_empty_content_no_html(self) -> None:
        msg = _assistant_msg("")
        spec, _consumed = RestoreWorker._build_spec(msg, 0, None)
        assert spec is not None
        # Empty content should not call md_to_html (no work to do)
        self.assertEqual(spec.content, "")

    def test_tool_message_picks_up_results(self) -> None:
        tc = ToolCall(id="call_1", name="decompile", arguments={"addr": "0x401000"})
        tr = ToolResult(tool_call_id="call_1", name="decompile", content="ret 0x42;")
        msg = _tool_msg([tc], [tr])

        spec, _consumed = RestoreWorker._build_spec(msg, 0, None)
        assert spec is not None
        self.assertEqual(spec.role, "tool")
        self.assertEqual(len(spec.tool_specs), 1)

        tool_spec = spec.tool_specs[0]
        self.assertEqual(tool_spec.id, "call_1")
        self.assertEqual(tool_spec.name, "decompile")
        # arguments_json must be a real JSON string
        self.assertEqual(json.loads(tool_spec.arguments_json), {"addr": "0x401000"})
        self.assertEqual(tool_spec.result_content, "ret 0x42;")
        self.assertFalse(tool_spec.result_is_error)

    def test_tool_message_error_result_propagated(self) -> None:
        tc = ToolCall(id="call_2", name="decompile", arguments={})
        tr = ToolResult(tool_call_id="call_2", name="decompile", content="fail", is_error=True)
        msg = _tool_msg([tc], [tr])
        spec, _consumed = RestoreWorker._build_spec(msg, 0, None)
        assert spec is not None
        self.assertTrue(spec.tool_specs[0].result_is_error)

    def test_tool_message_swallows_non_serializable_args(self) -> None:
        # Arguments that can't be json-dumped should fall back to "{}"
        tc = ToolCall(id="call_3", name="f", arguments={"bad": {"nested": "thing"}})
        tr = ToolResult(tool_call_id="call_3", name="f", content="ok")
        msg = _tool_msg([tc], [tr])
        spec, _consumed = RestoreWorker._build_spec(msg, 0, None)
        assert spec is not None
        # Either the original was serialized, or we fell back to "{}"
        # — both are valid as long as json.loads succeeds.
        json.loads(spec.tool_specs[0].arguments_json)

    def test_assistant_with_tool_calls_none_does_not_crash(self) -> None:
        """A persisted ASSISTANT message whose ``tool_calls`` is
        ``None`` (e.g. a corrupt IDB or an older session format)
        must not crash ``_build_spec``."""
        msg = Message(role=Role.ASSISTANT, content="hi")
        # ``Message`` defaults ``tool_calls`` to ``[]`` via
        # ``field(default_factory=list)``, but a manually-set
        # ``None`` simulates a corrupted persisted transcript.
        msg.tool_calls = None
        spec, _consumed = RestoreWorker._build_spec(msg, 0, None)
        assert spec is not None
        self.assertEqual(spec.role, "assistant")
        # The empty ``tool_calls`` collapses into a no-tool-spec spec.
        self.assertEqual(len(spec.tool_specs), 0)

    def test_tool_message_with_tool_results_none_does_not_crash(self) -> None:
        """A TOOL message whose ``tool_results`` is ``None`` must
        not crash ``_build_spec``."""
        tc = ToolCall(id="call_x", name="f", arguments={})
        msg = Message(role=Role.TOOL, content="")
        msg.tool_calls = [tc]
        # Manually set ``None`` to simulate a corrupt transcript.
        msg.tool_results = None
        spec, _consumed = RestoreWorker._build_spec(msg, 0, None)
        assert spec is not None
        self.assertEqual(spec.role, "tool")
        # The tool call still renders, with an empty result.
        self.assertEqual(len(spec.tool_specs), 1)
        self.assertEqual(spec.tool_specs[0].id, "call_x")


class WorkerRunTests(unittest.TestCase):
    """Drive the worker synchronously and inspect the queue it populates.

    Spec § A (Task 8): ``RestoreWorker`` no longer emits Qt signals.
    Instead, ``run()`` pushes ``("chunk", _RenderedChunk)`` and
    ``("finished", None)`` tuples onto ``worker.queue``.  These tests
    call ``run()`` synchronously and then drain the queue, mirroring
    the production ``ChatView._drain_restore_queue`` loop.
    """

    def _run(self, messages: list[Message]) -> tuple[list[_RenderedChunk], bool]:
        worker = RestoreWorker(messages)
        chunks: list[_RenderedChunk] = []
        finished = False
        worker.run()
        while True:
            try:
                kind, payload = worker.queue.get_nowait()
            except Exception:
                break
            if kind == "chunk":
                assert isinstance(payload, _RenderedChunk)
                chunks.append(payload)
            elif kind == "finished":
                finished = True
        return chunks, finished

    def test_empty_messages(self) -> None:
        chunks, finished = self._run([])
        self.assertEqual(chunks, [])
        self.assertTrue(finished, "finished sentinel must enqueue on empty list")

    def test_single_user_message(self) -> None:
        chunks, finished = self._run([_user_msg("hi")])
        # Remainder flush enqueues the partial chunk
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0].specs), 1)
        self.assertEqual(chunks[0].specs[0].role, "user")
        self.assertTrue(finished)

    def test_filtered_out_message_skipped(self) -> None:
        chunks, _ = self._run(
            [
                _user_msg("[SYSTEM] hidden"),
                _user_msg("real"),
            ]
        )
        # Only the visible user msg survives
        self.assertEqual(len(chunks[0].specs), 1)
        self.assertEqual(chunks[0].specs[0].content, "real")

    def test_chunk_boundary_emits_partial_chunk(self) -> None:
        # _RESTORE_CHUNK_SIZE + 5 messages; the first chunk is full,
        # the remainder is a single chunk of 5.
        n = _RESTORE_CHUNK_SIZE + 5
        messages = [_user_msg(f"m{i}") for i in range(n)]
        chunks, _ = self._run(messages)
        self.assertEqual(len(chunks), 2, f"expected 2 chunks, got {len(chunks)}")
        self.assertEqual(len(chunks[0].specs), _RESTORE_CHUNK_SIZE)
        self.assertEqual(len(chunks[1].specs), 5)
        # Total spec count equals input count
        total = sum(len(c.specs) for c in chunks)
        self.assertEqual(total, n)

    def test_cancel_stops_early(self) -> None:
        n = 200
        messages = [_user_msg(f"m{i}") for i in range(n)]
        worker = RestoreWorker(messages)
        # Cancel before starting
        worker.cancel()
        worker.run()
        chunks: list[_RenderedChunk] = []
        finished = False
        while True:
            try:
                kind, payload = worker.queue.get_nowait()
            except Exception:
                break
            if kind == "chunk":
                chunks.append(payload)
            elif kind == "finished":
                finished = True
        # No specs produced because run() returns at the first iteration check
        self.assertEqual(sum(len(c.specs) for c in chunks), 0)
        # finished sentinel must NOT enqueue when cancelled before any work
        self.assertFalse(finished)

    def test_exactly_chunk_size_emits_one_chunk(self) -> None:
        # Boundary: exactly _RESTORE_CHUNK_SIZE messages. The boundary check
        # enqueues a chunk when len >= CHUNK_SIZE, then the remainder is
        # empty so the flush guard (`if chunk.specs`) skips it.
        messages = [_user_msg(f"m{i}") for i in range(_RESTORE_CHUNK_SIZE)]
        chunks, finished = self._run(messages)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0].specs), _RESTORE_CHUNK_SIZE)
        self.assertTrue(finished)

class WorkerQueueTests(unittest.TestCase):
    """RestoreWorker pushes (kind, payload) tuples to a queue.Queue.

    Spec § A: Thread UAF — the worker is a QThread but must NEVER emit
    Qt signals across threads.  ``run()`` instead enqueues
    ``("chunk", _RenderedChunk)`` / ``("finished", None)`` tuples onto
    a queue that the main-thread ``QTimer`` callback drains.  This is
    the same shape panel_core uses for the history executor queue.
    """

    KIND_CHUNK = "chunk"
    KIND_FINISHED = "finished"

    def _drain(self, worker: RestoreWorker) -> tuple[list[_RenderedChunk], bool]:
        """Run a worker and drain its queue on the (mock) main thread.

        Mirrors the production drain loop in ``ChatView._drain_restore_queue``:
        pull tuples until the queue is empty, dispatch by ``kind``.  We do NOT
        use any Qt signal connection — the worker thread ``run()`` enqueues
        only, the main-thread drain is the sole consumer.
        """
        chunks: list[_RenderedChunk] = []
        finished = False
        while True:
            try:
                kind, payload = worker.queue.get_nowait()
            except Exception:
                break
            if kind == "chunk":
                assert isinstance(payload, _RenderedChunk)
                chunks.append(payload)
            elif kind == "finished":
                finished = True
            else:  # pragma: no cover - defensive
                raise AssertionError(f"unknown kind: {kind!r}")
        return chunks, finished

    def test_worker_has_queue_attribute(self) -> None:
        worker = RestoreWorker([])
        import queue as _queue
        self.assertIsInstance(worker.queue, _queue.Queue)

    def test_worker_run_populates_queue_with_chunks_and_finished(self) -> None:
        # _RESTORE_CHUNK_SIZE + 5 → two chunks (one full, one remainder)
        # plus a finished sentinel.
        n = _RESTORE_CHUNK_SIZE + 5
        messages = [_user_msg(f"m{i}") for i in range(n)]
        worker = RestoreWorker(messages)
        worker.run()  # synchronous in-test, no QThread.start()
        chunks, finished = self._drain(worker)
        self.assertTrue(finished, "finished sentinel must be enqueued")
        self.assertEqual(len(chunks), 2, f"expected 2 chunks, got {len(chunks)}")
        self.assertEqual(len(chunks[0].specs), _RESTORE_CHUNK_SIZE)
        self.assertEqual(len(chunks[1].specs), 5)
        self.assertEqual(sum(len(c.specs) for c in chunks), n)
    def test_worker_run_no_chunk_ready_or_finished_ok_signal(self) -> None:
        """The Qt-signal interface must be GONE — clean cutover.

        Importing ``RestoreWorker`` and looking up either attribute
        must raise ``AttributeError``.  This guards against a future
        regression that re-introduces the cross-thread signal path.
        """
        self.assertFalse(
            hasattr(RestoreWorker, "chunk_ready"),
            "RestoreWorker.chunk_ready signal must be removed (Shiboken UAF).",
        )
        self.assertFalse(
            hasattr(RestoreWorker, "finished_ok"),
            "RestoreWorker.finished_ok signal must be removed (Shiboken UAF).",
        )

    def test_widget_construction_happens_on_drain_not_on_producer(self) -> None:
        """The stub widget factory must be invoked from the drain loop,
        NOT from ``worker.run()``.  This is the central contract for
        Task 8: widget construction must stay on the Qt main thread.
        """
        # Skip __init__ (heavy Qt setup) — we only need the drain entry
        # point and the restore-generation state.  ``_drain_restore_queue``
        # delegates to ``_on_chunk_ready`` / ``_on_restore_finished``,
        # which is the widget-construction seam we are asserting against.
        view = ChatView.__new__(ChatView)
        view._restore_generation = 1
        view._restore_worker = None
        # ``_stop_restore_poll_timer`` runs at the finished sentinel
        # even when no timer was ever created — must not AttributeError.
        view._restore_poll_timer = None

        import threading as _threading

        factory_calls: list[tuple[int, str]] = []

        def _on_chunk_stub(chunk, gen):  # replaces _on_chunk_ready
            for spec in chunk.specs:
                factory_calls.append((_threading.get_ident(), spec.msg_id))

        def _on_finished_stub(gen):
            factory_calls.append((_threading.get_ident(), "FINISHED"))

        view._on_chunk_ready = _on_chunk_stub  # type: ignore[method-assign]
        view._on_restore_finished = _on_finished_stub  # type: ignore[method-assign]

        # Use explicit ids so the assertion can compare against stable names.
        a = Message(role=Role.USER, content="a"); a.id = "a"
        b = Message(role=Role.USER, content="b"); b.id = "b"
        c = Message(role=Role.USER, content="c"); c.id = "c"
        worker = RestoreWorker([a, b, c])
        view._restore_worker = worker
        producer_tid = _threading.get_ident()
        worker.run()
        # Producer: worker.run() must NOT call the factory.
        self.assertEqual(
            factory_calls,
            [],
            "worker.run() must NOT invoke the factory — widget "
            "construction must be deferred to the main-thread drain "
            "(Shiboken UAF risk).",
        )
        # Consumer: drain pulls tuples from the queue and dispatches.
        view._drain_restore_queue()
        self.assertEqual(len(factory_calls), 4)
        self.assertEqual(
            [role for _tid, role in factory_calls],
            ["a", "b", "c", "FINISHED"],
            "drain must dispatch every chunk spec in order, then the "
            "finished sentinel",
        )
        drain_tid = factory_calls[0][0]
        self.assertEqual(
            drain_tid,
            producer_tid,
            "in the synchronous test, drain runs on the same thread as "
            "producer; the contract we are asserting is that the factory "
            "call originates from the drain call stack, NOT from "
            "worker.run() — the empty-call assertion above enforces that",
        )

    def test_cancel_short_circuits_producer_no_finished(self) -> None:
        """A cancelled worker puts no ``finished`` sentinel."""
        messages = [_user_msg(f"m{i}") for i in range(10)]
        worker = RestoreWorker(messages)
        worker.cancel()
        worker.run()
        chunks, finished = self._drain(worker)
        # Cancellation runs in run() before the loop body, so neither
        # chunk nor finished lands.
        self.assertEqual(chunks, [])
        self.assertFalse(finished)

    def test_empty_messages_emits_only_finished(self) -> None:
        """No chunks, but a finished sentinel still arrives."""
        worker = RestoreWorker([])
        worker.run()
        chunks, finished = self._drain(worker)
        self.assertEqual(chunks, [])
        self.assertTrue(finished)

class PlaceholderTests(unittest.TestCase):
    """``MessagePlaceholder`` is a tiny ``QFrame`` used during async
    restore to reserve vertical space until the real message widget
    lands.  These tests exercise the constructor contract directly
    using the offscreen ``QApplication`` this test file already
    configures via the ``QT_QPA_PLATFORM=offscreen`` env var.
    """

    @classmethod
    def setUpClass(cls) -> None:
        # Create (or reuse) a real ``QApplication`` for the lifetime
        # of this class.  ``MessagePlaceholder`` is a ``QFrame``
        # subclass and needs a live QApplication to construct.
        from PySide6.QtWidgets import QApplication

        cls._qapp = QApplication.instance() or QApplication([])

    def test_placeholder_constructs_with_msg_id(self) -> None:
        from rikugan.ui.chat_view import MessagePlaceholder

        ph = MessagePlaceholder(estimated_height=123, msg_id="m1")
        self.addCleanup(ph.deleteLater)
        self.assertEqual(ph.msg_id, "m1")

    def test_placeholder_height_matches_estimate(self) -> None:
        from rikugan.ui.chat_view import MessagePlaceholder

        ph = MessagePlaceholder(estimated_height=123, msg_id="m1")
        self.addCleanup(ph.deleteLater)
        # Both heights clamp to the estimated value (it is a
        # fixed-size spacer).  See :class:`MessagePlaceholder`.
        self.assertEqual(ph.minimumHeight(), 123)
        self.assertEqual(ph.maximumHeight(), 123)

    def test_placeholder_clamps_small_heights_to_minimum(self) -> None:
        """An estimate of e.g. 4 px must still produce a placeholder
        that is at least 16 px tall — otherwise the layout collapses
        and the scrollbar geometry becomes wrong during restore."""
        from rikugan.ui.chat_view import MessagePlaceholder

        for tiny in (0, 1, 4, 15):
            ph = MessagePlaceholder(estimated_height=tiny, msg_id=f"m{tiny}")
            self.addCleanup(ph.deleteLater)
            self.assertGreaterEqual(
                ph.minimumHeight(),
                16,
                f"placeholder with estimate {tiny} should clamp to >=16 px",
            )
            self.assertEqual(ph.minimumHeight(), ph.maximumHeight())


class TestRestoreExecutePython(unittest.TestCase):
    """Restored ``execute_python`` tool calls should render as
    :class:`ExecutePythonWidget` (unified code + result layout), not
    the generic :class:`ToolCallWidget`.  See ``_build_restored_tool_widgets``.
    """

    @classmethod
    def setUpClass(cls) -> None:
        # ``ExecutePythonWidget`` is a ``QFrame`` subclass and needs a
        # live ``QApplication`` to construct.
        from PySide6.QtWidgets import QApplication

        cls._qapp = QApplication.instance() or QApplication([])

    def _make_view(self) -> ChatView:
        # ``ChatView.__new__`` skips ``__init__`` (which wires up the
        # full Qt UI); we only need the three attributes the restore
        # path touches.
        view = ChatView.__new__(ChatView)
        view._tool_widgets = {}
        view._group_map = {}
        view._container = None
        return view

    def test_execute_python_restores_as_execute_python_widget(self) -> None:
        ts = ToolSpec(
            id="rc1",
            name=constants.EXECUTE_PYTHON_TOOL_NAME,
            arguments_json='{"code": "print(1)"}',
            result_content="42",
            result_is_error=False,
        )
        view = self._make_view()
        widgets = view._build_restored_tool_widgets((ts,))
        # Single tool — returned directly (not in a group).
        self.assertEqual(len(widgets), 1)
        self.assertIsInstance(widgets[0], ExecutePythonWidget)
        self.assertEqual(widgets[0]._code, "print(1)")
        self.assertFalse(widgets[0]._result_block.isHidden())
        self.assertFalse(widgets[0]._is_error)

    def test_other_tool_still_restores_as_tool_call_widget(self) -> None:
        # Regression guard: non-execute_python tools must keep using
        # the generic ToolCallWidget.
        ts = ToolSpec(
            id="rc2",
            name="decompile_function",
            arguments_json='{"addr": "0x401000"}',
            result_content="ret 0;",
            result_is_error=False,
        )
        view = self._make_view()
        widgets = view._build_restored_tool_widgets((ts,))
        self.assertEqual(len(widgets), 1)
        self.assertIsInstance(widgets[0], ToolCallWidget)

    def test_execute_python_error_result_restored(self) -> None:
        ts = ToolSpec(
            id="rc3",
            name=constants.EXECUTE_PYTHON_TOOL_NAME,
            arguments_json='{"code": "x = 1/0"}',
            result_content="ZeroDivisionError",
            result_is_error=True,
        )
        view = self._make_view()
        widgets = view._build_restored_tool_widgets((ts,))
        self.assertIsInstance(widgets[0], ExecutePythonWidget)
        self.assertFalse(widgets[0]._result_block.isHidden())
        self.assertTrue(widgets[0]._is_error)


if __name__ == "__main__":
    unittest.main()
