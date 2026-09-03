"""Chat view: scrollable area containing message widgets."""

from __future__ import annotations
import json
import queue
import time
from dataclasses import dataclass, field

from .. import constants
from ..agent.turn import TurnEvent, TurnEventType
from ..core.logging import log_debug
from ..core.types import Message, Role, ToolResult
from .markdown import md_to_html
from .message_widgets import (
    AssistantMessageWidget,
    ErrorMessageWidget,
    ExplorationFindingWidget,
    ExplorationPhaseWidget,
    QueuedMessageWidget,
    ResearchNoteWidget,
    SubagentEventWidget,
    ThinkingWidget,
    UserMessageWidget,
    UserQuestionWidget,
    _extract_thinking_text,
    _extract_visible_text,
    _split_thinking,
    _ThinkingBlock,
)
from .plan_view import PlanView
from .qt_compat import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    Qt,
    QThread,
    QTimer,
    QVBoxLayout,
    QWidget,
    Signal,
)
from .styles import (
    get_history_nav_button_style,
    get_history_nav_frame_style,
    get_history_nav_label_style,
    get_tool_colors,
    is_host_theme,
)
from .tool_widgets import ExecutePythonWidget, ToolApprovalWidget, ToolCallWidget, ToolGroupWidget

_THINKING_MIN_DISPLAY_MS = 500

# How many MessageSpec objects the worker accumulates before emitting
# a single ``chunk_ready`` signal to the main thread.  Larger chunks
# reduce per-emit overhead but increase latency-to-first-paint.
_RESTORE_CHUNK_SIZE = 20

# Default cap on how many messages the async restore path materialises
# into real widgets. Only the most recent N messages are built; older
# messages keep their lightweight placeholder so the scrollbar geometry
# stays correct. This caps the main-thread cost of opening a plugin with
# a very large session (a 677-message session measured 6.3s of Qt widget
# creation — inherent cost, not a bug). Callers can override via
# ``RestoreWorker(max_rendered=...)``; ``None`` disables the cap (legacy).
_RESTORE_DEFAULT_MAX_RENDERED = 100

# RestoreWorker payload kinds (strings, kept short to avoid queue overhead).
# The drain callback in ``ChatView._drain_restore_queue`` dispatches on these.
_RESTORE_KIND_CHUNK = "chunk"
_RESTORE_KIND_FINISHED = "finished"

# Drain cadence for ``ChatView._drain_restore_queue``.  Mirrors the
# agent-event poll (50ms) and the history executor poll — small enough to
# keep first-paint latency tight, large enough that the timer is not a
# busy-loop on an idle session.
_RESTORE_POLL_INTERVAL_MS = 50

# How many queue items the drain processes per tick.  Bounds the per-tick
# main-thread cost so a flood of late chunks cannot freeze the UI.
_RESTORE_DRAIN_BATCH = 30

# Collapse consecutive tool runs once they reach this many calls.
# A single tool call is shown inline with its name visible;
# only 2+ consecutive calls get grouped into a collapsible widget.
_TOOL_GROUP_MIN_CALLS = 2

def _is_hidden_system_user_message(content: str) -> bool:
    """Internal system hints are persisted as user messages but not shown in UI."""
    if not content:
        return False
    return content.lstrip().startswith("[SYSTEM]")


# ---------------------------------------------------------------------------
# Async restore: spec types, placeholder, worker
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """Pre-serialized tool call/result pair for async restore.

    Frozen so it can safely cross thread boundaries without locks.
    """

    id: str
    name: str
    arguments_json: str  # pre-serialized via json.dumps
    # Estimated pixel height of the rendered ToolCallWidget.
    estimated_height: int = 80
    # ToolResult side: optional result content / error flag.
    result_content: str = ""
    result_is_error: bool = False


@dataclass(frozen=True)
class MessageSpec:
    """Pre-built description of a single chat message.

    Built off the UI thread by :class:`RestoreWorker`.  Carries everything
    needed to instantiate the real Qt widget on the main thread without
    further I/O or computation.
    """

    # Stable identifier ΓÇö set once in the worker so the main thread can
    # correlate emitted chunks with their original position in the list.
    msg_id: str
    role: str  # one of Role.{USER,ASSISTANT,TOOL}
    # USER / ASSISTANT raw text (assistant text is the *markdown* source;
    # HTML is rendered on the main thread inside set_text_deferred).
    content: str = ""
    # Pre-rendered HTML for ASSISTANT content, produced in the worker thread
    # by ``md_to_html`` so the main thread can ``setText`` directly without
    # paying the markdown + pygments cost synchronously at restore time.
    # Empty for USER messages (no markdown) and legacy specs.
    content_html: str = ""
    # USER_QUESTION payload
    question_options: tuple[str, ...] = ()
    # TOOL side: list of ToolSpec (call + result)
    tool_specs: tuple[ToolSpec, ...] = ()
    # Provider reasoning trace (GLM thinking, OpenAI o-series, etc.).
    # When non-empty, the renderer creates a _ThinkingBlock from this
    # field directly, bypassing legacy ``_split_thinking`` parsing.
    reasoning_content: str = ""
    # Estimated pixel height of the widget once it is laid out at the
    # current viewport width.  Used for MessagePlaceholder sizing.
    estimated_height: int = 60


@dataclass
class _RenderedChunk:
    """A batch of MessageSpecs delivered from worker to main thread.

    Wrapped in a mutable dataclass so the queued signal can carry an
    object (signals carrying ``tuple`` work but are harder to extend).
    """

    specs: list[MessageSpec] = field(default_factory=list)


def _estimate_assistant_height(text: str) -> int:
    """Cheap line-count based height estimate for an assistant message.

    Used to size MessagePlaceholder widgets before the real
    AssistantMessageWidget is constructed.  18px per text line + 32px
    for header/footer chrome.  Falls back to 32px for empty content.
    """
    if not text:
        return 32
    lines = text.count("\n") + 1
    # ~18px per wrapped line, capped to a sensible minimum
    return max(64, min(800, 32 + lines * 18))


def _estimate_tool_height(result_content: str = "") -> int:
    """Cheap height estimate for a single tool call + result.

    Takes the raw ``result_content`` string (not a full ``ToolSpec``)
    so callers do not have to allocate a throw-away dataclass just to
    measure the rendered height. The base 80 px covers the widget's
    header/result-box chrome; the per-line term accounts for the
    wrapped result text (capped at 400 px to keep the placeholder
    height bounded for very large results).
    """
    result_lines = (result_content.count("\n") + 1) if result_content else 0
    extra = min(400, result_lines * 14)
    return 80 + extra


def _estimate_user_height(text: str) -> int:
    """Cheap height estimate for a user message."""
    if not text:
        return 40
    lines = text.count("\n") + 1
    # ~16px per wrapped line
    return max(40, min(600, 32 + lines * 16))


