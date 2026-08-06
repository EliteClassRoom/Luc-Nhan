"""Tests for GLM reasoning rendering in ChatView.

Exercises REASONING_DELTA coalescing into a single transient
_ThinkingBlock, RECOVERY_START removal of that block, the
TOOL_CALL_DISCARDED terminal state, and reasoning-aware restore
(Message.reasoning_content preferred over legacy <think> tags).
"""

from __future__ import annotations

import os
import sys
import unittest

# Headless test environments need an offscreen Qt platform.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Re-import safety: drop sibling stubs that may clobber real modules.
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

# Repair real QFont if a sibling test clobbered it.
try:
    import PySide6  # type: ignore[import-not-found]

    real_qfont = getattr(PySide6, "_real_qfont_backup", None)
    if real_qfont is not None:
        import PySide6.QtGui  # type: ignore[import-not-found]

        PySide6.QtGui.QFont = real_qfont
except ImportError:
    pass

from rikugan.agent.turn import TurnEvent  # noqa: E402
from rikugan.core.types import Message, Role  # noqa: E402
from rikugan.ui.chat_view import ChatView, MessageSpec, RestoreWorker  # noqa: E402


class _ChatViewHarness:
    """Build a minimal ChatView without running __init__.

    Only the attributes needed by the GLM reasoning event handlers
    are populated.  The real Qt widgets (_ThinkingBlock, QLabel, etc.)
    require a QApplication, which is created once per test class.
    """

    @staticmethod
    def make() -> ChatView:
        from PySide6.QtWidgets import QVBoxLayout, QWidget

        view = ChatView.__new__(ChatView)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.addStretch()
        view._container = container
        view._layout = layout
        view._current_assistant = None
        view._message_thinking = None
        view._tool_widgets = {}
        view._group_map = {}
        view._tool_run_ids = []
        view._tool_run_names = []
        view._tool_run_widgets = []
        view._tool_group = None
        view._plan_view = None
        view._think_buffer = ""
        view._waiting_think_close = False
        view._restore_paged = False
        view._restore_first_page = 0
        view._restore_last_page = 0
        view._restore_pages = []
        view._in_restore = False
        view._scroll_to_bottom = lambda: None  # type: ignore[assignment]
        view._is_near_bottom = lambda: True  # type: ignore[assignment]
        view._hide_thinking = lambda: None  # type: ignore[assignment]
        view._force_hide_thinking = lambda: None  # type: ignore[assignment]
        view._reset_tool_run = lambda: None  # type: ignore[assignment]
        return view


class TestReasoningDeltaRendering(unittest.TestCase):
    """REASONING_DELTA events append to one transient _ThinkingBlock."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls._qapp = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._view = _ChatViewHarness.make()

    def test_reasoning_delta_creates_one_transient_thinking_block(self):
        self._view.handle_event(TurnEvent.reasoning_event("first"))
        first_block = self._view._message_thinking
        assert first_block is not None
        assert first_block._source_text == "first"

    def test_reasoning_delta_appends_to_same_block(self):
        self._view.handle_event(TurnEvent.reasoning_event("first"))
        first_block = self._view._message_thinking
        self._view.handle_event(TurnEvent.reasoning_event(" second"))

        assert self._view._message_thinking is first_block
        assert first_block._source_text == "first second"

    def test_reasoning_delta_does_not_create_assistant_widget(self):
        self._view.handle_event(TurnEvent.reasoning_event("thinking only"))
        assert self._view._current_assistant is None


class TestRecoveryStartRendering(unittest.TestCase):
    """RECOVERY_START removes the transient reasoning block exactly once
    and inserts one compact status label."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls._qapp = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._view = _ChatViewHarness.make()

    def _count_recovery_status_widgets(self) -> int:
        count = 0
        for index in range(self._view._layout.count()):
            item = self._view._layout.itemAt(index)
            widget = item.widget() if item is not None else None
            if widget is not None and widget.objectName() == "recovery_status":
                count += 1
        return count

    def test_recovery_start_clears_transient_reasoning(self):
        self._view.handle_event(TurnEvent.reasoning_event("discard me"))
        assert self._view._message_thinking is not None
        self._view.handle_event(
            TurnEvent.recovery_start(
                attempt=2,
                reason="reasoning_degenerated",
                discard_transient_reasoning=True,
            )
        )
        assert self._view._message_thinking is None

    def test_recovery_start_removes_thinking_block_from_layout(self):
        """After recovery, no thinking_block widget should remain in the layout."""
        self._view.handle_event(TurnEvent.reasoning_event("discard me"))
        self._view.handle_event(
            TurnEvent.recovery_start(
                attempt=1,
                reason="stream_broken",
                discard_transient_reasoning=True,
            )
        )
        for index in range(self._view._layout.count()):
            item = self._view._layout.itemAt(index)
            widget = item.widget() if item is not None else None
            if widget is not None:
                assert widget.objectName() != "thinking_block", (
                    "thinking_block widget should have been removed from layout"
                )

    def test_recovery_start_inserts_exactly_one_status_label(self):
        self._view.handle_event(TurnEvent.reasoning_event("discard me"))
        self._view.handle_event(
            TurnEvent.recovery_start(
                attempt=2,
                reason="reasoning_degenerated",
                discard_transient_reasoning=True,
            )
        )
        assert self._count_recovery_status_widgets() == 1

    def test_recovery_start_without_prior_reasoning_is_safe(self):
        self._view.handle_event(
            TurnEvent.recovery_start(
                attempt=1,
                reason="stream_broken",
                discard_transient_reasoning=False,
            )
        )
        assert self._view._message_thinking is None
        assert self._count_recovery_status_widgets() == 1

    def test_recovery_start_is_hard_boundary_no_coalescing(self):
        """Two recovery_start events must produce two status labels,
        not coalesce into one."""
        self._view.handle_event(TurnEvent.reasoning_event("r1"))
        self._view.handle_event(
            TurnEvent.recovery_start(
                attempt=1,
                reason="stream_broken",
                discard_transient_reasoning=True,
            )
        )
        self._view.handle_event(TurnEvent.reasoning_event("r2"))
        self._view.handle_event(
            TurnEvent.recovery_start(
                attempt=2,
                reason="reasoning_degenerated",
                discard_transient_reasoning=True,
            )
        )
        assert self._count_recovery_status_widgets() == 2


