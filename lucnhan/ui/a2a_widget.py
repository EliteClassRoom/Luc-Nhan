"""A2ABridgeWidget — Qt UI for delegating tasks to external agents.

Mirrors the design in AGENTS.md: a 3-pane widget (Available Agents,
Delegate Task form, Task History table) that lets the user browse
auto-discovered + user-configured agents, send a task, and review
the streamed output. The widget is hosted in the existing
``LucNhanPanelCore`` and runs alongside the chat tabs.

Threading: every delegation runs in its own ``threading.Thread``
(one thread per task, not a shared pool) so a long-running
subprocess call doesn't block the UI. The thread enqueues plain
``_A2ATaskEvent`` objects into a ``queue.Queue``; the widget's main
thread owns a ``QTimer`` that polls each runner's queue and routes
events to the existing UI handlers. This avoids Qt cross-thread
signal/slot issues (Shiboken UAF on IDA's Qt binding) and matches
the queue + polling model used elsewhere in Luc Nhan
(``panel_core._poll_events``, ``_poll_tools_events``). Cancellation
is done by a ``threading.Event`` that the runner passes to the
dispatcher, which polls it between subprocess stdout lines
(subprocess transport) or A2A status checks (a2a transport).
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..agent.a2a import A2ADispatcher
from ..agent.turn import TurnEventType
from ..core.early_log import _early_log
from ..core.logging import get_logger
from .qt_compat import (
    QAbstractItemView,
    QCheckBox,
    QColor,
    QComboBox,
    QEvent,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    Qt,
    QTableWidget,
    QTableWidgetItem,
    QTimer,
    QVBoxLayout,
    QWidget,
    Signal,
)

# Role constants — store the ExternalAgentConfig on the QListWidgetItem
# (UserRole) and the task_id on QTableWidgetItem cells (UserRole + 1).
# We keep a named constant for the +1 offset so the magic number doesn't
# leak through the file. Both are stable Qt 5 / Qt 6 values; the
# ``qt_compat`` shim re-exports ``Qt`` from either binding.
_TASK_ID_ROLE: int = Qt.ItemDataRole.UserRole + 1

# History table column indices. Centralised so a future layout change
# (e.g. add a "Duration" column) ripples to every handler at once.
_COL_TIME = 0
_COL_AGENT = 1
_COL_TASK = 2
_COL_STATUS = 3
_COL_RESULT = 4

# How often the widget polls runner event queues (milliseconds).
_POLL_INTERVAL_MS = 100

# How many events we drain per runner per poll tick. Bounded so a
# bursty stream can't stall the main thread.
_MAX_EVENTS_PER_TICK = 64

logger = get_logger()

# ---------------------------------------------------------------------------
# Background task runner — drives a single dispatch call in a thread
# ---------------------------------------------------------------------------


class _A2ARunnerEventType(str, Enum):
    """Renderer-facing event names emitted by ``_A2ATaskRunner``.

    Using an enum (rather than bare string literals) lets the dispatcher
    surface the same set of values everywhere they are referenced
    (runner enqueue, poll dispatcher, terminal-event classification)
    so a typo becomes a ``NameError`` at import time instead of a
    silently-dropped event at runtime.
    """

    STARTED = "started"
    OUTPUT = "output"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: Runner events that mean "no more polling needed" for this task.
#: A terminal event always removes the runner from ``_inflight`` and
#: stops iterating that runner's queue. The shared reference lets
#: both the runner (``_run``) and the widget (``_poll_task_events``)
#: agree on which events close the loop.
_TERMINAL_RUNNER_EVENTS: frozenset[_A2ARunnerEventType] = frozenset(
    {
        _A2ARunnerEventType.COMPLETED,
        _A2ARunnerEventType.FAILED,
        _A2ARunnerEventType.CANCELLED,
    }
)


@dataclass
class _A2ATaskEvent:
    """Widget-facing event for one delegation step.

    ``type`` is a ``_A2ARunnerEventType`` member. ``text`` carries the
    per-type payload (streamed text, final result, or error message).
    The widget is the only consumer; the dispatcher is unaware of this
    dataclass.
    """

    type: _A2ARunnerEventType
    task_id: str
    text: str = ""


class _A2ATaskRunner:
    """Runs one ``A2ADispatcher.run_task`` call in a background thread.

    Owns the ``threading.Thread`` + ``threading.Event`` + ``queue.Queue``
    for a single delegation. The widget enqueues events by appending
    to ``self.queue``; the poll timer drains the queue on the GUI
    thread and routes each event to a UI handler.

    Thread safety: the background thread is the only writer to
    ``self.queue``; the widget is the only reader. ``cancel_event``
    is shared with the dispatcher, which is documented to be
    thread-safe (``threading.Event`` is a synchronization primitive).

    Cancellation is encapsulated behind ``cancel()`` and ``is_alive()``
    so callers never reach into ``self._cancel_event`` / ``self._thread``
    directly. The widget-side shutdown loop uses ``is_alive()`` to
    decide whether a runner has actually exited before pruning it
    from ``_inflight``.
    """

    def __init__(
        self,
        dispatcher: A2ADispatcher,
        agent_name: str,
        task: str,
        include_context: str,
        cancel_event: threading.Event,
        task_id: str,
    ) -> None:
        self._dispatcher = dispatcher
        self._agent_name = agent_name
        self._task = task
        self._include_context = include_context
        self._cancel_event = cancel_event
        self.task_id = task_id
        self.queue: queue.Queue[_A2ATaskEvent] = queue.Queue()
        self._thread = threading.Thread(
            target=self._run,
            name=f"a2a-task-{task_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def cancel(self) -> None:
        """Signal the dispatcher to abort. Idempotent and thread-safe."""
        self._cancel_event.set()

    def is_alive(self) -> bool:
        """True if the background thread is still running."""
        return self._thread.is_alive()

    def join(self, timeout: float) -> None:
        """Best-effort join — daemon threads won't block process exit."""
        self._thread.join(timeout=timeout)

    def _enqueue(self, type_: _A2ARunnerEventType, text: str = "") -> None:
        self.queue.put(_A2ATaskEvent(type=type_, task_id=self.task_id, text=text))

    def _run(self) -> None:
        """Background entry point. Runs once; never returns events twice."""
        self._enqueue(_A2ARunnerEventType.STARTED)
        gen = self._dispatcher.run_task(
            self._agent_name,
            self._task,
            cancel_event=self._cancel_event,
            include_context=self._include_context,
        )
        result_text = ""
        terminal_type: _A2ARunnerEventType | None = None
        terminal_text: str = ""
        try:
            while True:
                try:
                    event = next(gen)
                except StopIteration as stop:
                    # The dispatcher's generator return value IS the
                    # authoritative final result. A plain ``for`` loop
                    # would discard this — manual iteration captures it.
                    result_text = stop.value or ""
                    break
                if event.type == TurnEventType.TEXT_DELTA:
                    self._enqueue(_A2ARunnerEventType.OUTPUT, event.text or "")
                elif event.type == TurnEventType.ERROR:
                    msg = event.error or "External agent error"
                    # If the cancel event is set, the user cancelled
                    # the task (the cancel button is the only writer
                    # in normal operation). A timeout from the
                    # dispatcher sets the event too, so we
                    # conservatively classify it as cancelled as
                    # well — the status text is what the user sees.
                    if self._cancel_event.is_set():
                        terminal_type = _A2ARunnerEventType.CANCELLED
                    else:
                        terminal_type = _A2ARunnerEventType.FAILED
                    terminal_text = msg
                    break
        except Exception as e:
            terminal_type = _A2ARunnerEventType.FAILED
            terminal_text = f"Worker exception: {e}"
        finally:
            try:
                gen.close()
            except Exception:
                pass

        if terminal_type is not None:
            self._enqueue(terminal_type, terminal_text)
        else:
            self._enqueue(_A2ARunnerEventType.COMPLETED, result_text)