class MessagePlaceholder(QFrame):
    """Lightweight sized spacer used during async restore.

    Holds the vertical space for an upcoming real widget so the
    QScrollArea can compute correct scrollbar geometry.  Replaced by
    the real widget via :func:`replace_with` when the chunk is
    rendered.  Holds no real content ΓÇö just a fixed ``minimumHeight``.
    """

    def __init__(self, estimated_height: int, msg_id: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._msg_id = msg_id
        self.setObjectName("chat_msg_placeholder")
        # QFrame with no frame looks invisible ΓÇö exactly what we want.
        # We only need its size contribution to the layout.
        self.setMinimumHeight(max(16, int(estimated_height)))
        self.setMaximumHeight(self.minimumHeight())

    @property
    def msg_id(self) -> str:
        return self._msg_id


class RestoreWorker(QThread):
    """Background thread that builds ``MessageSpec`` objects off the UI thread.

    ``run()`` walks the input ``list[Message]`` and pushes
    ``(kind, payload)`` tuples onto ``self.queue``:

      * ``("chunk", _RenderedChunk)`` — every :data:`_RESTORE_CHUNK_SIZE`
        messages, and again for any remainder at the end of the loop.
      * ``("finished", None)`` — emitted exactly once when ``run()``
        returns normally, so the main-thread drain can finalise the
        restore (reapply viewport, scroll to bottom, etc.).

    The :class:`ChatView` polls ``self.queue`` from a ``QTimer`` and
    dispatches each tuple on the main thread.  **No Qt signals** cross
    the thread boundary — see ``AGENTS.md`` §1 (Shiboken UAF on
    Python ≥ 3.11).  The queue + QTimer shape mirrors the
    ``RikuganPanelCore._drain_history_results`` history executor.

    Cancellation: :meth:`cancel` sets a stop flag the worker checks
    at the top of each iteration.  A cancelled worker does NOT
    enqueue ``"finished"`` — the main thread's safety-net cleanup
    in ``_on_worker_finished`` handles the in-flight restore state
    in that case (mirroring the legacy ``finished_ok`` semantics).
    """

    def __init__(
        self,
        messages: list[Message],
        parent=None,
        max_rendered: int | None = _RESTORE_DEFAULT_MAX_RENDERED,
    ):
        super().__init__(parent)
        self._messages = messages
        self._stop_requested = False
        # When set, only messages at index >= (n - max_rendered) are
        # materialised into real widgets. Earlier messages keep their
        # placeholder (see ``restore_from_messages_async``) so the
        # scrollbar still spans the full conversation height. ``None``
        # disables the cap (legacy / opt-in callers).
        self._max_rendered = max_rendered
        # Thread-safe handoff to the main-thread drain.  Unbounded
        # (``maxsize=0``) — the worker is rate-limited by the
        # chunk-size boundary and the main-thread drain is rate-limited
        # by the QTimer poll interval.  A bounded queue would block
        # ``put`` and stall the worker for no real benefit.
        self.queue: queue.Queue = queue.Queue()

    def cancel(self) -> None:
        """Request the worker to stop at the next safe point."""
        self._stop_requested = True

    def run(self) -> None:
        chunk = _RenderedChunk()
        i = 0
        n = len(self._messages)
        # When ``max_rendered`` is set, skip building specs for messages
        # older than the most-recent window. Those messages keep the
        # placeholder inserted by ``restore_from_messages_async`` so the
        # scrollbar still spans the full conversation; they simply never
        # get a real widget (cheap spacer, not a full AssistantMessageWidget
        # + ToolCallWidgets). ASSISTANT+TOOL pairs are consumed together,
        # so the window is computed by message index — a TOOL paired with
        # a kept ASSISTANT is always built alongside its ASSISTANT.
        # ``max_rendered=None`` disables the cap (legacy / opt-in callers).
        cutoff = 0
        if self._max_rendered is not None and self._max_rendered >= 0:
            cutoff = max(0, n - self._max_rendered)
        try:
            while i < n:
                if self._stop_requested:
                    return
                if i < cutoff:
                    # Skip: advance past this message and any TOOL it would
                    # consume. Pairing rule mirrors _build_spec so the cutoff
                    # stays aligned with render units.
                    consumed = 0
                    msg = self._messages[i]
                    if (
                        msg.role == Role.ASSISTANT
                        and msg.tool_calls
                        and i + 1 < n
                        and self._messages[i + 1].role == Role.TOOL
                    ):
                        consumed = 1
                    i += 1 + consumed
                    continue
                msg = self._messages[i]
                next_msg = self._messages[i + 1] if i + 1 < n else None
                spec, consumed = self._build_spec(msg, i, next_msg)
                if spec is not None:
                    chunk.specs.append(spec)
                    if len(chunk.specs) >= _RESTORE_CHUNK_SIZE:
                        self.queue.put((_RESTORE_KIND_CHUNK, chunk))
                        chunk = _RenderedChunk()
                # Advance past any consumed follow-up message.  For
                # ASSISTANT+TOOL pairs the TOOL is consumed so its
                # placeholder (which ``restore_from_messages_async`` also
                # skips) is never expected by the main thread.
                i += 1 + consumed
            # Flush remainder (only if not cancelled — otherwise we don't
            # want a half-built chunk racing the cancel signal into the
            # main-thread drain).
            if chunk.specs and not self._stop_requested:
                self.queue.put((_RESTORE_KIND_CHUNK, chunk))
        finally:
            # Always push the finished sentinel on normal completion so
            # the main-thread drain can finalise.  A cancelled worker
            # reaches ``return`` above before this point and skips the
            # sentinel — ``_on_worker_finished`` is the cleanup path.
            if not self._stop_requested:
                self.queue.put((_RESTORE_KIND_FINISHED, None))



    @staticmethod
    def _build_spec(
        msg: Message,
        idx: int,
        next_msg: Message | None,
    ) -> tuple[MessageSpec | None, int]:
        """Convert one Message into a MessageSpec (or None to skip).

        Returns ``(spec, consumed)`` where ``consumed`` is the number
        of *additional* messages consumed by this spec — currently 0
        or 1, used to fold a trailing TOOL result message into the
        preceding ASSISTANT tool-call spec so the renderer can build a
        single ``MessageSpec`` per logical "assistant turn" unit.
        """
        # msg_id is derived here and only here — used for correlation
        # between emitted chunks and their original position.
        msg_id = msg.id or f"restore_{idx}"

        if msg.role == Role.USER:
            if _is_hidden_system_user_message(msg.content):
                return None, 0
            return (
                MessageSpec(
                    msg_id=msg_id,
                    role=Role.USER.value,
                    content=msg.content,
                    estimated_height=_estimate_user_height(msg.content),
                ),
                0,
            )

        if msg.role == Role.ASSISTANT:
            content = msg.content or ""
            # Pair tool_calls with results from the *immediately
            # following* TOOL message (normal persisted shape).  A
            # normal TOOL message carries only ``tool_results`` —
            # ``tool_calls`` belongs to the preceding ASSISTANT.
            tool_specs = RestoreWorker._collect_tool_specs(msg, next_msg)
            estimated = _estimate_assistant_height(content) + sum(s.estimated_height for s in tool_specs)
            # Tolerate ``tool_calls`` being ``None`` on a malformed
            # persisted message (older sessions, manual edits, partial
            # recovery from a corrupt IDB).  ``msg.tool_calls or []``
            # makes the truthiness check robust against ``None``.
            tool_calls = msg.tool_calls or []
            consumed = 0
            if tool_calls and next_msg is not None and next_msg.role == Role.TOOL:
                consumed = 1
            # Pre-render the visible portion to HTML here (in the worker
            # thread) so the main thread only does a cheap setText at build
            # time. This moves the markdown + pygments cost off the UI
            # thread — the fix for the 6.6s main-thread freeze measured on
            # a 677-message restore. ``content_html`` carries the rendered
            # HTML; ``content`` stays as the raw source for set_text_deferred
            # callers that did not opt in (kept for the sync restore path).
            #
            # When ``reasoning_content`` is present (GLM thinking, OpenAI
            # o-series), it is carried directly on the spec so the renderer
            # bypasses legacy ``_split_thinking`` parsing. The visible
            # ``content`` is used as-is (no <think> stripping needed).
            reasoning_content = msg.reasoning_content or ""
            visible_text = ""
            if content:
                if reasoning_content:
                    # New field wins — content is already the visible text.
                    visible_text = content
                else:
                    # Legacy: strip <think> tags from content.
                    _thinking, visible_text = _split_thinking(content)
            render_source = visible_text if visible_text else content
            content_html = md_to_html(render_source) if render_source else ""
            return (
                MessageSpec(
                    msg_id=msg_id,
                    role=Role.ASSISTANT.value,
                    content=content,
                    content_html=content_html,
                    tool_specs=tuple(tool_specs),
                    reasoning_content=reasoning_content,
                    estimated_height=estimated or _estimate_assistant_height(content),
                ),
                consumed,
            )

        if msg.role == Role.TOOL:
            # Orphan TOOL message (no preceding assistant call).  Some
            # persisted transcripts include the ``tool_calls`` on the
            # TOOL message itself (older versions paired the call and
            # result on a single message); if so, use the call's
            # arguments for the rendered args panel.  Otherwise fall
            # back to an empty JSON object — this branch only fires
            # if the persisted transcript has a TOOL without a
            # matching ASSISTANT (defensive).
            results_by_id: dict[str, ToolResult] = {r.tool_call_id: r for r in (msg.tool_results or [])}
            tool_specs: list[ToolSpec] = []
            # Pair any ``tool_calls`` with matching ``tool_results``.
            # Iterate over whichever side is present so we always
            # produce a spec for every result, even if the call was
            # dropped from the persisted transcript.
            seen_ids: set[str] = set()
            for tc in msg.tool_calls or []:
                tr = results_by_id.get(tc.id)
                try:
                    args_json = json.dumps(
                        tc.arguments or {},
                        ensure_ascii=False,
                        indent=2,
                    )
                except (TypeError, ValueError):
                    args_json = "{}"
                result_content = tr.content if tr else ""
                tool_specs.append(
                    ToolSpec(
                        id=tc.id,
                        name=tc.name,
                        arguments_json=args_json,
                        estimated_height=_estimate_tool_height(result_content),
                        result_content=result_content,
                        result_is_error=tr.is_error if tr else False,
                    )
                )
                seen_ids.add(tc.id)
            # Results whose call is not on this message (orphan
            # results): produce a placeholder spec with empty args.
            for tr_id, tr in results_by_id.items():
                if tr_id in seen_ids:
                    continue
                tool_specs.append(
                    ToolSpec(
                        id=tr.tool_call_id,
                        name=tr.name,
                        arguments_json="{}",
                        estimated_height=_estimate_tool_height(tr.content),
                        result_content=tr.content,
                        result_is_error=tr.is_error,
                    )
                )
            return (
                MessageSpec(
                    msg_id=msg_id,
                    role=Role.TOOL.value,
                    tool_specs=tuple(tool_specs),
                    estimated_height=sum(s.estimated_height for s in tool_specs) or 60,
                ),
                0,
            )

        # SYSTEM / unknown — skip
        return None, 0

    @staticmethod
    def _collect_tool_specs(msg: Message, next_msg: Message | None) -> list[ToolSpec]:
        """Build ToolSpec objects pairing ``msg.tool_calls`` with results.

        Results are looked up on the immediately-following TOOL
        message (``next_msg``).  A tool call with no matching result
        still gets a ``ToolSpec`` — with an empty result — so the
        renderer produces a widget for the call regardless of whether
        the transcript was complete.
        """
        results_by_id: dict[str, ToolResult] = {}
        if next_msg is not None and next_msg.role == Role.TOOL:
            # ``next_msg.tool_results or []`` — tolerate malformed
            # persisted TOOL messages whose ``tool_results`` is
            # ``None`` instead of an empty list.
            results_by_id = {r.tool_call_id: r for r in (next_msg.tool_results or [])}
        specs: list[ToolSpec] = []
        # ``msg.tool_calls or []`` — tolerate ``None`` on malformed
        # ASSISTANT messages so the loop body is unreachable when the
        # field is missing.
        for tc in msg.tool_calls or []:
            tr = results_by_id.get(tc.id)
            try:
                # ``indent=2`` mirrors the sync restore path so the
                # rendered args panel looks identical whether the
                # transcript was restored sync or async.
                args_json = json.dumps(tc.arguments or {}, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                args_json = "{}"
            # Build the height estimate using the same shape the
            # renderer will see (i.e. include the result content).
            # Pass the raw result string — ``_estimate_tool_height`` only
            # needs the result content, so we avoid allocating a
            # throw-away ``ToolSpec`` here.
            result_content = tr.content if tr else ""
            specs.append(
                ToolSpec(
                    id=tc.id,
                    name=tc.name,
                    arguments_json=args_json,
                    estimated_height=_estimate_tool_height(result_content),
                    result_content=result_content,
                    result_is_error=tr.is_error if tr else False,
                )
            )
        return specs


class ChatView(QScrollArea):
    """Scrollable chat area that renders TurnEvents into widgets."""

    tool_approval_submitted = Signal(str, str)  # (tool_call_id, "allow"/"deny")
    user_answer_submitted = Signal(str)  # chosen option / typed answer
    orchestra_approval_decided = Signal(str, str)  # (tool_call_id, "approve"/"deny")

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("chat_scroll")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._container = QWidget()
        self._container.setObjectName("chat_container")
        # Prevent the container from requesting more width than the viewport;
        # this is critical for word-wrap to work inside a QScrollArea.
        self._container.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.setSpacing(4)
        self._layout.addStretch()
        self.setWidget(self._container)

        # Set to True during ``restore_from_messages`` so the per-widget
        # ``resizeEvent`` cascade is suppressed. See that method.
        self._in_restore: bool = False

        # Track current assistant widget for streaming
        self._current_assistant: AssistantMessageWidget | None = None
        self._message_thinking: _ThinkingBlock | None = None  # For message content thinking
        self._tool_widgets: dict[str, ToolCallWidget | ExecutePythonWidget] = {}
        self._thinking: ThinkingWidget | None = None
        self._thinking_shown_at: float = 0.0
        self._plan_view: PlanView | None = None

        # Consecutive tool run state (collapsed when threshold is reached)
        self._tool_run_ids: list[str] = []
        self._tool_run_names: list[str] = []
        self._tool_run_widgets: list[ToolCallWidget] = []
        # Active collapsible group for the current run
        self._tool_group: ToolGroupWidget | None = None
        # Map tool_call_id -> group it belongs to (for result routing/status)
        self._group_map: dict[str, ToolGroupWidget] = {}

        # Async restore state.  ``_restore_generation`` is bumped on
        # every new restore (and on cancel) so any in-flight chunks
        # from a superseded worker are ignored.
        self._restore_generation: int = 0
        self._restore_worker: RestoreWorker | None = None
        self._placeholders: dict[str, MessagePlaceholder] = {}
        # Drain timer for the active ``RestoreWorker`` queue — replaces
        # the cross-thread ``chunk_ready`` / ``finished_ok`` Qt signals
        # that triggered the Shiboken UAF (see ``AGENTS.md`` §1).  Mirrors
        # ``RikuganPanelCore._history_poll_timer`` for the history executor.
        self._restore_poll_timer: QTimer | None = None

        # Member timer for scroll-to-bottom — coalesce at 80ms to reduce
        # layout thrashing during rapid streaming.
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.setInterval(80)
        self._scroll_timer.timeout.connect(self._do_scroll)

        # Timer for minimum thinking display duration (500ms)
        self._thinking_hide_timer = QTimer(self)
        self._thinking_hide_timer.setSingleShot(True)
        self._thinking_hide_timer.timeout.connect(self._force_hide_thinking)

        # Batched session restore state ΓÇö REMOVED.  The async restore
        # path is the only chunked path; ``_pending_restore`` and
        # ``_restore_chunk_size`` were leftovers from an earlier
        # chunked-batch design that was superseded by
        # ``restore_from_messages_async`` / ``RestoreWorker``.

        # Paginated restore state ΓÇö restored histories are rendered as
        # page windows instead of one huge transcript. See
        # _build_restore_units / _build_restore_pages / _render_restore_window.
        self._restore_messages: list[Message] = []
        self._restore_units: list[tuple[int, int]] = []
        self._restore_pages: list[tuple[int, int]] = []
        # Cap on how many recent messages the async restore path
        # materialises (see ``RestoreWorker.max_rendered``). Grows when
        # the user clicks "Load older" so they can page back into the
        # history without paying the full restore cost up front.
        self._restore_max_rendered: int = _RESTORE_DEFAULT_MAX_RENDERED
        # "Load older" nav button shown at the top of the chat when the
        # session has more messages than the current cap. ``None`` when
        # no button is currently in the layout.
        self._load_older_btn: QPushButton | None = None
        self._restore_first_page: int = 0
        self._restore_last_page: int = 0
        self._restore_paged: bool = False
        self._restore_rendered: bool = False
        self._restore_page_size_units: int = 40
        self._restore_max_window_pages: int = 5
        self._restore_default_window_pages: int = 1

        # Live-tail safety: once the user/app appends a live message into a
        # paginated restore, history navigation is disabled so a stray click
        # on "Load older/newer/latest" can no longer wipe the live tail by
        # triggering _render_restore_window().  See
        # _ensure_latest_restore_window_for_live_append and the
        # _go_restore_*/_render_restore_window guards.
        self._restore_live_tail_started: bool = False

        # Track currently-rendered history nav frames so refresh_inline_styles
        # can re-apply the theme palette to them after a theme switch.
        self._nav_widgets: list[QFrame] = []

        # Thinking block buffering for proper ordering
        self._think_buffer: str = ""  # Accumulated text while waiting for <think> to close
        self._waiting_think_close: bool = False  # True when we have <think> but not yet

    def add_user_message(self, text: str) -> None:
        self._begin_live_tail_append()
        self._insert_user_message_widget(text)

    def add_error_message(self, text: str) -> None:
        self._begin_live_tail_append()
        self._insert_widget(ErrorMessageWidget(text))
        self._scroll_to_bottom()

    def add_queued_message(self, text: str) -> None:
        self._begin_live_tail_append()
        self._insert_widget(QueuedMessageWidget(text))
        self._scroll_to_bottom()

    def _begin_live_tail_append(self) -> None:
        """Prepare a paginated restore for a live-widget append.

        Order matters here:

        1. Jump the page window back to the final page **with the nav
           strip suppressed** so the new live widget appears beneath
           the latest restored page.  This call happens *before* the
           live-tail flag is set, so the (re-)render still produces
           the visible restored content.
        2. Lock the live tail by setting ``_restore_live_tail_started``.
           From this point on, ``_render_restore_window`` early-returns
           and the ``_go_restore_*`` nav callbacks are no-ops, so a
           stray click can no longer wipe the live tail.
        3. Remove any nav frames that may still be in the layout from
           the previous render.  Live widgets (already inserted before
           this call) are intentionally left untouched ΓÇö this method
           only touches ``self._nav_widgets``.
        """
        if not self._restore_paged:
            return
        # 1. Jump to latest page WITHOUT nav so the visible restored
        #    content is the latest page but no nav strip is added.
        self._ensure_latest_restore_window_for_live_append(show_nav=False)
        # 2. Lock the live tail ΓÇö subsequent _render_restore_window
        #    calls become a no-op, and _go_restore_* callbacks no-op.
        self._restore_live_tail_started = True
        # 3. Strip any nav frames left from a previous render so the
        #    UI matches the "no nav" state implied by the live-tail flag.
        self._remove_restore_nav_widgets()

    def _remove_restore_nav_widgets(self) -> None:
        """Remove rendered history nav frames only; never clear live widgets.

        The removal is immediate and thorough:

        * ``hide()`` so the widget vanishes from the viewport in this
          event-loop tick (rather than waiting for ``deleteLater()``).
        * ``removeWidget()`` detaches it from the layout.
        * ``setParent(None)`` detaches it from the widget tree so a
          subsequent ``deleteLater()`` cannot accidentally walk back
          into the live-tail widgets via the parent chain.
        * ``deleteLater()`` schedules the C++ object for deletion.
        * ``self._nav_widgets`` is cleared so ``refresh_inline_styles``
          does not iterate over stale references.
        """
        for frame in list(self._nav_widgets):
            try:
                frame.hide()
                self._layout.removeWidget(frame)
                frame.setParent(None)
                frame.deleteLater()
            except RuntimeError:
                # Widget may already be deleted; ignore.
                pass
        self._nav_widgets = []

    def _insert_user_message_widget(self, text: str) -> None:
        """Insert a UserMessageWidget without touching restore pagination.

        Used by both the live ``add_user_message`` path and the restore
        renderer.  Live callers must run
        ``_ensure_latest_restore_window_for_live_append`` first.
        """
        widget = UserMessageWidget(text)
        self._insert_widget(widget)
        self._current_assistant = None

    def remove_queued_messages(self) -> None:
        """Remove all [queued] message widgets (e.g. on cancel)."""
        for i in reversed(range(self._layout.count())):
            item = self._layout.itemAt(i)
            widget = item.widget() if item else None
            if isinstance(widget, QueuedMessageWidget):
                self._layout.removeWidget(widget)
                widget.deleteLater()

    def pop_first_queued_message(self) -> None:
        """Remove the first [queued] widget (when it gets submitted)."""
        for i in range(self._layout.count()):
            item = self._layout.itemAt(i)
            widget = item.widget() if item else None
            if isinstance(widget, QueuedMessageWidget):
                self._layout.removeWidget(widget)
                widget.deleteLater()
                return

    def _show_thinking(self) -> None:
        if self._thinking is not None:
            return
        self._thinking = ThinkingWidget()
        self._thinking_shown_at = time.monotonic()
        self._insert_widget(self._thinking)
        self._scroll_to_bottom()

    def _hide_thinking(self) -> None:
        if self._thinking is None:
            return
        elapsed_ms = (time.monotonic() - self._thinking_shown_at) * 1000
        if elapsed_ms < _THINKING_MIN_DISPLAY_MS:
            remaining = int(_THINKING_MIN_DISPLAY_MS - elapsed_ms)
            self._thinking_hide_timer.start(remaining)
            return
        self._force_hide_thinking()

    def _force_hide_thinking(self) -> None:
        if self._thinking is None:
            return
        self._thinking.stop()
        self._layout.removeWidget(self._thinking)
        self._thinking.deleteLater()
        self._thinking = None

    def _reset_tool_run(self) -> None:
        """End the current consecutive tool run (state only)."""
        self._tool_group = None
        self._tool_run_ids.clear()
        self._tool_run_names.clear()
        self._tool_run_widgets.clear()

    def _register_tool_widget(self, tool_name: str, tool_id: str, widget: ToolCallWidget) -> None:
        """Attach a new tool widget to the current run, collapsing at threshold."""
        self._tool_run_ids.append(tool_id)
        self._tool_run_names.append(tool_name)
        self._tool_run_widgets.append(widget)

        run_len = len(self._tool_run_widgets)

        # Below threshold: show tool calls directly.
        if self._tool_group is None and run_len < _TOOL_GROUP_MIN_CALLS:
            self._insert_widget(widget)
            return

        # Threshold reached: move entire run into a new collapsible group.
        if self._tool_group is None and run_len == _TOOL_GROUP_MIN_CALLS:
            self._tool_group = ToolGroupWidget()
            self._insert_widget(self._tool_group)

            for idx, run_widget in enumerate(self._tool_run_widgets):
                self._layout.removeWidget(run_widget)
                run_widget.hide_preview()

                run_tool_id = self._tool_run_ids[idx]
                run_tool_name = self._tool_run_names[idx]
                self._tool_group.add_widget(run_widget, run_tool_name)
                self._group_map[run_tool_id] = self._tool_group
            return

        # Already collapsed: add new call directly to existing group.
        widget.hide_preview()
        if self._tool_group is not None:
            self._tool_group.add_widget(widget, tool_name)
            self._group_map[tool_id] = self._tool_group

    def handle_event(self, event: TurnEvent) -> None:
        """Process a TurnEvent and update the UI accordingly."""
        # Live turn events must not be appended under an older paged
        # window, and any history nav must be removed before we start
        # inserting live widgets so a stray click cannot wipe the tail.
        # Diagnostic: every TEXT_DONE reaching the chat panel is
        # logged with length only so we can confirm the ``/report``
        # draft event actually arrived without writing the body
        # (which may carry sensitive analysis) to the persistent
        # debug log. ``container_width`` is the chat's actual visible
        # pixel budget — a value << 400 suggests the chat docked
        # narrower than the Markdown wrapper expects and may clip
        # body content for that reason.
        if event.type == TurnEventType.TEXT_DONE:
            try:
                cw = self._container.width() if self._container is not None else 0
            except RuntimeError:
                cw = -1
            log_debug(
                f"CHAT_TEXT_DONE: text_len={len(event.text or '')} "
                f"head={(event.text or '')[:60] if event.text else ''!r} "
                f"container_width={cw}"
            )
        self._begin_live_tail_append()
        etype = event.type
        if etype in (TurnEventType.TEXT_DELTA, TurnEventType.TEXT_DONE):
            self._handle_text_event(event)
        elif etype in (
            TurnEventType.TOOL_CALL_START,
            TurnEventType.TOOL_CALL_ARGS_DELTA,
            TurnEventType.TOOL_CALL_DONE,
            TurnEventType.TOOL_RESULT,
            TurnEventType.TOOL_APPROVAL_REQUEST,
        ):
            self._handle_tool_event(event)
        elif etype in (
            TurnEventType.TURN_START,
            TurnEventType.TURN_END,
            TurnEventType.CANCELLED,
        ):
            self._handle_lifecycle_event(event)
        elif etype in (
            TurnEventType.PLAN_GENERATED,
            TurnEventType.PLAN_STEP_START,
            TurnEventType.PLAN_STEP_DONE,
        ):
            self._handle_plan_event(event)
        elif etype in (
            TurnEventType.EXPLORATION_PHASE_CHANGE,
            TurnEventType.EXPLORATION_FINDING,
        ):
            self._handle_exploration_event(event)
        elif etype in (
            TurnEventType.RESEARCH_NOTE_SAVED,
            TurnEventType.RESEARCH_NOTE_REVIEWED,
        ):
            self._handle_research_event(event)
        elif etype in (
            TurnEventType.USER_QUESTION,
            TurnEventType.SAVE_APPROVAL_REQUEST,
        ):
            self._handle_question_event(event)
        elif etype in (
            TurnEventType.SUBAGENT_SPAWNED,
            TurnEventType.SUBAGENT_COMPLETED,
            TurnEventType.SUBAGENT_FAILED,
        ):
            self._handle_subagent_event(event)
        elif etype == TurnEventType.KNOWLEDGE_RETRIEVED:
            self._handle_knowledge_event(event)
        elif etype == TurnEventType.ERROR:
            self._hide_thinking()
            self._reset_tool_run()
            self._insert_widget(ErrorMessageWidget(event.error or "Unknown error"))
            self._scroll_to_bottom()
        elif etype == TurnEventType.DOCS_GATE_STATUS:
            # UI-only signal: route into the matching tool widget's
            # status line. Routed here (not in ``_handle_tool_event``)
            # because it never carries args/result/approval semantics.
            self._handle_docs_gate_status(event)
        elif etype == TurnEventType.REASONING_DELTA:
            self._handle_reasoning_delta(event)
        elif etype == TurnEventType.RECOVERY_START:
            self._handle_recovery_start(event)
        elif etype == TurnEventType.TOOL_CALL_DISCARDED:
            self._handle_tool_call_discarded(event)

    def _finalize_reasoning_block(self) -> None:
        """Switch the transient reasoning block from 'Thinking...' to 'Thinking'.

        Called when visible text begins (TEXT_DELTA/TEXT_DONE) or at
        TURN_END so the spinner indicator stops on the reasoning block.
        """
        if self._message_thinking is not None:
            self._message_thinking.set_thinking(
                self._message_thinking._source_text,
                in_progress=False,
            )

    def _handle_text_event(self, event: TurnEvent) -> None:
        self._hide_thinking()
        self._reset_tool_run()
        # Finalize any in-progress reasoning block now that visible text
        # has started arriving.
        self._finalize_reasoning_block()
        if event.type == TurnEventType.TEXT_DELTA:
            text = event.text

            if self._waiting_think_close:
                # Buffer text until </think> arrives
                self._think_buffer += text
                if "</think>" in text:
                    # Complete — parse accumulated buffer.  Use the
                    # unstripped visible helper so boundary whitespace
                    # between the think close and the post-think text
                    # is preserved across chunks (otherwise
                    # e.g. "</think>\n" + " Hello" loses the
                    # implicit space).  See _extract_visible_text.
                    thinking_text = _extract_thinking_text(self._think_buffer)
                    visible_text = _extract_visible_text(self._think_buffer)
                    self._waiting_think_close = False
                    if thinking_text:
                        if self._message_thinking is None:
                            self._message_thinking = _ThinkingBlock()
                            self._insert_widget(self._message_thinking)
                        self._message_thinking.set_thinking(thinking_text, in_progress=False)
                    if visible_text:
                        if self._current_assistant is None:
                            self._current_assistant = AssistantMessageWidget()
                            self._insert_widget(self._current_assistant)
                        self._current_assistant.append_text(visible_text)
            elif "<think>" in text and "</think>" not in text:
                # Opening <think> without closing — start buffering
                self._waiting_think_close = True
                self._think_buffer = text
                # Use the unstripped visible helper for the same
                # boundary-space reason as the buffer-close branch
                # above.  Thinking content extraction stays unchanged
                # (already stripped, which is what the thinking panel
                # wants).
                thinking_text = _extract_thinking_text(text)
                visible_text = _extract_visible_text(text)
                if thinking_text:
                    if self._message_thinking is None:
                        self._message_thinking = _ThinkingBlock()
                        self._insert_widget(self._message_thinking)
                    self._message_thinking.set_thinking(thinking_text, in_progress=True)
                if visible_text:
                    if self._current_assistant is None:
                        self._current_assistant = AssistantMessageWidget()
                        self._insert_widget(self._current_assistant)
                    self._current_assistant.append_text(visible_text)
            else:
                # Normal text (no thinking, or complete <think>...</think>
                # in one delta).  Use the unstripped visible helper —
                # this is the most common streaming case and the
                # boundary-space bug manifested here ("I am " chunk
                # followed by "thinking" chunk produced "I amthinking"
                # in the chat because each chunk's whitespace was
                # dropped at extraction).
                thinking_text = _extract_thinking_text(text)
                visible_text = _extract_visible_text(text)
                if thinking_text:
                    if self._message_thinking is None:
                        self._message_thinking = _ThinkingBlock()
                        self._insert_widget(self._message_thinking)
                    self._message_thinking.set_thinking(thinking_text, in_progress=False)
                if visible_text:
                    if self._current_assistant is None:
                        self._current_assistant = AssistantMessageWidget()
                        self._insert_widget(self._current_assistant)
                    self._current_assistant.append_text(visible_text)

            self._scroll_to_bottom()
        else:  # TEXT_DONE
            thinking_text = _extract_thinking_text(event.text)
            visible_text = _extract_visible_text(event.text)
            has_visible = bool(visible_text)
            has_thinking = bool(thinking_text)

            if self._current_assistant is not None:
                # Final render - extract and handle thinking if any.
                # The visible is taken from the unstripped helper so
                # the boundary spaces preserved during streaming are
                # not clobbered here.  The thinking extraction stays
                # the same (always stripped).
                if has_thinking:
                    # Finalize thinking block
                    if self._message_thinking is None:
                        self._message_thinking = _ThinkingBlock()
                        self._insert_widget(self._message_thinking)
                    self._message_thinking.set_thinking(thinking_text, in_progress=False)

                if has_visible:
                    self._current_assistant.set_text(visible_text)
                elif not has_thinking:
                    # No visible text at all, just render normally
                    self._current_assistant.set_text(event.text)
            elif has_visible or has_thinking:
                # Standalone TEXT_DONE with no prior streaming — typical
                # for direct commands like `/goal`, `/memory`, `/mcp`,
                # `/doctor`, etc. that emit a single confirmation event.
                # Without this branch the command acknowledgement would
                # be silently dropped because _current_assistant is
                # only set by TEXT_DELTA handlers.
                if has_thinking:
                    if self._message_thinking is None:
                        self._message_thinking = _ThinkingBlock()
                        self._insert_widget(self._message_thinking)
                    self._message_thinking.set_thinking(thinking_text, in_progress=False)
                if has_visible:
                    self._current_assistant = AssistantMessageWidget()
                    self._insert_widget(self._current_assistant)
                    self._current_assistant.set_text(visible_text)
                # If only thinking is present, skip rendering — we
                # don't want a blank bubble. The user will see the
                # thinking panel alone.
            # Reset per-turn reasoning state so the next turn starts
            # fresh.  Without this, _message_thinking retains the
            # previous turn's _ThinkingBlock and subsequent
            # REASONING_DELTA events append to the stale widget
            # instead of creating a new one.
            self._current_assistant = None
            self._message_thinking = None
            self._think_buffer = ""
            self._waiting_think_close = False
            self._scroll_to_bottom()

    def _handle_tool_event(self, event: TurnEvent) -> None:
        etype = event.type
        if etype == TurnEventType.TOOL_CALL_START:
            self._hide_thinking()
            # ``execute_python`` uses the unified lifecycle widget that
            # also renders the docs-review status, the approval buttons,
            # and the result. All other tools keep the compact
            # ``ToolCallWidget`` + separate ``ToolApprovalWidget`` flow.
            if event.tool_name == constants.EXECUTE_PYTHON_TOOL_NAME:
                tw: ToolCallWidget | ExecutePythonWidget = ExecutePythonWidget(event.tool_call_id)
            else:
                tw = ToolCallWidget(event.tool_name, event.tool_call_id)
            self._tool_widgets[event.tool_call_id] = tw
            self._register_tool_widget(event.tool_name, event.tool_call_id, tw)
            self._scroll_to_bottom()
        elif etype == TurnEventType.TOOL_CALL_ARGS_DELTA:
            existing_tw = self._tool_widgets.get(event.tool_call_id)
            if existing_tw is not None:
                existing_tw.append_args_delta(event.tool_args)
        elif etype == TurnEventType.TOOL_CALL_DONE:
            existing_tw = self._tool_widgets.get(event.tool_call_id)
            if existing_tw is not None:
                existing_tw.set_arguments(event.tool_args)
        elif etype == TurnEventType.TOOL_RESULT:
            self._reset_tool_run()
            existing_tw = self._tool_widgets.get(event.tool_call_id)
            if existing_tw is not None:
                existing_tw.set_result(event.tool_result, event.tool_is_error)
            group = self._group_map.get(event.tool_call_id)
            if group:
                group.notify_result(event.tool_is_error)
            self._scroll_to_bottom()
        elif etype == TurnEventType.TOOL_APPROVAL_REQUEST:
            self._hide_thinking()
            self._reset_tool_run()
            # If the corresponding ``ExecutePythonWidget`` already exists
            # (created during TOOL_CALL_START), drive its approval flow
            # inline rather than spawning a separate ``ToolApprovalWidget``
            # — the unified widget renders code + status + buttons + result
            # in one card, so a second approval widget would duplicate the
            # code preview and confuse the user.
            existing = self._tool_widgets.get(event.tool_call_id)
            if isinstance(existing, ExecutePythonWidget):
                # If this widget was nested inside a collapsed ToolGroupWidget
                # (because it was part of a multi-tool run), surface it now —
                # the Allow button must stay visible regardless of the group's
                # collapsed state, or the user sees a "hanging" group with no
                # clue that action is pending.
                self._promote_widget_out_of_group(event.tool_call_id)
                existing.show_approval_buttons()
                existing.approved.connect(self._on_tool_approval)
            else:
                widget = ToolApprovalWidget(
                    event.tool_call_id,
                    event.tool_name,
                    event.tool_args,
                    event.text,
                )
                widget.approved.connect(self._on_tool_approval)
                self._insert_widget(widget)
            self._scroll_to_bottom()

    def _promote_widget_out_of_group(self, tool_call_id: str) -> None:
        """Re-parent a tool widget out of its ToolGroupWidget into the main layout.

        Called when a widget nested inside a *collapsed* group needs the user's
        attention (e.g. an ``execute_python`` approval request).  Leaving the
        widget inside the collapsed group hides its Allow/Deny buttons, which
        looks like a hang — the agent loop is blocked on approval while the
        user sees nothing actionable.

        After promotion: the widget is re-parented to the container, inserted
        into the main layout before the stretch, and dropped from
        ``_group_map`` so a later ``TOOL_RESULT`` for this id routes to the
        widget directly (not the now-detached group).
        """
        group = self._group_map.pop(tool_call_id, None)
        widget = self._tool_widgets.get(tool_call_id)
        if group is None or widget is None:
            return
        # Remove from the group's body, re-parent to the container, and
        # insert into the main layout. ``setParent(container)`` detaches it
        # from the group's layout; ``_insert_widget`` re-adds it before the
        # trailing stretch. Guard ``_container`` for test paths that bypass
        # ``__init__``.
        container = getattr(self, "_container", None)
        widget.setParent(container)
        self._insert_widget(widget)

    def _handle_docs_gate_status(self, event: TurnEvent) -> None:
        """Route a docs-review gate status update to the matching widget.

        The metadata payload carries ``docs_gate_state`` (one of
        ``running`` | ``approved`` | ``blocked`` | ``failed``), an
        optional tuple of ``docs_gate_reasons`` (for ``running``), and a
        ``docs_gate_summary`` (for ``blocked``/``failed``).  The signal
        is dropped silently when no matching widget exists — the docs
        gate only ever fires for ``execute_python``, which is always
        preceded by a TOOL_CALL_START that creates the
        :class:`ExecutePythonWidget`.  An orphan event therefore means
        the call was already torn down (clear_chat / cancel / new
        restore), so dropping is the right behaviour.
        """
        md = event.metadata or {}
        tw = self._tool_widgets.get(event.tool_call_id)
        if isinstance(tw, ExecutePythonWidget):
            tw.set_docs_gate_status(
                md.get("docs_gate_state", ""),
                reasons=tuple(md.get("docs_gate_reasons", [])),
                summary=md.get("docs_gate_summary", ""),
            )
            self._scroll_to_bottom()

    def _handle_reasoning_delta(self, event: TurnEvent) -> None:
        """Append reasoning text to the transient _ThinkingBlock.

        Creates the block lazily on the first delta and appends
        subsequent deltas to the same block's source text. Never
        touches ``_current_assistant`` — reasoning must not enter
        the visible assistant buffer.
        """
        delta = event.reasoning
        if not delta:
            return
        if self._message_thinking is None:
            self._message_thinking = _ThinkingBlock()
            self._insert_widget(self._message_thinking)
        existing = self._message_thinking._source_text
        self._message_thinking.set_thinking(existing + delta, in_progress=True)
        self._scroll_to_bottom()

    def _handle_recovery_start(self, event: TurnEvent) -> None:
        """Remove the transient reasoning block and show one compact status.

        This is a hard boundary: the transient ``_ThinkingBlock`` is
        hidden, removed from the layout, and scheduled for deletion
        exactly once (reference set to ``None``). A single compact
        status label is inserted. Subsequent recovery events each
        insert their own label — no coalescing.
        """
        md = event.metadata or {}
        attempt = md.get("attempt", 0)
        reason = md.get("reason", "")
        # Properly tear down the transient reasoning block.
        if self._message_thinking is not None:
            old = self._message_thinking
            self._message_thinking = None
            old.hide()
            self._layout.removeWidget(old)
            old.deleteLater()
        # Insert one compact status label.
        status_text = f"Recovery (attempt {attempt})"
        if reason:
            status_text += f": {reason}"
        status_label = QLabel(status_text)
        status_label.setObjectName("recovery_status")
        tool_colors = get_tool_colors()
        status_label.setStyleSheet(
            f"color: {tool_colors.get('preview', '#888')}; font-style: italic; font-size: 0.9em; padding: 2px 8px;"
        )
        self._insert_widget(status_label)
        self._scroll_to_bottom()

    def _handle_tool_call_discarded(self, event: TurnEvent) -> None:
        """Mark a tool widget as discarded (terminal state, no result).

        Looks up the widget by ``tool_call_id`` and calls
        ``mark_discarded`` so its spinner stops and a neutral glyph
        replaces the spinner. If no widget exists (orphan event),
        the signal is dropped silently.
        """
        tw = self._tool_widgets.get(event.tool_call_id)
        if tw is not None and hasattr(tw, "mark_discarded"):
            reason = (event.metadata or {}).get("reason", "discarded")
            tw.mark_discarded(reason)
            self._scroll_to_bottom()

    def _handle_lifecycle_event(self, event: TurnEvent) -> None:
        etype = event.type
        if etype == TurnEventType.TURN_START:
            self._current_assistant = None
            self._reset_tool_run()
            self._group_map.clear()
            self._show_thinking()
            self._scroll_to_bottom()
        elif etype == TurnEventType.TURN_END:
            self._hide_thinking()
            self._finalize_reasoning_block()
            self._reset_tool_run()
            self._current_assistant = None
        elif etype == TurnEventType.CANCELLED:
            self._hide_thinking()
            self._reset_tool_run()
            self._insert_widget(ErrorMessageWidget("Cancelled by user"))
            self._scroll_to_bottom()

    def _handle_plan_event(self, event: TurnEvent) -> None:
        etype = event.type
        if etype == TurnEventType.PLAN_GENERATED:
            self._hide_thinking()
            self._reset_tool_run()
            self._plan_view = PlanView()
            if event.plan_steps:
                self._plan_view.set_plan(event.plan_steps)

            def _on_plan_approve(pv=self._plan_view):
                pv.set_buttons_visible(False)
                self._on_user_answer("approve")

            def _on_plan_reject(pv=self._plan_view):
                pv.set_buttons_visible(False)
                self._on_user_answer("reject")

            self._plan_view.set_approved_callback(_on_plan_approve)
            self._plan_view.set_rejected_callback(_on_plan_reject)
            self._insert_widget(self._plan_view)
            self._scroll_to_bottom()
        elif etype == TurnEventType.PLAN_STEP_START:
            if self._plan_view:
                self._plan_view.set_step_status(event.plan_step_index, "active")
                self._plan_view.set_buttons_visible(False)
            self._scroll_to_bottom()
        elif etype == TurnEventType.PLAN_STEP_DONE:
            if self._plan_view:
                self._plan_view.set_step_status(event.plan_step_index, "done")
            self._scroll_to_bottom()

    def _handle_exploration_event(self, event: TurnEvent) -> None:
        meta = event.metadata
        if event.type == TurnEventType.EXPLORATION_PHASE_CHANGE:
            self._hide_thinking()
            self._reset_tool_run()
            self._insert_widget(
                ExplorationPhaseWidget(
                    meta.get("from_phase", ""),
                    meta.get("to_phase", ""),
                    event.text,
                )
            )
        else:  # EXPLORATION_FINDING
            self._insert_widget(
                ExplorationFindingWidget(
                    meta.get("category", "general"),
                    event.text,
                    meta.get("address"),
                    meta.get("relevance", "medium"),
                )
            )
        self._scroll_to_bottom()

    def _handle_research_event(self, event: TurnEvent) -> None:
        meta = event.metadata
        if event.type == TurnEventType.RESEARCH_NOTE_SAVED:
            self._hide_thinking()
            self._reset_tool_run()
            self._insert_widget(
                ResearchNoteWidget(
                    title=event.text,
                    genre=meta.get("genre", "general"),
                    path=meta.get("path", ""),
                    preview=meta.get("preview", ""),
                    review_passed=meta.get("review_passed", True),
                )
            )
            self._scroll_to_bottom()
        # RESEARCH_NOTE_REVIEWED ΓÇö no separate widget, info is in the saved event

    def _handle_subagent_event(self, event: TurnEvent) -> None:
        meta = event.metadata
        if event.type == TurnEventType.SUBAGENT_SPAWNED:
            name = event.text
            agent_type = meta.get("agent_type", "custom")
            self._insert_widget(SubagentEventWidget("spawned", name, f"type: {agent_type}"))
        elif event.type == TurnEventType.SUBAGENT_COMPLETED:
            name = meta.get("name", "")
            turns = meta.get("turn_count", 0)
            elapsed = meta.get("elapsed", 0.0)
            detail = f"{turns} turns, {elapsed:.0f}s"
            self._insert_widget(SubagentEventWidget("completed", name, detail))
        elif event.type == TurnEventType.SUBAGENT_FAILED:
            name = meta.get("name", "")
            error = event.error or "Unknown error"
            self._insert_widget(SubagentEventWidget("failed", name, error))
        self._scroll_to_bottom()

    def _handle_knowledge_event(self, event: TurnEvent) -> None:
        """Render the compact ``KNOWLEDGE_RETRIEVED`` indicator.

        Cheap, no scroll: this is a small status pill, not a full
        message.  Uses the dedicated :class:`KnowledgeContextWidget`
        so the label reads "Retrieved Knowledge" instead of
        "Subagent ...".
        """
        from .message_widgets import KnowledgeContextWidget

        meta = event.metadata or {}
        items = meta.get("items", []) or []
        self._insert_widget(
            KnowledgeContextWidget(
                summary=event.text or "",
                items=items,
            )
        )

    def _handle_question_event(self, event: TurnEvent) -> None:
        self._hide_thinking()
        self._reset_tool_run()
        is_orchestra = event.metadata.get("orchestra_delegate") if event.metadata else False

        if event.type == TurnEventType.SAVE_APPROVAL_REQUEST:
            options = ["Save All", "Discard All"]
        else:  # USER_QUESTION
            options = event.metadata.get("options", []) if event.metadata else []

        if is_orchestra:
            from .orchestra_approval_dialog import DelegationApprovalWidget

            delegate_spec = (event.metadata or {}).get("delegate_spec", {})
            widget = DelegationApprovalWidget(
                task_name=delegate_spec.get("task", "Unknown Task"),
                instruction=delegate_spec.get("instruction", ""),
                context=delegate_spec.get("context", ""),
                tools=delegate_spec.get("tools", []),
                model=delegate_spec.get("model", ""),
                max_steps=delegate_spec.get("max_steps", 20),
            )
            widget.approved.connect(lambda _, d="approve": self._on_orchestra_approval(event.tool_call_id, d))
            widget.denied.connect(lambda _, d="deny": self._on_orchestra_approval(event.tool_call_id, d))
        else:
            widget = UserQuestionWidget(event.text, options)
            widget.option_selected.connect(self._on_user_answer)

        self._insert_widget(widget)
        self._scroll_to_bottom()

    def _on_orchestra_approval(self, tool_call_id: str, decision: str) -> None:
        """Forward orchestra delegation approval decision to the panel/controller."""
        self.orchestra_approval_decided.emit(tool_call_id, decision)

    def _on_tool_approval(self, tool_call_id: str, decision: str) -> None:
        """Forward tool approval decision to the panel/controller."""
        self.tool_approval_submitted.emit(tool_call_id, decision)

    def _on_user_answer(self, answer: str) -> None:
        """Forward a button-selected answer to the panel/controller."""
        self.user_answer_submitted.emit(answer)

    def restore_from_messages(self, messages: list[Message]) -> None:
        """Replay saved Message objects into the chat view using pagination.

        Only the last page is rendered initially.  Older pages are
        available on demand via the navigation controls emitted into the
        chat; the full message list is retained in memory for the agent
        context.

        Sets ``_in_restore`` so the cascade of ``resizeEvent`` calls
        triggered by every ``insertWidget`` is suppressed ΓÇö without
        this, 50+ widgets can each trigger a ``setFixedWidth`` and a
        full layout pass, which dominates restore time (~50% in
        profiling). The width is fixed up explicitly at the end.
        """
        # Full reset ΓÇö clears widgets and pagination state.
        self.clear_chat()
        # A fresh restore re-enables history navigation; the live-tail
        # guard is reset by clear_chat() above, but be explicit.
        self._restore_live_tail_started = False
        self._restore_messages = list(messages)
        self._restore_units = self._build_restore_units(self._restore_messages)
        self._restore_pages = self._build_restore_pages(
            self._restore_units,
            self._restore_page_size_units,
        )

        if not self._restore_pages:
            # Nothing visible to render (e.g. hidden system messages only).
            return

        last_page = len(self._restore_pages) - 1
        self._restore_last_page = last_page
        self._restore_first_page = last_page
        self._restore_paged = True
        self._in_restore = True
        try:
            self._render_restore_window(scroll_to="bottom")
        finally:
            self._in_restore = False

    def _build_restore_units(self, messages: list[Message]) -> list[tuple[int, int]]:
        """Group messages into render units for paged restore.

        Rules:
        - Hidden persisted system hints (USER messages with ``[SYSTEM]`` prefix) are skipped.
        - A normal USER message is one unit.
        - An ASSISTANT message is one unit, extended to include the immediately following
          TOOL message so tool call + result never straddle a page boundary.
        - Orphan TOOL messages are included as their own unit for safety.
        """
        units: list[tuple[int, int]] = []
        n = len(messages)
        i = 0
        while i < n:
            msg = messages[i]
            if msg.role == Role.USER:
                if _is_hidden_system_user_message(msg.content):
                    i += 1
                    continue
                units.append((i, i + 1))
                i += 1
            elif msg.role == Role.ASSISTANT:
                start = i
                i += 1
                if i < n and messages[i].role == Role.TOOL:
                    i += 1
                units.append((start, i))
            elif msg.role == Role.TOOL:
                units.append((i, i + 1))
                i += 1
            else:
                i += 1
        return units

    def _build_restore_pages(
        self,
        units: list[tuple[int, int]],
        page_size_units: int,
    ) -> list[tuple[int, int]]:
        """Split a list of units into fixed-size page ranges (unit indices)."""
        if not units or page_size_units <= 0:
            return []
        pages: list[tuple[int, int]] = []
        total = len(units)
        for start in range(0, total, page_size_units):
            pages.append((start, min(start + page_size_units, total)))
        return pages

    def _message_range_for_restore_window(self) -> tuple[int, int]:
        """Convert the current page window into a (start, end) message range.

        ``end`` is exclusive, matching list-slicing semantics.
        """
        first = max(0, min(self._restore_first_page, len(self._restore_pages) - 1))
        last = max(first, min(self._restore_last_page, len(self._restore_pages) - 1))
        unit_start = self._restore_pages[first][0]
        unit_end_excl = self._restore_pages[last][1]
        if unit_end_excl <= 0:
            return (0, 0)
        msg_start = self._restore_units[unit_start][0]
        # The last unit's end index (exclusive) is the message slice end.
        msg_end = self._restore_units[unit_end_excl - 1][1]
        return (msg_start, msg_end)

    def _clear_rendered_widgets(self) -> None:
        """Delete currently-rendered widgets without touching pagination state.

        Used when sliding the page window; ``_restore_messages`` /
        ``_restore_pages`` are preserved so the next render can rebuild
        the visible window cheaply.
        """
        self._force_hide_thinking()
        self._thinking_hide_timer.stop()
        self._think_buffer = ""
        self._waiting_think_close = False
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            widget = item.widget() if item else None
            if widget is not None:
                widget.deleteLater()
        self._current_assistant = None
        self._message_thinking = None
        self._tool_widgets.clear()
        self._plan_view = None
        self._reset_tool_run()
        self._group_map.clear()

    def _ensure_latest_restore_window_for_live_append(self, show_nav: bool = True) -> None:
        """Jump the page window back to the final page before live appends.

        This guarantees new live messages always appear beneath the
        latest restored page instead of beneath an older one.

        ``show_nav`` controls whether the rendered page includes the
        history nav strip.  The live-tail pre-lock helper
        (``_begin_live_tail_append``) passes ``False`` because the
        live-tail flag is about to suppress nav anyway, and re-emitting
        nav frames only to immediately strip them is wasteful.
        """
        if not self._restore_paged or not self._restore_pages:
            return
        final_page = len(self._restore_pages) - 1
        if self._restore_last_page != final_page or self._restore_first_page != final_page:
            self._restore_first_page = final_page
            self._restore_last_page = final_page
            self._render_restore_window(scroll_to="bottom", show_nav=show_nav)

    def _render_restored_messages(self, messages: list[Message]) -> None:
        """Render a slice of restored messages into the current page window.

        This is the page-bounded equivalent of the old
        ``_restore_next_chunk`` body ΓÇö it iterates the supplied messages
        once and dispatches by role, but never mutates any pending queue.
        """
        for msg in messages:
            if msg.role == Role.USER:
                if _is_hidden_system_user_message(msg.content):
                    continue
                self._reset_tool_run()
                self._insert_user_message_widget(msg.content)
            elif msg.role == Role.ASSISTANT:
                self._reset_tool_run()
                # Prefer structured reasoning_content over legacy tags.
                reasoning_content = msg.reasoning_content or ""
                content = msg.content or ""
                if reasoning_content:
                    # New field: render thinking directly, content as-is.
                    tb = _ThinkingBlock()
                    tb.set_thinking(reasoning_content, in_progress=False)
                    self._insert_widget(tb)
                    if content:
                        w = AssistantMessageWidget(parent=self._container)
                        w.set_text_deferred(content)
                        self._insert_widget(w)
                    else:
                        # Reasoning-only message: still emit a spacer widget
                        # for any tool calls that follow.
                        w = AssistantMessageWidget(parent=self._container)
                        self._insert_widget(w)
                elif content:
                    thinking_text, visible_text = _split_thinking(content)

                    if thinking_text:
                        tb = _ThinkingBlock()
                        tb.set_thinking(thinking_text, in_progress=False)
                        self._insert_widget(tb)

                    w = AssistantMessageWidget(parent=self._container)
                    w.set_text_deferred(visible_text if visible_text else content)
                    self._insert_widget(w)
                else:
                    w = AssistantMessageWidget()
                    self._insert_widget(w)
                for tc in msg.tool_calls:
                    tw = ToolCallWidget(tc.name, tc.id)
                    try:
                        args_str = json.dumps(tc.arguments, indent=2)
                    except (TypeError, ValueError):
                        args_str = str(tc.arguments)
                    tw.set_arguments(args_str)
                    tw.mark_done()
                    self._tool_widgets[tc.id] = tw
                    self._register_tool_widget(tc.name, tc.id, tw)
            elif msg.role == Role.TOOL:
                self._reset_tool_run()
                for tr in msg.tool_results:
                    existing_tw = self._tool_widgets.get(tr.tool_call_id)
                    if existing_tw is not None:
                        existing_tw.set_result(tr.content, tr.is_error)
                    group = self._group_map.get(tr.tool_call_id)
                    if group:
                        group.notify_result(tr.is_error)
            # SYSTEM / unknown ΓÇö skip during restore; not part of UI transcript.

    def _make_restore_nav_widget(self, position: str) -> QWidget:
        """Build a small navigation strip for the paged restore view.

        Two of these are emitted per render ΓÇö one at the top, one at the
        bottom ΓÇö so the user can jump in either direction regardless of
        scroll position.  Buttons are disabled when no pages exist in
        that direction or when only a single page is present.
        """
        frame = QFrame()
        frame.setObjectName("history_nav")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)

        older_btn = QPushButton("Load older")
        newer_btn = QPushButton("Load newer")
        latest_btn = QPushButton("Latest")
        for btn in (older_btn, newer_btn, latest_btn):
            btn.setObjectName("history_nav_btn")

        total = len(self._restore_pages)
        first = self._restore_first_page
        last = self._restore_last_page
        has_older = first > 0
        has_newer = last < total - 1
        not_at_latest = last < total - 1

        older_btn.setEnabled(has_older)
        newer_btn.setEnabled(has_newer)
        latest_btn.setEnabled(not_at_latest)

        older_btn.clicked.connect(self._go_restore_older)
        newer_btn.clicked.connect(self._go_restore_newer)
        latest_btn.clicked.connect(self._go_restore_latest)

        # Page summary (e.g. "Showing pages 2-3 of 12").
        # Pages are 1-indexed in the label for readability.
        first_label = first + 1
        last_label = last + 1
        if first == last:
            summary = f"Page {first_label} of {total}"
        else:
            summary = f"Pages {first_label}-{last_label} of {total}"
        label = QLabel(summary)
        label.setObjectName("history_nav_label")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Top widget prioritises "Load older"; bottom prioritises
        # "Latest" / "Load newer".  Both expose all three for symmetry.
        if position == "top":
            order = (older_btn, label, newer_btn, latest_btn)
        else:
            order = (latest_btn, newer_btn, label, older_btn)
        for w in order:
            layout.addWidget(w)
        layout.addStretch(1)

        # Apply theme-aware inline styles so the strip respects the
        # active light/dark palette regardless of which theme the
        # global stylesheet was loaded with.
        self._apply_nav_widget_style(frame, label, older_btn, newer_btn, latest_btn)
        return frame

    def _apply_nav_widget_style(
        self,
        frame: QFrame,
        label: QLabel,
        *buttons: QPushButton,
    ) -> None:
        """Apply the current theme's styles to a history nav strip.

        In the explicit ``"light"`` / ``"dark"`` themes, the per-widget
        stylesheets from the style getters are applied so the nav strip
        matches the rest of the Rikugan palette.

        In the ``"ida"`` (host) theme, the per-widget stylesheets are
        cleared so the IDA wrapper's minimal targeted stylesheet (or
        the host's Qt palette) can control the nav strip.  This
        prevents a stale light/dark inline stylesheet from bleeding
        into the host theme after a theme switch.
        """
        if is_host_theme():
            frame.setStyleSheet("")
            label.setStyleSheet("")
            for btn in buttons:
                btn.setStyleSheet("")
            return
        frame.setStyleSheet(get_history_nav_frame_style())
        label.setStyleSheet(get_history_nav_label_style())
        btn_style = get_history_nav_button_style()
        for btn in buttons:
            btn.setStyleSheet(btn_style)

    def refresh_inline_styles(self) -> None:
        """Re-apply the theme-aware inline styles to history nav widgets.

        Qt's stylesheet cascade is per-widget; widgets that received a
        widget-local stylesheet at construction time do not auto-refresh
        when the parent theme changes.  This method walks the currently
        rendered nav frames and re-issues the same style sheets so the
        strip matches the active palette after a theme switch.

        In the ``"ida"`` (host) theme the per-widget stylesheets are
        cleared instead, so the IDA wrapper's minimal targeted
        stylesheet ΓÇö or the host's Qt palette ΓÇö can take over without
        a stale light/dark inline stylesheet leaking through.

        Only direct children whose ``objectName()`` is
        ``"history_nav_label"`` or ``"history_nav_btn"`` receive the
        per-widget stylesheet; unrelated children inside the frame are
        left untouched.  Widgets that have been deleted (e.g. via a
        live append that removed the nav strip) are pruned from
        ``self._nav_widgets`` so the list does not accumulate stale
        ``QFrame`` references.
        """
        host = is_host_theme()
        alive_navs: list[QFrame] = []
        for frame in self._nav_widgets:
            try:
                if host:
                    frame.setStyleSheet("")
                    for child in frame.findChildren(QLabel):
                        if child.objectName() == "history_nav_label":
                            child.setStyleSheet("")
                    for child in frame.findChildren(QPushButton):
                        if child.objectName() == "history_nav_btn":
                            child.setStyleSheet("")
                else:
                    frame.setStyleSheet(get_history_nav_frame_style())
                    btn_style = get_history_nav_button_style()
                    label_style = get_history_nav_label_style()
                    for child in frame.findChildren(QLabel):
                        if child.objectName() == "history_nav_label":
                            child.setStyleSheet(label_style)
                    for child in frame.findChildren(QPushButton):
                        if child.objectName() == "history_nav_btn":
                            child.setStyleSheet(btn_style)
                alive_navs.append(frame)
            except RuntimeError:
                # Widget may have been deleted; drop the stale ref.
                pass
        self._nav_widgets = alive_navs

    def _render_restore_window(
        self,
        scroll_to: str = "bottom",
        show_nav: bool | None = None,
    ) -> None:
        """Render the currently-selected page window.

        Idempotent: clears the layout, then re-inserts the visible
        messages plus top/bottom nav strips.

        Live-tail safety: once ``_restore_live_tail_started`` is set
        (i.e. a live widget has been appended into a paginated
        restore), this method early-returns without touching the
        layout.  The ``_go_restore_*`` callbacks also early-return in
        that state, so a stray click cannot wipe the live tail.

        ``show_nav`` overrides the default nav-strip policy.  When
        ``None`` (the default) the nav strip is shown iff there is
        more than one page.  Pass ``False`` to render the page without
        the nav strip even before the live-tail flag is set (used by
        the live-tail pre-lock helper).
        """
        # Live-tail safety: a render after a live append must not wipe
        # the live widgets.  The early-return is structural ΓÇö it guards
        # against any future caller that might trigger a re-render
        # (e.g. a stray nav-button click that bypasses the _go_*
        # guards, or a re-entry from a different code path).
        if self._restore_live_tail_started:
            return
        if not self._restore_pages:
            self._clear_rendered_widgets()
            return

        # Clamp window into [0, len(pages)) and cap its size to
        # ``_restore_max_window_pages``.  Navigation callbacks keep
        # their requested windows under this cap, but this defensive
        # clamp protects callers that set page indices directly.
        total_pages = len(self._restore_pages)
        max_window = max(1, self._restore_max_window_pages)
        last = max(0, min(self._restore_last_page, total_pages - 1))
        first = max(0, min(self._restore_first_page, last))
        if last - first + 1 > max_window:
            first = max(0, last - max_window + 1)
        self._restore_first_page = first
        self._restore_last_page = last

        # Reset the live-tracked nav widgets ΓÇö _clear_rendered_widgets
        # below will delete them.
        self._nav_widgets = []

        self._clear_rendered_widgets()

        # Resolve the nav-strip policy.  When the caller did not pass
        # one, show the nav iff there is more than one page.
        if show_nav is None:
            show_nav = total_pages > 1

        # Top nav ΓÇö only show if there's at least one page to navigate to.
        if show_nav:
            top_nav = self._make_restore_nav_widget("top")
            self._nav_widgets.append(top_nav)
            self._insert_widget(top_nav)

        msg_start, msg_end = self._message_range_for_restore_window()
        if msg_end > msg_start:
            self._render_restored_messages(self._restore_messages[msg_start:msg_end])

        if show_nav:
            bottom_nav = self._make_restore_nav_widget("bottom")
            self._nav_widgets.append(bottom_nav)
            self._insert_widget(bottom_nav)

        # Reset transient streaming/tool-run state at the end of the
        # render so the chat view is in a clean state for the next
        # live event.  We intentionally do NOT clear ``self._tool_widgets``
        # here: those entries are needed for routing tool results to
        # the rendered tool-call widgets, and the dict is cleaned up
        # when the corresponding widgets are torn down.
        self._current_assistant = None
        self._message_thinking = None
        self._reset_tool_run()

        self._restore_rendered = True

        if scroll_to == "top":
            self.verticalScrollBar().setValue(0)
        elif scroll_to == "force_bottom":
            # Explicit "Latest" actions always want to land on the
            # bottom of the chat, even if the user has scrolled away.
            self._force_scroll_to_bottom()
        else:
            self._scroll_to_bottom()

    # ------------------------------------------------------------------
    # History navigation callbacks wired from ``_make_restore_nav_widget``.
    #
    # All three callbacks are no-ops when live tail has started (the
    # live tail is the source of truth once a live widget has been
    # appended; allowing the user to wipe it by clicking a nav button
    # would be a regression) and when there are no pages to navigate.
    # The actual slide/jump logic is in ``_render_restore_window``,
    # which is idempotent and clamps the window into the valid range.
    # ------------------------------------------------------------------

    def _go_restore_older(self) -> None:
        """Slide the visible page window toward older pages.

        Each click grows the window by its current size (roughly
        doubling the visible history) up to
        ``_restore_max_window_pages``.  When the window is already at
        the cap, it slides the capped window back so the user sees
        fresh older pages instead of the same fixed window.  No-op if
        the window already touches the oldest page, if the live tail
        has started, or if there are no pages.
        """
        if self._restore_live_tail_started:
            return
        if not self._restore_paged or not self._restore_pages:
            return
        total = len(self._restore_pages)
        first = max(0, min(self._restore_first_page, total - 1))
        last = max(first, min(self._restore_last_page, total - 1))
        if first == 0:
            return  # already at the oldest page
        current_size = last - first + 1
        max_window = max(1, self._restore_max_window_pages)
        if current_size >= max_window:
            # Already at the cap: slide the capped window toward
            # older pages rather than growing past the cap.
            new_first = max(0, first - current_size)
            new_last = min(total - 1, new_first + current_size - 1)
        else:
            # Grow the visible window toward older pages, preserving
            # the newest edge of the current window.
            new_size = min(current_size * 2, max_window)
            new_first = max(0, last - new_size + 1)
            new_last = last
        if new_first == first and new_last == last:
            return  # nothing changed
        self._restore_first_page = new_first
        self._restore_last_page = new_last
        self._render_restore_window(scroll_to="top")

    def _go_restore_newer(self) -> None:
        """Slide the visible page window toward newer pages.

        Moves ``_restore_last_page`` forward by the current window
        size (so the window slides rather than grows), capped at the
        final page.  No-op if the window already touches the newest
        page, if the live tail has started, or if there are no pages.
        """
        if self._restore_live_tail_started:
            return
        if not self._restore_paged or not self._restore_pages:
            return
        total = len(self._restore_pages)
        first = max(0, min(self._restore_first_page, total - 1))
        last = max(first, min(self._restore_last_page, total - 1))
        if last >= total - 1:
            return  # already at the newest page
        current_size = last - first + 1
        new_last = min(total - 1, last + current_size)
        if new_last == last:
            return  # nothing changed
        # Keep the window the same size; the leading edge slides with
        # the trailing edge so the user keeps the same amount of
        # context on screen.
        new_first = max(0, new_last - current_size + 1)
        self._restore_first_page = new_first
        self._restore_last_page = new_last
        self._render_restore_window(scroll_to="force_bottom")

    def _go_restore_latest(self) -> None:
        """Jump the visible page window to the final page.

        No-op if the window is already at the final page, if the
        live tail has started, or if there are no pages.
        """
        if self._restore_live_tail_started:
            return
        if not self._restore_paged or not self._restore_pages:
            return
        total = len(self._restore_pages)
        last = total - 1
        if self._restore_last_page == last and self._restore_first_page == last:
            return  # already at the latest
        self._restore_first_page = last
        self._restore_last_page = last
        self._render_restore_window(scroll_to="force_bottom")

    # ------------------------------------------------------------------
    # Async restore ΓÇö uses a worker thread to build MessageSpecs without
    # blocking the GUI.  The main thread inserts MessagePlaceholders up
    # front (so scrollbar geometry is correct from frame 1) and replaces
    # them with real widgets as each chunk_ready signal arrives.
    # ------------------------------------------------------------------

    def restore_from_messages_async(self, messages: list[Message]) -> None:
        """Start a background restore of *messages*.

        Behaviour:
        1. Cancels any in-flight restore (its worker is told to stop and
           any late signals are dropped via a generation counter).
        2. Clears the view and immediately inserts one
           :class:`MessagePlaceholder` per message so the layout has a
           correct total height.  This gives the QScrollArea an accurate
           ``verticalScrollBar().maximum()`` from the first paint.
        3. Starts a :class:`RestoreWorker` thread that builds
           ``MessageSpec`` objects off the UI thread.  Each
           ``chunk_ready`` slot replaces the corresponding placeholders
           with real widgets.

        Idempotency: calling this method twice cancels the first
        worker and discards its late signals.
        """
        # Detect a "Load older" re-restore: same message list, grown cap.
        # In that case we must NOT reset the cap (the caller grew it on
        # purpose). A different message list (new session / new tab)
        # resets to the default cap.
        is_load_older = (
            messages is self._restore_messages and self._restore_max_rendered > _RESTORE_DEFAULT_MAX_RENDERED
        )

        # Cancel any prior worker ΓÇö late signals are dropped via
        # ``_restore_generation`` below.
        self._cancel_restore()

        self.clear_chat()
        if not is_load_older:
            self._restore_max_rendered = _RESTORE_DEFAULT_MAX_RENDERED
        # Stash the full message list AFTER clear_chat (which resets
        # ``_restore_messages`` to []) so ``_update_load_older_button``
        # can compute how many messages remain beyond the cap, and so a
        # later "Load older" click can re-enter restore with the same
        # list. Keep the exact reference (no ``list(...)`` copy) so the
        # ``is`` identity check above still recognises a re-restore.
        self._restore_messages = messages
        self._in_restore = True

        # Bump generation so any late signals from the prior worker
        # are ignored.  Captured in closures for chunk/finished slots.
        self._restore_generation += 1
        generation = self._restore_generation

        # Insert one placeholder per *visible* message so the layout
        # is full from the start.  We mirror the worker's pairing
        # rule: an ASSISTANT message with tool_calls followed by a
        # TOOL message is a single render unit, so only the ASSISTANT
        # gets a placeholder.  The TOOL message is "consumed" by the
        # ASSISTANT spec and never produces its own placeholder.
        self._placeholders: dict[str, MessagePlaceholder] = {}
        i = 0
        n = len(messages)
        while i < n:
            msg = messages[i]
            if msg.role == Role.USER and _is_hidden_system_user_message(msg.content):
                i += 1
                continue
            placeholder_id = msg.id or f"restore_{i}"
            # Use a per-message heuristic so placeholders track the
            # content size closely enough for the scrollbar.  When
            # this message will be paired with the next TOOL message,
            # include the tool heights in the placeholder estimate so
            # the scrollbar doesn't jump when the real widgets land.
            est = self._placeholder_height_for(msg)
            consumed = 0
            if msg.role == Role.ASSISTANT and msg.tool_calls and i + 1 < n and messages[i + 1].role == Role.TOOL:
                next_msg = messages[i + 1]
                est += self._tool_results_height_estimate(next_msg.tool_results)
                consumed = 1
            ph = MessagePlaceholder(est, placeholder_id, parent=self._container)
            self._insert_widget(ph)
            self._placeholders[placeholder_id] = ph
            i += 1 + consumed

        # Pin width once now; widgets added later inherit it.
        if self._container is not None:
            self._container.setFixedWidth(self.viewport().width())

        # Start the worker.  No Qt signals cross the thread boundary —
        # see ``AGENTS.md`` §1 (Shiboken UAF on Python ≥ 3.11).  The
        # worker enqueues ``(kind, payload)`` tuples onto ``worker.queue``;
        # ``_drain_restore_queue`` (driven by ``_ensure_restore_poll_timer``)
        # is the sole consumer on the main thread.
        worker = RestoreWorker(messages, parent=self, max_rendered=self._restore_max_rendered)
        # ``finished`` is emitted by QThread when ``run`` returns; use
        # it as a hard cleanup point regardless of the queue's
        # ``"finished"`` sentinel (cancelled workers do not enqueue
        # one, and a hard crash never reaches the finally block).
        worker.finished.connect(lambda w=worker: self._on_worker_finished(w))
        self._restore_worker = worker
        self._ensure_restore_poll_timer()
        worker.start()

    @staticmethod
    def _placeholder_height_for(msg: Message) -> int:
        """Cheap height estimate for the placeholder of a single message."""
        if msg.role == Role.USER:
            return _estimate_user_height(msg.content)
        if msg.role == Role.ASSISTANT:
            return _estimate_assistant_height(msg.content)
        if msg.role == Role.TOOL:
            return max(60, 80 * len(msg.tool_results or msg.tool_calls))
        return 60

    @staticmethod
    def _tool_results_height_estimate(results) -> int:
        """Approximate total height for a list of ``ToolResult`` objects.

        Used to pad an ASSISTANT placeholder that will be paired with
        the following TOOL message, so the scrollbar does not have to
        be re-sized twice (once for the placeholder, once again when
        the real tool widgets land).
        """
        if not results:
            return 0
        total = 0
        for tr in results:
            spec = ToolSpec(
                id=tr.tool_call_id,
                name=tr.name,
                arguments_json="",
                result_content=tr.content,
                result_is_error=tr.is_error,
            )
            total += _estimate_tool_height(spec.result_content)
        return total

    def _cancel_restore(self) -> None:
        """Cancel the in-flight restore (if any) and bump generation."""
        worker = getattr(self, "_restore_worker", None)
        if worker is not None:
            try:
                worker.cancel()
            except RuntimeError:
                pass  # already deleted
        # Bump generation BEFORE stopping the timer so any in-flight
        # drain callback that fires after the stop sees the new
        # generation and bails out via ``_on_chunk_ready`` /
        # ``_on_restore_finished``.
        self._restore_generation += 1
        self._restore_worker = None
        # Without this, cancelling an async restore permanently
        # suppresses resizeEvent layout.  The flag is normally managed
        # by restore_from_messages' try/finally, but cancel can be
        # invoked out-of-band (clear_chat, new restore, etc.).
        self._in_restore = False
        # Stop the drain timer; the worker thread may still be alive
        # briefly after ``cancel()`` returns, but its remaining queue
        # items belong to the bumped generation and would be no-ops.
        self._stop_restore_poll_timer()

    def _on_chunk_ready(self, chunk: _RenderedChunk, generation: int) -> None:
        """Replace placeholders with real widgets for one chunk.

        Each spec may produce zero, one, or multiple widgets (e.g. an
        ASSISTANT spec with both visible text and tool calls expands
        to a thinking block + assistant widget + one or more tool
        widgets).  All widgets produced for a single spec are
        inserted at the placeholder's *original* layout index, in
        render order, so order is preserved across chunk boundaries.

        Drops the chunk if a newer restore has superseded this worker.
        """
        if generation != self._restore_generation:
            return  # superseded
        for spec in chunk.specs:
            ph = self._placeholders.pop(spec.msg_id, None)
            if ph is None:
                continue
            # Mirror the sync restore: each spec starts a fresh tool
            # run so restored grouping state cannot leak into the
            # next spec (or the next live turn after restore).
            self._reset_tool_run()
            widgets = self._build_widgets_from_spec(spec)
            if not widgets:
                # Spec was filtered out (hidden user, empty tool
                # list, etc.) ΓÇö collapse the placeholder to zero
                # height and delete it cleanly.
                ph.setMinimumHeight(0)
                ph.setMaximumHeight(0)
                ph.deleteLater()
                continue
            self._replace_placeholder_with_widgets(ph, widgets)
        # Belt-and-braces: clear any per-spec run state that may
        # still be in flight from the last spec in the chunk so the
        # next live event starts clean.
        self._reset_tool_run()
        # Trigger a single repaint of the affected region.
        if self._container is not None:
            self._container.update()

    def _on_restore_finished(self, generation: int) -> None:
        if generation != self._restore_generation:
            return
        # Leftover placeholders are messages the worker skipped because
        # they fell outside the ``max_rendered`` window (older messages
        # kept as lightweight spacers for scrollbar geometry). Keep them
        # in the layout and in ``_placeholders`` so a future "load older"
        # action can still find them by id. Filtered/empty specs are
        # already cleaned up in ``_on_chunk_ready`` (height 0 + deleteLater),
        # so any leftover here is a skip, not a leak.
        self._in_restore = False
        # The worker is done — stop the drain timer.  ``_drain_restore_queue``
        # also stops on the sentinel path, but stopping here covers the
        # case where the drain is mid-batch and the safety-net
        # ``_on_worker_finished`` already ran first.
        self._stop_restore_poll_timer()
        # Reapply width and scroll-to-bottom now that the real widgets
        # are in place.
        if self._container is not None:
            self._container.setFixedWidth(self.viewport().width())
        self._scroll_to_bottom()
        # Show/hide the "Load older" button depending on whether the
        # session still has unmaterialised messages beyond the cap.
        self._update_load_older_button()

    @staticmethod
    def _remaining_older_count(total: int, cap: int) -> int:
        """Pure logic: how many messages are beyond the current cap.

        Returns 0 (or negative, clamped by callers) when the cap covers
        every message. Extracted so tests can exercise the cap arithmetic
        without instantiating a full ChatView.
        """
        return total - cap

    @staticmethod
    def _next_cap(current: int, total: int) -> int:
        """Pure logic: the cap to apply after a "Load older" click.

        Doubles the current cap, clamped to the full message count. The
        doubling bounds the number of clicks to log2(N) while never
        overshooting the session.
        """
        return min(current * 2, total)

    def _update_load_older_button(self) -> None:
        """Show a "Load older" button at the top when the session has
        more messages than the current render cap.

        Clicking the button grows the cap and re-runs the async restore,
        so the user can page back into a long history without paying the
        full restore cost up front. The button is hidden once the cap
        covers every message.
        """
        total = len(self._restore_messages)
        remaining = ChatView._remaining_older_count(total, self._restore_max_rendered)
        if remaining <= 0:
            if self._load_older_btn is not None:
                self._load_older_btn.deleteLater()
                self._load_older_btn = None
            return
        if self._load_older_btn is None:
            btn = QPushButton(f"Load older ({remaining} more messages)", parent=self._container)
            btn.setObjectName("history_nav_btn")
            btn.clicked.connect(self._load_older_clicked)
            # Insert at the very top (index 0, before the first placeholder).
            self._layout.insertWidget(0, btn)
            self._load_older_btn = btn
        else:
            self._load_older_btn.setText(f"Load older ({remaining} more messages)")

    def _load_older_clicked(self) -> None:
        """Grow the render cap and re-run the async restore.

        Each click doubles the cap (capped at the full message count) so
        the user can reach the beginning in a few clicks without the
        plugin guessing how far back they want to go.
        """
        total = len(self._restore_messages)
        new_cap = ChatView._next_cap(self._restore_max_rendered, total)
        if new_cap == self._restore_max_rendered:
            # Already at or beyond the full count — no-op defensively.
            return
        self._restore_max_rendered = new_cap
        self.restore_from_messages_async(self._restore_messages)

    def _ensure_restore_poll_timer(self) -> None:
        """Create + start the restore drain timer if not already running.

        Mirrors ``RikuganPanelCore._ensure_history_poll_timer``:
        a dedicated ``QTimer`` drains the active ``RestoreWorker`` queue
        at :data:`_RESTORE_POLL_INTERVAL_MS` cadence.  The timer is
        stopped by ``_on_restore_finished`` on the normal completion
        path, and by ``_on_worker_finished`` (safety net) for
        cancelled/crashed workers.
        """
        if self._restore_poll_timer is not None:
            return
        timer = QTimer(self)
        timer.timeout.connect(self._drain_restore_queue)
        timer.start(_RESTORE_POLL_INTERVAL_MS)
        self._restore_poll_timer = timer

    def _stop_restore_poll_timer(self) -> None:
        """Stop + tear down the restore drain timer.

        Mirrors ``RikuganPanelCore._stop_history_poll_timer``: stop,
        disconnect, deleteLater, null the reference.  Swallows
        ``RuntimeError`` / ``TypeError`` so partial-init fixtures
        and post-teardown calls stay safe.
        """
        timer = self._restore_poll_timer
        if timer is None:
            return
        try:
            timer.stop()
        except RuntimeError:
            pass
        try:
            timer.timeout.disconnect(self._drain_restore_queue)
        except (RuntimeError, TypeError):
            pass
        timer.deleteLater()
        self._restore_poll_timer = None

    def _drain_restore_queue(self) -> None:
        """Main-thread slot: drain ``RestoreWorker.queue`` and dispatch.

        ``RestoreWorker.run()`` runs on a Qt thread but emits NO Qt
        signals (Shiboken UAF on Python ≥ 3.11 — see ``AGENTS.md`` §1).
        Instead it pushes ``(kind, payload)`` tuples onto a
        ``queue.Queue``.  This slot is the sole consumer: it runs on
        the main thread (timer-driven) and dispatches chunks to
        ``_on_chunk_ready`` and the ``"finished"`` sentinel to
        ``_on_restore_finished``.  Widget construction lives entirely
        on the main thread as a result.

        Each tick processes up to :data:`_RESTORE_DRAIN_BATCH` items
        to bound per-tick cost.  The timer continues firing until
        ``_on_restore_finished`` sees the sentinel and stops it.
        """
        worker = self._restore_worker
        if worker is None:
            return
        # Capture the live generation so late items from a superseded
        # worker (the same way ``_on_chunk_ready`` already gates by
        # ``generation``) can no-op via the per-call early return.
        generation = self._restore_generation
        for _ in range(_RESTORE_DRAIN_BATCH):
            try:
                kind, payload = worker.queue.get_nowait()
            except queue.Empty:
                return
            if kind == _RESTORE_KIND_CHUNK:
                assert isinstance(payload, _RenderedChunk)
                self._on_chunk_ready(payload, generation)
            elif kind == _RESTORE_KIND_FINISHED:
                self._on_restore_finished(generation)
                # Stop polling: the worker is done.  _on_restore_finished
                # also calls ``_stop_restore_poll_timer`` so this is
                # belt-and-braces for partial-init paths.
                self._stop_restore_poll_timer()
                return
            # Unknown kind: skip and keep draining (defensive — a future
            # protocol addition should not crash the existing drain).

    def _on_worker_finished(self, worker: RestoreWorker) -> None:
        """Cleanup hook for the worker's QThread.finished signal.

        ``RestoreWorker`` no longer emits ``chunk_ready`` /
        ``finished_ok`` (those were cross-thread Qt signals — Shiboken
        UAF risk).  The main-thread ``QTimer`` drains the worker
        ``queue`` instead.  This hook is now only a safety net:
        if the worker exits WITHOUT a clean ``"finished"`` sentinel
        (a hard crash, an unhandled exception, or an early cancel
        before any chunks), ``_in_restore`` would otherwise remain
        True forever and suppress the resizeEvent cascade on every
        subsequent live message.  We clear it here for the *current*
        generation only — a newer restore's generation does not match
        and is left alone.
        """
        # Safety-net cleanup: if the worker exited without a clean
        # ``"finished"`` sentinel (the normal completion path is
        # ``_on_restore_finished`` which already cleared
        # ``_in_restore``), make sure the flag does not leak.
        # ``self._restore_worker`` is the worker the view still
        # considers "current" — if it matches, we own the cleanup.
        if getattr(self, "_restore_worker", None) is worker:
            # We do not know which generation this worker was
            # started under, but the per-generation guards in
            # ``_on_chunk_ready`` / ``_on_restore_finished`` already
            # ensure no later restore has been disturbed.  The only
            # remaining leak is the ``_in_restore`` flag itself and
            # any leftover placeholders that may not have been
            # consumed (e.g. a worker that crashed mid-loop).
            self._in_restore = False
            leftovers = list(self._placeholders.values())
            self._placeholders.clear()
            for ph in leftovers:
                try:
                    ph.deleteLater()
                except RuntimeError:
                    pass
        # The QTimer may still be alive (waiting for a sentinel that
        # will never come for a cancelled worker).  ``_on_restore_finished``
        # already stopped it on the normal completion path; this
        # stop is the safety-net cleanup for the cancelled/crashed case.
        self._stop_restore_poll_timer()
        worker.deleteLater()

    def _replace_placeholder_with_widgets(self, placeholder: MessagePlaceholder, widgets: list[QWidget]) -> None:
        """Replace *placeholder* with *widgets* in render order.

        The widgets are inserted at the placeholder's original layout
        index, in the supplied order, so the visual order matches
        the order the worker emitted them.  This preserves order
        even when ``_RESTORE_CHUNK_SIZE == 1`` and chunks are
        delivered one spec at a time.
        """
        layout = self._layout
        if layout is None:
            return
        idx = layout.indexOf(placeholder)
        if idx < 0:
            # Placeholder already gone ΓÇö fall back to the live-append
            # path.  ``_insert_widget`` puts each widget just before
            # the trailing stretch.
            for w in widgets:
                self._insert_widget(w)
            return
        layout.removeWidget(placeholder)
        placeholder.setParent(None)
        placeholder.deleteLater()
        # Insert in order.  Each ``insertWidget`` shifts subsequent
        # items down by one, so consecutive ``idx + i`` positions
        # keep the widgets packed together at the placeholder's slot.
        for i, w in enumerate(widgets):
            target = idx + i
            if target >= layout.count():
                self._insert_widget(w)
            else:
                layout.insertWidget(target, w)

    def _build_widgets_from_spec(self, spec: MessageSpec) -> list[QWidget]:
        """Materialise the real widgets for a single MessageSpec.

        Returns a list of top-level widgets in *render order*.  The
        caller is responsible for inserting them into the layout.
        An empty list means "this spec produced no visible widget"
        (e.g. a hidden system user message, or an orphan TOOL with
        no results) and the caller should collapse the placeholder.

        For an ASSISTANT spec the function mirrors the sync restore
        path exactly:
        - run ``_split_thinking`` on the content
        - emit a ``_ThinkingBlock`` if any thinking was extracted
        - emit an ``AssistantMessageWidget`` with ``set_text_deferred``
          populated with the *visible* portion (falling back to the
          full content if visible_text is empty, like the sync path)
        - emit one or more restored tool widgets (grouped into a
          ``ToolGroupWidget`` when the spec carries 2+ tool_specs)
        """
        try:
            role = Role(spec.role)
        except ValueError:
            return []

        if role == Role.USER:
            if _is_hidden_system_user_message(spec.content):
                return []
            return [UserMessageWidget(spec.content, parent=self._container)]

        if role == Role.ASSISTANT:
            widgets: list[QWidget] = []
            content = spec.content or ""
            visible_text = ""
            thinking_text = ""
            # Prefer structured reasoning_content over legacy <think> tags.
            if spec.reasoning_content:
                thinking_text = spec.reasoning_content
                visible_text = content
            elif content:
                thinking_text, visible_text = _split_thinking(content)
            if thinking_text:
                tb = _ThinkingBlock(parent=self._container)
                tb.set_thinking(thinking_text, in_progress=False)
                widgets.append(tb)
            # Mirror the sync path: always emit an
            # ``AssistantMessageWidget`` for an ASSISTANT message,
            # even when the content is empty (the widget acts as a
            # spacer before the following tool widgets).
            w = AssistantMessageWidget(parent=self._container)
            # Prefer the worker's pre-rendered HTML so the UI thread skips
            # md_to_html entirely. Fall back to set_text_deferred (which
            # renders on first show) when no HTML was pre-rendered
            # (legacy sync path, or empty content).
            render_source = visible_text if visible_text else content
            if spec.content_html and render_source:
                w.set_html_deferred(render_source, spec.content_html)
            else:
                w.set_text_deferred(render_source)
            widgets.append(w)
            widgets.extend(self._build_restored_tool_widgets(spec.tool_specs))
            return widgets

        if role == Role.TOOL:
            return self._build_restored_tool_widgets(spec.tool_specs)

        return []

    def _build_restored_tool_widgets(self, tool_specs: tuple[ToolSpec, ...]) -> list[QWidget]:
        """Build tool widgets from a sequence of ``ToolSpec``.

        Restored widgets are marked done and have their result
        pre-applied so they look exactly like completed live calls.
        2+ tool calls collapse into a single ``ToolGroupWidget``,
        matching the sync restore's ``_register_tool_widget``
        grouping behaviour.  ``self._tool_widgets`` and
        ``self._group_map`` are updated so future TOOL_RESULT events
        can still route back to the right widget.
        """
        if not tool_specs:
            return []
        tool_widgets: list[ToolCallWidget | ExecutePythonWidget] = []
        for ts in tool_specs:
            if ts.name == constants.EXECUTE_PYTHON_TOOL_NAME:
                tw = ExecutePythonWidget(ts.id, parent=self._container)
            else:
                tw = ToolCallWidget(ts.name, ts.id, parent=self._container)
            tw.set_arguments(ts.arguments_json)
            tw.mark_done()
            if ts.result_content or ts.result_is_error:
                tw.set_result(ts.result_content, ts.result_is_error)
            self._tool_widgets[ts.id] = tw
            tool_widgets.append(tw)

        if len(tool_widgets) >= _TOOL_GROUP_MIN_CALLS:
            group = ToolGroupWidget(parent=self._container)
            for tw, ts in zip(tool_widgets, tool_specs, strict=False):
                tw.hide_preview()
                group.add_widget(tw, ts.name)
                self._group_map[ts.id] = group
                # Pre-account for results so the group's status
                # reflects "all done" at first render.
                group.notify_result(ts.result_is_error)
            return [group]

        return list(tool_widgets)

    def clear_chat(self) -> None:
        # Cancel any in-flight async restore so its worker stops
        # emitting signals while we tear down the widgets.
        self._cancel_restore()
        # Full reset ΓÇö wipes widgets AND pagination state.
        self._force_hide_thinking()
        self._thinking_hide_timer.stop()
        self._think_buffer = ""
        self._waiting_think_close = False
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self._current_assistant = None
        self._message_thinking = None
        self._tool_widgets.clear()
        self._plan_view = None
        self._reset_tool_run()
        self._group_map.clear()
        self._placeholders.clear()

        # Reset pagination state.
        self._restore_messages = []
        self._restore_units = []
        self._restore_pages = []
        self._restore_first_page = 0
        self._restore_last_page = 0
        # Drop the "Load older" button on clear; ``restore_from_messages_async``
        # re-creates it from the new cap if needed. The cap itself is reset by
        # callers that start a fresh session (``new_chat`` / panel_core restore),
        # not here — ``_load_older_clicked`` re-enters restore with a grown cap
        # and must survive the ``clear_chat`` this triggers.
        if self._load_older_btn is not None:
            self._load_older_btn.deleteLater()
            self._load_older_btn = None
        self._restore_paged = False
        self._restore_rendered = False
        # Live-tail guard is per-restore, so a fresh restore re-enables
        # history navigation.
        self._restore_live_tail_started = False
        self._nav_widgets = []

    def _insert_widget(self, widget: QWidget) -> None:
        """Insert before the stretch at the end."""
        idx = self._layout.count() - 1
        self._layout.insertWidget(idx, widget)

    def resizeEvent(self, event) -> None:
        """Keep the container width pinned to the viewport width.

        QScrollArea.setWidgetResizable(True) handles this when there is no
        horizontal scrollbar, but QLabel rich-text word-wrap still sometimes
        requests a wider sizeHint.  Explicitly clamping here guarantees text
        wraps to the visible area.

        Suppressed during ``restore_from_messages`` because every
        ``insertWidget`` triggers a resize cascade; 50+ widgets in
        a row causes 50+ ``setFixedWidth`` calls (~50% of total
        restore time in profiling). The width is reapplied once at
        the end of the restore.
        """
        super().resizeEvent(event)
        if self._container is not None and not getattr(self, "_in_restore", False):
            self._container.setFixedWidth(self.viewport().width())

    def _is_near_bottom(self) -> bool:
        """True if the user hasn't scrolled up (within ~60px of bottom)."""
        sb = self.verticalScrollBar()
        return sb.maximum() - sb.value() < 60

    def _scroll_to_bottom(self) -> None:
        if not self._is_near_bottom():
            return
        # Don't restart an already-running timer - Qt coalesces the start() calls
        if self._scroll_timer.isActive():
            return
        self._scroll_timer.start()

    def _force_scroll_to_bottom(self) -> None:
        """Scroll the chat to the bottom regardless of the user's scroll position.

        Used by explicit "Latest" / "Jump to bottom" actions where the
        user has indicated they want to be at the bottom even if they
        had previously scrolled up to read older messages.  Bypasses
        the ``_is_near_bottom`` guard used by passive streaming.
        """
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _do_scroll(self) -> None:
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())

    def shutdown(self) -> None:
        self._cancel_restore()
        self._scroll_timer.stop()
        self._thinking_hide_timer.stop()
        self._force_hide_thinking()