class TestToolCallDiscarded(unittest.TestCase):
    """TOOL_CALL_DISCARDED stops the tool widget spinner without
    requiring a result or approval."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls._qapp = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._view = _ChatViewHarness.make()

    def test_tool_call_discarded_stops_spinner(self):
        from rikugan.ui.tool_widgets import ToolCallWidget

        # Simulate a prior TOOL_CALL_START that registered a widget
        self._view.handle_event(TurnEvent.tool_call_start("call_1", "decompile_function"))
        tw = self._view._tool_widgets.get("call_1")
        assert tw is not None
        # Now discard it
        self._view.handle_event(
            TurnEvent.tool_call_discarded("call_1", "decompile_function", "truncated_partial_tool_use")
        )
        # The status label must show a terminal glyph (not the spinner)
        status = tw._status_label.text()
        assert status not in ToolCallWidget._SPINNER_FRAMES, f"spinner still running after discard: {status!r}"

    def test_execute_python_mark_discarded_stops_lifecycle(self):
        """ExecutePythonWidget.mark_discarded sets a neutral terminal glyph."""
        from rikugan import constants
        from rikugan.ui.tool_widgets import ExecutePythonWidget

        self._view.handle_event(TurnEvent.tool_call_start("ep_call", constants.EXECUTE_PYTHON_TOOL_NAME))
        tw = self._view._tool_widgets.get("ep_call")
        assert isinstance(tw, ExecutePythonWidget)
        self._view.handle_event(
            TurnEvent.tool_call_discarded("ep_call", constants.EXECUTE_PYTHON_TOOL_NAME, "truncated_args")
        )
        # Status icon must show a terminal glyph (not the spinner '')
        assert tw._status_icon.text() not in ("", "⟳"), (
            f"spinner still running after discard: {tw._status_icon.text()!r}"
        )


class TestReasoningBlockFinalization(unittest.TestCase):
    """Reasoning block header transitions from 'Thinking...' to 'Thinking'
    when visible text begins or TEXT_DONE/TURN_END arrives."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls._qapp = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._view = _ChatViewHarness.make()

    def test_reasoning_in_progress_shows_ellipsis(self):
        self._view.handle_event(TurnEvent.reasoning_event("thinking hard"))
        block = self._view._message_thinking
        assert block is not None
        assert block._in_progress is True
        assert "…" in block._header_label.text()  # contains "..."

    def test_text_delta_finalizes_reasoning_header(self):
        self._view.handle_event(TurnEvent.reasoning_event("thinking hard"))
        block = self._view._message_thinking
        assert block is not None
        assert block._in_progress is True

        # TEXT_DELTA arrives — reasoning should finalize.
        self._view.handle_event(TurnEvent.text_delta("visible answer"))
        assert block._in_progress is False
        assert "…" not in block._header_label.text()

    def test_turn_end_finalizes_reasoning_header(self):
        self._view.handle_event(TurnEvent.reasoning_event("thinking hard"))
        block = self._view._message_thinking
        assert block is not None

        self._view.handle_event(TurnEvent.turn_end(1))
        assert block._in_progress is False