# ---------------------------------------------------------------------------
# History row model — kept as a dict, not a dataclass, to avoid
# thread-safety concerns with Qt row reads from the main thread.
# ---------------------------------------------------------------------------


class _HistoryStatus(str, Enum):
    """Lifecycle status for one ``_HistoryRow``.

    Stored on the row and rendered as plain text via ``.value`` so the
    user sees the same five words the existing string-based code
    produced. Centralising the vocabulary prevents status strings
    drifting between ``_on_task_*`` handlers and table renderers.
    """

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class _HistoryRow:
    """Lightweight record for one delegation row in the history table."""

    __slots__ = (
        "agent_name",
        "error_text",
        "result_text",
        "started_at",
        "status",
        "task_excerpt",
        "task_id",
    )

    def __init__(
        self,
        task_id: str,
        agent_name: str,
        task_excerpt: str,
    ) -> None:
        self.task_id = task_id
        self.agent_name = agent_name
        self.task_excerpt = task_excerpt
        self.status: _HistoryStatus = _HistoryStatus.QUEUED
        self.started_at = time.time()
        self.result_text = ""
        self.error_text = ""


# ---------------------------------------------------------------------------
# Main widget
# ---------------------------------------------------------------------------


class A2ABridgeWidget(QWidget):
    """3-pane Qt widget: agents, delegate form, task history.

    Signals:
        task_dispatched: emitted when a new delegation starts.
            Args: task_id (str), agent_name (str), task_excerpt (str)
    """

    task_dispatched = Signal(str, str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        _early_log("a2a_widget:init:entry")
        super().__init__(parent)
        self.setObjectName("a2a_bridge_widget")

        # One dispatcher shared across this widget's lifetime. The
        # registry is cached on first ``discover()`` so the agents
        # list only does the heavy work once.  The dispatcher
        # construction itself is cheap (no subprocess bridge, no
        # ``A2AClient`` yet — those are lazy in the dispatcher).
        _early_log("a2a_widget:dispatcher:entry")
        self._dispatcher = A2ADispatcher()
        _early_log("a2a_widget:dispatcher_created")

        # task_id → _HistoryRow (only main thread reads/writes)
        self._history: dict[str, _HistoryRow] = {}

        # task_id → _A2ATaskRunner. The runner owns its own
        # background thread + cancel event + event queue. We hold
        # a strong reference so the runner isn't GC'd mid-run.
        self._inflight: dict[str, _A2ATaskRunner] = {}

        # Fallback list for ``_target_combo_agents`` lookup when the
        # qt_stubs don't support userData on QComboBox.addItem. Reset
        # on every refresh so stale entries don't survive across
        # agent-list rebuilds.
        self._target_combo_agents: list[Any] = []

        # Poll timer — started on first dispatch, stopped when the
        # last inflight task completes. Runs on the GUI thread and
        # is the only path that touches Qt widgets from runner output.
        _early_log("a2a_widget:timer:entry")
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(_POLL_INTERVAL_MS)
        _safe_connect(self._poll_timer, "timeout", self._poll_task_events)
        _early_log("a2a_widget:timer_created")

        # Top-level layout
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(8)

        _early_log("a2a_widget:agents_pane:entry")
        root.addWidget(self._build_agents_pane(), stretch=1)
        _early_log("a2a_widget:agents_pane:done")
        _early_log("a2a_widget:delegate_pane:entry")
        root.addWidget(self._build_delegate_pane(), stretch=2)
        _early_log("a2a_widget:delegate_pane:done")
        _early_log("a2a_widget:history_pane:entry")
        root.addWidget(self._build_history_pane(), stretch=3)
        _early_log("a2a_widget:history_pane:done")

        # Defer agent discovery: the previous version scheduled
        # ``QTimer.singleShot(0, self._refresh_agents)`` here, which
        # ran A2A discovery (PATH checks for ``claude`` / ``codex``,
        # ``orchestra.toml`` load, optional live HTTP agent-card
        # fetches) on the main thread the moment the A2A tab was
        # first activated.  The widget construction itself now only
        # happens when the user selects the A2A tab (see
        # ``panel_core._ensure_a2a_tab_initialized``), and we don't
        # auto-discover even then — the user clicks Refresh.
        # ``_agent_count_label`` shows 0 and the Send button stays
        # disabled until a real refresh fills the list.
        self._agent_count_label.setText("0 agents")
        self._send_btn.setEnabled(False)
        _early_log("a2a_widget:init:done")

    # -- Pane builders -------------------------------------------------------

    def _build_agents_pane(self) -> QGroupBox:
        """Top: list of discovered external agents + refresh button."""
        box = QGroupBox("Available Agents")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        self._agent_list = QListWidget()
        self._agent_list.setAlternatingRowColors(True)
        # ``QListWidget`` is a subclass of ``QAbstractItemView``; we
        # reference the enum via the base class because the qt_stubs
        # attach ``SelectionMode`` there (consistent with agent_tree.py
        # and bulk_renamer.py).
        self._agent_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        _safe_connect(self._agent_list, "itemDoubleClicked", self._on_agent_double_clicked)
        layout.addWidget(self._agent_list, 1)

        bar = QHBoxLayout()
        self._refresh_agents_btn = QPushButton("Refresh")
        self._refresh_agents_btn.setToolTip("Re-scan PATH for claude / codex and reload orchestra.toml")
        _safe_connect(self._refresh_agents_btn, "clicked", self._refresh_agents)
        bar.addWidget(self._refresh_agents_btn)
        self._agent_count_label = QLabel("0 agents")
        # Right-align the count label so it pushes against the
        # right edge of the agents pane.
        self._agent_count_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        bar.addWidget(self._agent_count_label, 1)
        layout.addLayout(bar)
        return box

    def _build_delegate_pane(self) -> QGroupBox:
        """Middle: target agent selector + task text + context toggle + send."""
        box = QGroupBox("Delegate Task")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # Agent picker
        agent_row = QHBoxLayout()
        agent_row.addWidget(QLabel("Target:"))
        self._target_combo = QComboBox()
        # ``setSizePolicy`` isn't in the qt_stubs; use _safe_call to
        # skip in test environments.
        _safe_call(
            self._target_combo,
            "setSizePolicy",
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        agent_row.addWidget(self._target_combo, 1)
        layout.addLayout(agent_row)

        # Task text
        self._task_edit = QPlainTextEdit()
        self._task_edit.setPlaceholderText(
            "Describe what the external agent should do. Be specific — the external agent has no Luc Nhan tool access."
        )
        self._task_edit.setMinimumHeight(80)
        layout.addWidget(self._task_edit, 1)

        # Options
        opt_row = QHBoxLayout()
        self._include_context_check = QCheckBox("Include current binary context")
        # Disabled: the widget used to forward a "not implemented"
        # placeholder string instead of real binary context, which
        # silently misled users (the external agent answered with no
        # context). Leave the control visible-but-off with an honest
        # tooltip until the dispatcher path that injects real context
        # is wired in. See C5 / Phase 2.
        self._include_context_check.setEnabled(False)
        self._include_context_check.setChecked(False)
        self._include_context_check.setToolTip(
            "Not available yet — binary-context injection for external agents "
            "is planned for a future release. Describe the relevant context in "
            "the task text for now."
        )
        opt_row.addWidget(self._include_context_check)
        opt_row.addStretch(1)
        self._send_btn = QPushButton("Send Task")
        self._send_btn.setDefault(True)
        _safe_connect(self._send_btn, "clicked", self._on_send_clicked)
        opt_row.addWidget(self._send_btn)
        layout.addLayout(opt_row)
        return box

    def _build_history_pane(self) -> QGroupBox:
        """Bottom: history of past + in-flight delegations."""
        box = QGroupBox("Task History")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        self._history_table = QTableWidget(0, 5)
        self._history_table.setHorizontalHeaderLabels(["Time", "Agent", "Task", "Status", "Result"])
        # Sanity guard: ``_COL_*`` constants and the header order are
        # now declared in one block above; if anyone reorders this
        # header literal and forgets to update the constants, the
        # column count won't match and the assertion catches it on
        # widget construction (instead of an IndexError hours later
        # in the poll loop).
        assert self._history_table.columnCount() == 5
        # Same QAbstractItemView-via-base-class trick for the
        # ``SelectionBehavior`` and ``EditTrigger`` enums. The qt_stubs
        # only attach them to the base class.
        self._history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._history_table.verticalHeader().setVisible(False)
        # Stretch the Result column so long outputs are readable.
        header = self._history_table.horizontalHeader()
        # Use _safe_call because QHeaderView.ResizeMode is not in the
        # qt_stubs. In real Qt this sets the column to ResizeToContents
        # (0) or Stretch (1).
        for col in range(5):
            mode = 1 if col in (2, 4) else 0  # Stretch for Task/Result
            _safe_call(header, "setSectionResizeMode", col, mode)
        layout.addWidget(self._history_table, 1)

        bar = QHBoxLayout()
        self._cancel_btn = QPushButton("Cancel Selected")
        self._cancel_btn.setEnabled(False)
        _safe_connect(self._cancel_btn, "clicked", self._on_cancel_clicked)
        bar.addWidget(self._cancel_btn)
        self._view_output_btn = QPushButton("View Output")
        self._view_output_btn.setEnabled(False)
        _safe_connect(self._view_output_btn, "clicked", self._on_view_output_clicked)
        bar.addWidget(self._view_output_btn)
        bar.addStretch(1)
        self._clear_history_btn = QPushButton("Clear")
        _safe_connect(self._clear_history_btn, "clicked", self._on_clear_history_clicked)
        bar.addWidget(self._clear_history_btn)
        layout.addLayout(bar)
        return box

    # -- Agent list management -----------------------------------------------

    def _refresh_agents(self) -> None:
        """Re-run discovery and rebuild the agent list widgets.

        Bail out silently if the underlying Qt widgets were already deleted
        (e.g. a PaletteChange arriving after the panel was torn down) —
        Shiboken raises ``RuntimeError`` on access to a deleted C++ object.
        """
        try:
            self._agent_list.clear()
            self._target_combo.clear()
        except RuntimeError:
            return
        # Reset the stub-fallback list so stale entries from a prior
        # discovery don't leak into the rebuilt combo.  Real Qt
        # (``addItem(label, userData=agent)``) does not need this,
        # but keeping the list empty + repopulating it below also
        # works for the stub path.
        self._target_combo_agents = []
        agents = self._dispatcher.discover()
        for agent in agents:
            label = f"{agent.name}  ({agent.transport})"
            list_item = QListWidgetItem(label)
            list_item.setData(Qt.ItemDataRole.UserRole, agent)  # stash the config
            self._agent_list.addItem(list_item)
            # ``addItem(label, userData=agent)`` is the Qt 5+ form.
            # The qt_stubs only support the no-kwarg form, so use
            # the positional variant and ``setData`` to attach the
            # agent config to the item afterward.
            try:
                self._target_combo.addItem(label, agent)
            except TypeError:
                self._target_combo.addItem(label)
                # Attach the agent to the last-added item via
                # Qt.UserRole. The combo's ``itemData(i)`` won't
                # return it under this fallback — use
                # ``_target_combo_agents[i]`` for lookup. (See
                # _lookup_target_agent.)  The list is initialised
                # in ``__init__`` and reset by ``_refresh_agents``
                # so we just append.
                self._target_combo_agents.append(agent)

        count = len(agents)
        self._agent_count_label.setText(f"{count} agent{'s' if count != 1 else ''}")
        self._send_btn.setEnabled(count > 0)

    def _on_agent_double_clicked(self, item: QListWidgetItem) -> None:
        """Double-click selects the same agent in the delegate combo."""
        agent = item.data(Qt.ItemDataRole.UserRole)
        if agent is None:
            return
        for i in range(self._target_combo.count()):
            if self._lookup_target_agent(i) is agent:
                self._target_combo.setCurrentIndex(i)
                return

    def _lookup_target_agent(self, index: int | None = None) -> Any:
        """Return the ExternalAgentConfig for the selected (or given) combo row.

        Tries the Qt-native ``currentData``/``itemData`` first
        (real Qt path). Falls back to ``_target_combo_agents`` if
        the qt_stubs don't support userData on QComboBox.addItem.
        Returns ``None`` for an empty combo.
        """
        # First try the stub-fallback list which we maintain
        # ourselves. This works in BOTH real Qt and stubbed env
        # because we populate it in _refresh_agents as a safety net.
        agents = getattr(self, "_target_combo_agents", [])
        if agents:
            if index is not None:
                if 0 <= index < len(agents):
                    return agents[index]
                return None
            # Selected index
            i = self._safe_int(self._target_combo.currentIndex(), default=0)
            if 0 <= i < len(agents):
                return agents[i]
            return None
        # No fallback list — try real Qt
        count = self._safe_int(getattr(self._target_combo, "count", lambda: 0)(), default=-1)
        if count <= 0:
            return None
        i = index if index is not None else self._safe_int(self._target_combo.currentIndex(), default=0)
        if i < 0 or i >= count:
            return None
        try:
            return self._target_combo.itemData(i)
        except (TypeError, AttributeError):
            return None

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        """Coerce a value to int, falling back if it's a MagicMock or non-int."""
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return default

    # -- Send / cancel handlers ---------------------------------------------

    def _on_send_clicked(self) -> None:
        """Spawn a runner thread for the selected agent + task."""
        agent = self._lookup_target_agent()
        task_text = self._task_edit.toPlainText().strip()
        if agent is None:
            return
        if not task_text:
            self._task_edit.setFocus()
            return

        # Binary-context inclusion is disabled in the UI (checkbox is
        # off + unclickable) until the dispatcher path that injects
        # real context is wired in. Forwarding the checkbox state here
        # would previously send a misleading "not implemented"
        # placeholder to the external agent; we send no prefix until
        # the feature ships.
        context_prefix = ""

        task_id = uuid.uuid4().hex[:12]
        row = _HistoryRow(
            task_id=task_id,
            agent_name=agent.name,
            task_excerpt=task_text[:60] + ("…" if len(task_text) > 60 else ""),
        )
        self._history[task_id] = row
        self._append_history_row(row)

        cancel_event = threading.Event()
        runner = _A2ATaskRunner(
            self._dispatcher,
            agent.name,
            task_text,
            context_prefix,
            cancel_event,
            task_id,
        )
        self._inflight[task_id] = runner
        runner.start()

        # Start the poll timer if this is the first inflight task.
        # We keep the timer running while at least one runner is
        # alive and stop it when the last runner drains, so a
        # long-lived widget doesn't waste a wakeup every 100ms
        # for no work.
        if not self._safe_int(
            getattr(self._poll_timer, "isActive", lambda: False)(),
            default=0,
        ):
            _safe_call(self._poll_timer, "start")

        self.task_dispatched.emit(task_id, agent.name, row.task_excerpt)

    def _on_cancel_clicked(self) -> None:
        """Cancel the currently selected in-flight task."""
        row_idx = self._history_table.currentRow()
        if row_idx < 0:
            return
        task_id_item = self._history_table.item(row_idx, _COL_TIME)
        if task_id_item is None:
            return
        # We stashed the task_id in the first cell's UserRole+1 slot.
        # See ``_append_history_row``.
        task_id = task_id_item.data(_TASK_ID_ROLE)
        runner = self._inflight.get(task_id)
        if runner is None:
            return
        # The cancel event is shared with the dispatcher. Setting it
        # signals the subprocess / HTTP loop to abort on the next
        # checkpoint. The runner will emit a ``CANCELLED`` event which
        # the poll timer will route to ``_on_task_cancelled``.
        runner.cancel()

    def _on_view_output_clicked(self) -> None:
        """Open a read-only dialog with the selected row's full output."""
        row_idx = self._history_table.currentRow()
        if row_idx < 0:
            return
        task_id = self._history_table.item(row_idx, _COL_TIME).data(_TASK_ID_ROLE)
        row = self._history.get(task_id)
        if row is None:
            return
        # Try the rich dialog first; fall back to a plain message
        # box. ``_OutputDialog`` lives in message_widgets and is the
        # same widget the chat view uses for tool results, so output
        # formatting is consistent across the app.
        try:
            from .message_widgets import _OutputDialog  # local import to avoid cycle

            dlg = _OutputDialog(row.agent_name, row.task_excerpt, row.result_text, self)
            dlg.exec()
            return
        except Exception as exc:
            logger.debug("A2A output: _OutputDialog failed", exc_info=exc)
        try:
            box = QMessageBox(self)
            box.setWindowTitle(f"{row.agent_name} — output")
            box.setText(row.result_text or row.error_text or "(no output)")
            box.exec()
        except Exception as exc:
            logger.debug("A2A output: QMessageBox fallback failed", exc_info=exc)

    def _on_clear_history_clicked(self) -> None:
        """Drop completed/failed rows from the table. In-flight rows stay."""
        for task_id in list(self._history.keys()):
            if task_id not in self._inflight:
                del self._history[task_id]
        self._rebuild_history_table()

    # -- Worker signal handlers (main thread) --------------------------------

    def _on_task_started(self, task_id: str, agent_name: str) -> None:
        row = self._history.get(task_id)
        if row is None:
            return
        row.status = _HistoryStatus.RUNNING
        self._update_row_status(row)

    def _on_task_output(self, task_id: str, text: str) -> None:
        """Append a chunk of output to the row's result text + result column."""
        row = self._history.get(task_id)
        if row is None:
            return
        row.result_text += text
        self._set_result_cell(row)

    def _on_task_completed(self, task_id: str, result_text: str) -> None:
        """Task finished successfully. ``result_text`` is the dispatcher's
        generator return value, which equals the text of the last
        ``TEXT_DELTA`` for both subprocess and A2A transports."""
        row = self._history.get(task_id)
        if row is None:
            return
        row.status = _HistoryStatus.COMPLETED
        # The streamed ``TEXT_DELTA`` events have already been appended
        # to ``row.result_text`` by ``_on_task_output`` and contain the
        # full output (raw stdout lines + the final JSON-parsed
        # result for the subprocess path). Unconditionally overwriting
        # with ``result_text`` would discard the prior streaming and
        # regress the pre-fix behavior. Only adopt ``result_text``
        # when nothing was streamed (defensive — the dispatcher
        # normally yields at least one ``TEXT_DELTA``).
        if result_text and not row.result_text:
            row.result_text = result_text
            self._set_result_cell(row)
        self._update_row_status(row)
        # The cancel button must be re-evaluated — the task is no
        # longer cancellable.
        self._refresh_button_states()

    def _on_task_failed(self, task_id: str, error: str) -> None:
        row = self._history.get(task_id)
        if row is None:
            return
        row.status = _HistoryStatus.FAILED
        row.error_text = error
        row.result_text = error
        self._update_row_status(row)
        self._refresh_button_states()

    def _on_task_cancelled(self, task_id: str) -> None:
        row = self._history.get(task_id)
        if row is None:
            return
        row.status = _HistoryStatus.CANCELLED
        # The cancel reason is whatever the dispatcher said; if the
        # user just hit Cancel, ``error`` is usually empty. Stash
        # the dispatcher message either way so View Output shows
        # the most informative string we have.
        row.error_text = ""  # clear; cancellation isn't a hard error
        self._update_row_status(row)
        self._refresh_button_states()

    def _poll_task_events(self) -> None:
        """Drain every inflight runner's event queue (GUI thread).

        Called by ``self._poll_timer`` every ``_POLL_INTERVAL_MS``.
        Bounded per-tick drain keeps the UI responsive even under a
        bursty stream. Terminal events remove the runner from
        ``_inflight``; the timer is stopped when the last runner
        finishes so the widget doesn't burn CPU on an idle queue.
        """
        for task_id in list(self._inflight.keys()):
            runner = self._inflight.get(task_id)
            if runner is None:
                continue
            for _ in range(_MAX_EVENTS_PER_TICK):
                try:
                    event = runner.queue.get_nowait()
                except queue.Empty:
                    break
                self._dispatch_runner_event(event, task_id)
                if event.type in _TERMINAL_RUNNER_EVENTS:
                    # The runner is done — drop the reference and
                    # stop iterating its queue. Other runners are
                    # drained on the next tick.
                    self._inflight.pop(task_id, None)
                    break

        if not self._inflight:
            _safe_call(self._poll_timer, "stop")

    def _dispatch_runner_event(self, event: _A2ATaskEvent, task_id: str) -> None:
        """Route one queued event to the appropriate UI handler."""
        et = event.type
        if et == _A2ARunnerEventType.STARTED:
            self._on_task_started(task_id, event.text or "")
        elif et == _A2ARunnerEventType.OUTPUT:
            self._on_task_output(task_id, event.text)
        elif et == _A2ARunnerEventType.COMPLETED:
            self._on_task_completed(task_id, event.text)
        elif et == _A2ARunnerEventType.FAILED:
            self._on_task_failed(task_id, event.text or "External agent error")
        elif et == _A2ARunnerEventType.CANCELLED:
            self._on_task_cancelled(task_id)

    # -- Table helpers -------------------------------------------------------

    def _append_history_row(self, row: _HistoryRow) -> None:
        """Insert a new row at the bottom. Stash task_id in cell 0's UserRole+1."""
        self._history_table.insertRow(self._history_table.rowCount())
        row_idx = self._history_table.rowCount() - 1
        # Time column shows wall clock + stashes task_id in
        # ``_TASK_ID_ROLE`` (= UserRole + 1) so cancel / view
        # handlers can find the row.
        time_item = QTableWidgetItem(time.strftime("%H:%M:%S", time.localtime(row.started_at)))
        time_item.setData(_TASK_ID_ROLE, row.task_id)
        self._history_table.setItem(row_idx, _COL_TIME, time_item)
        self._history_table.setItem(row_idx, _COL_AGENT, QTableWidgetItem(row.agent_name))
        self._history_table.setItem(row_idx, _COL_TASK, QTableWidgetItem(row.task_excerpt))
        self._history_table.setItem(row_idx, _COL_STATUS, QTableWidgetItem(row.status.value))
        self._history_table.setItem(row_idx, _COL_RESULT, QTableWidgetItem(""))

    def _set_result_cell(self, row: _HistoryRow) -> None:
        """Update only the result column for ``row`` (avoids scroll jumping)."""
        row_idx = self._find_row_index(row.task_id)
        if row_idx < 0:
            return
        item = self._history_table.item(row_idx, _COL_RESULT)
        if item is not None:
            item.setText(self._format_result_excerpt(row.result_text))

    def _update_row_status(self, row: _HistoryRow) -> None:
        row_idx = self._find_row_index(row.task_id)
        if row_idx < 0:
            return
        status_item = self._history_table.item(row_idx, _COL_STATUS)
        # ``row.status`` is a ``_HistoryStatus`` enum; ``.value`` gives
        # the raw string the user sees in the cell.
        status_item.setText(row.status.value)
        # Light status coloring: green/grey/red. Wrapped in try/except
        # so the stub environment (which doesn't have QColor) doesn't
        # break the test.
        try:
            if row.status == _HistoryStatus.COMPLETED:
                status_item.setForeground(QColor("#3fb950"))
            elif row.status == _HistoryStatus.FAILED:
                status_item.setForeground(QColor("#f85149"))
            elif row.status == _HistoryStatus.CANCELLED:
                status_item.setForeground(QColor("#d29922"))
            else:
                status_item.setForeground(QColor("#8b949e"))
        except Exception as exc:
            logger.debug("A2A history row colorize failed", exc_info=exc)

    def _find_row_index(self, task_id: str) -> int:
        # ``rowCount`` may return a MagicMock in tests; coerce to int
        # and clamp at 0 to avoid TypeError on ``range()``.
        n = self._safe_int(self._history_table.rowCount(), default=0)
        for i in range(n):
            item = self._history_table.item(i, _COL_TIME)
            if item is not None and item.data(_TASK_ID_ROLE) == task_id:
                return i
        return -1

    def _format_result_excerpt(self, text: str) -> str:
        """Last 60 chars of the result text for the table cell."""
        text = text.strip()
        if len(text) <= 60:
            return text
        return "…" + text[-57:]

    def _rebuild_history_table(self) -> None:
        """Clear the table and re-add all rows from ``_history``."""
        self._history_table.setRowCount(0)
        for row in self._history.values():
            self._append_history_row(row)

    def _refresh_button_states(self) -> None:
        """Enable cancel only if there's an in-flight task selected."""
        row_idx = self._safe_int(self._history_table.currentRow(), default=-1)
        if row_idx < 0:
            self._cancel_btn.setEnabled(False)
            self._view_output_btn.setEnabled(False)
            return
        task_id = self._history_table.item(row_idx, _COL_TIME).data(_TASK_ID_ROLE)
        in_flight = task_id in self._inflight
        self._cancel_btn.setEnabled(in_flight)
        self._view_output_btn.setEnabled(task_id in self._history)

    def changeEvent(self, event: QEvent) -> None:
        """Re-render agent list when the host theme flips — keeps colours consistent."""
        if event.type() == QEvent.Type.PaletteChange:
            self._refresh_agents()
        super().changeEvent(event)

    # -- Cleanup ------------------------------------------------------------

    def closeEvent(self, event: Any) -> None:
        """Last-resort cleanup hook when the host window closes.

        Panel teardown normally propagates ``shutdown()`` explicitly
        from ``LucNhanPanelCore.shutdown()`` or
        ``ToolsPanel._replace_tab``, but if the host window is closed
        by the OS or Qt directly, ``closeEvent`` is the only hook we
        get. ``shutdown()`` is idempotent so calling it from both
        paths is safe.
        """
        self.shutdown()
        super().closeEvent(event)

    def shutdown(self) -> None:
        """Cancel all in-flight tasks and wait briefly for threads to exit.

        Order of operations matters:
          1. Stop the poll timer first so the GUI thread doesn't race
             with our cleanup (e.g. by mutating ``_inflight`` while we
             iterate it).
          2. Cancel every runner via its public ``cancel()`` so the
             dispatcher's subprocess / HTTP loops notice on their
             next checkpoint.
          3. ``join`` each runner briefly, then ONLY remove it from
             ``_inflight`` once the thread is actually dead. A runner
             whose thread is still alive (slow subprocess shutdown,
             stuck HTTP retry) stays in ``_inflight``; the daemon
             flag on the thread keeps the process exit clean, and we
             at least don't *hide* a live worker from the registry.
        """
        _safe_call(self._poll_timer, "stop")
        for runner in self._inflight.values():
            runner.cancel()
        for task_id, runner in list(self._inflight.items()):
            runner.join(2.0)  # 2s grace
            if not runner.is_alive():
                # Thread exited within the grace window — safe to drop.
                self._inflight.pop(task_id, None)
            # else: leave the runner registered. The daemon thread
            # won't block process exit and the dispatcher's cancel
            # polling will still drain it on its own clock.


__all__ = ["A2ABridgeWidget"]


def _safe_connect(signal_owner: Any, signal_name: str, slot: Any) -> bool:
    """Connect a Qt signal if it exists; silently no-op otherwise.

    The qt_stubs used in unit tests do not implement Qt signals
    (no ``itemDoubleClicked`` on QListWidget, no ``valueChanged`` on
    QSpinBox, etc.). Wrapping every ``.connect`` in a try/except
    would clutter the widget; this helper keeps the call sites clean
    while letting the widget still build in stubbed environments.
    Returns True if the connection was made, False if the signal
    was missing.
    """
    try:
        signal = getattr(signal_owner, signal_name)
    except AttributeError:
        return False
    try:
        signal.connect(slot)
        return True
    except Exception:
        return False


def _safe_call(obj: Any, method_name: str, *args: Any, **kwargs: Any) -> bool:
    """Call a Qt method if it exists; silently no-op otherwise.

    The qt_stubs only implement a subset of the real Qt API
    (e.g. ``QComboBox.setSizePolicy`` is missing). Calling these
    methods on a stub raises ``AttributeError``. This helper
    lets the widget degrade gracefully when the method isn't
    available in the test environment.
    """
    try:
        method = getattr(obj, method_name)
    except AttributeError:
        return False
    try:
        method(*args, **kwargs)
        return True
    except Exception:
        return False