class TestReasoningAwareRestore(unittest.TestCase):
    """MessageSpec carries reasoning_content from Message; restore
    prefers the new field over legacy <think> tags."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls._qapp = QApplication.instance() or QApplication([])

    def test_restore_prefers_reasoning_field(self):
        new_message = Message(role=Role.ASSISTANT, content="answer", reasoning_content="reason")
        spec, _consumed = RestoreWorker._build_spec(new_message, 0, None)
        assert spec is not None
        assert spec.reasoning_content == "reason"
        # Visible content should be just "answer", not parsed from <think>
        assert spec.content == "answer"

    def test_restore_preserves_legacy_think_tags(self):
        legacy = Message(
            role=Role.ASSISTANT,
            content="<think>old</think>legacy answer",
        )
        spec, _consumed = RestoreWorker._build_spec(legacy, 1, None)
        assert spec is not None
        assert spec.reasoning_content == ""
        # Legacy content still carries the <think> tag; _split_thinking
        # handles it at render time (as before).
        assert spec.content == "<think>old</think>legacy answer"

    def test_message_spec_has_reasoning_content_field(self):
        spec = MessageSpec(msg_id="m1", role="assistant")
        # Default must be empty string for backward compatibility
        assert spec.reasoning_content == ""

    def test_build_spec_reasoning_does_not_leak_into_content_html(self):
        """When reasoning_content is set, the pre-rendered content_html
        must contain only the visible content (no reasoning text)."""
        msg = Message(
            role=Role.ASSISTANT,
            content="visible only",
            reasoning_content="secret reasoning",
        )
        spec, _consumed = RestoreWorker._build_spec(msg, 0, None)
        assert spec is not None
        assert "secret reasoning" not in spec.content_html
        assert "visible only" in spec.content_html or spec.content_html == ""


class TestReasoningResetBetweenTurns(unittest.TestCase):
    """After TEXT_DONE, reasoning state must reset so the next turn's
    REASONING_DELTA creates a fresh _ThinkingBlock instead of appending
    to the previous turn's finalised block."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls._qapp = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._view = _ChatViewHarness.make()

    def test_text_done_clears_message_thinking(self):
        """_message_thinking must be None after TEXT_DONE so the next
        REASONING_DELTA starts a fresh block."""
        self._view.handle_event(TurnEvent.reasoning_event("first reasoning"))
        assert self._view._message_thinking is not None

        self._view.handle_event(TurnEvent.text_delta("visible answer"))
        self._view.handle_event(TurnEvent.text_done("visible answer"))

        assert self._view._message_thinking is None

    def test_second_turn_reasoning_creates_new_block(self):
        """Full two-turn cycle: turn1 reasoning + text + TEXT_DONE +
        TURN_END, then turn2 REASONING_DELTA must create a NEW block."""
        # Turn 1
        self._view.handle_event(TurnEvent.reasoning_event("reasoning A"))
        block_a = self._view._message_thinking
        assert block_a is not None
        self._view.handle_event(TurnEvent.text_delta("answer A"))
        self._view.handle_event(TurnEvent.text_done("answer A"))
        self._view.handle_event(TurnEvent.turn_end(1))

        # Turn 2
        self._view.handle_event(TurnEvent.reasoning_event("reasoning B"))
        block_b = self._view._message_thinking
        assert block_b is not None
        # Must be a different widget instance, not the stale one from turn 1
        assert block_b is not block_a, "second-turn reasoning reused the first turn's _ThinkingBlock"
        assert block_b._source_text == "reasoning B"

    def test_text_done_clears_think_buffer(self):
        """_think_buffer and _waiting_think_close must reset after TEXT_DONE."""
        self._view._think_buffer = "stale"
        self._view._waiting_think_close = True

        self._view.handle_event(TurnEvent.text_done("done"))

        assert self._view._think_buffer == ""
        assert self._view._waiting_think_close is False


if __name__ == "__main__":
    unittest.main()
