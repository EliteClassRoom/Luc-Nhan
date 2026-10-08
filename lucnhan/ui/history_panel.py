"""Chat History side panel — isolated, passive metadata-only widget.

This widget is intentionally passive (spec §6.3):

* It never imports the chat-history persistence layer, the
  composition seam, threading primitives, executors, file I/O, or any
  other heavyweight seam.
* It exposes a Qt-only main-thread surface — three signals and five
  methods. PanelCore owns the worker, request queue, generation
  counter, and polling timer.
* It accepts a frozen :class:`SessionHistoryEntry` DTO per row and
  never exposes storage internals (paths, manifest keys, memory IDs).
* It must never start threads or perform I/O.

The widget is responsible for:

* Rendering the title + metadata row in plain text (spec §11.3 — never
  interpolate untrusted titles into stylesheets or LLM prompts).
* Case-insensitive title-only search over the cached list (spec §8.4).
  Search runs on the Qt main thread against the last ``set_entries``
  payload only — it never re-queries the persistence layer.
* Visual state transitions: empty / search-empty / loading / error /
  list. Retry is visible only after an explicit ``set_error`` call
  with ``retry_visible=True``.
* Re-rendering on theme changes via the
  :func:`bind_theme`/ :func:`disconnect_theme` helper.

The panel's content is capped at 320 px wide so a long title cannot
overflow horizontally on narrow IDA layouts (the cap is enforced via
``setMaximumWidth`` on every title/meta label and verified by a
deterministic widget-property assertion — see
``tests/ui/test_history_panel.py``).

Reopening behaviour (cached rows survive, query persists across close
+ refresh) is **PanelCore's** concern — this widget only renders what
it was given. ``set_loading`` does NOT clear cached rows because the
spec says "Closing and reopening History preserves the last successful
rows and search query, but reopening always starts a background refresh
so newly saved turns can reorder the list."
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .qt_compat import (
    QAccessible,
    QAccessibleAnnouncementEvent,
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    Qt,
    QVBoxLayout,
    QWidget,
    Signal,
)
from .styles import (
    get_history_close_btn_style,
    get_history_delete_btn_style,
    get_history_meta_style,
    get_history_panel_style,
    get_history_retry_btn_style,
    get_history_row_style,
    get_history_scope_style,
    get_history_search_style,
    get_history_status_style,
    get_history_title_style,
    maybe_host_stylesheet,
)
from .theme.applicator import bind_theme, disconnect_theme

if TYPE_CHECKING:
    from ..state.history_types import SessionHistoryEntry


# Maximum panel content width. The panel caps itself + every title/meta
# label at this width so a long title cannot overflow horizontally on
# narrow IDA layouts (the spec lists 320 px as the reference width).
HISTORY_PANEL_CONTENT_MAX_WIDTH = 320
# Panel-level padding (left + right) consumed by the root QVBoxLayout.
_HISTORY_PANEL_PADDING = 16
# Row-level horizontal inset (left + right padding inside each row).
_HISTORY_ROW_HORIZONTAL_INSET = 12
# Computed cap for the row's title and meta labels. The cap is the
# panel width minus the panel padding minus the row inset.
_HISTORY_LABEL_MAX_WIDTH = HISTORY_PANEL_CONTENT_MAX_WIDTH - _HISTORY_PANEL_PADDING - _HISTORY_ROW_HORIZONTAL_INSET
# Separator used in the meta line.
_HISTORY_META_SEPARATOR = "  ·  "

# Empty / search-empty / loading / error copy strings (spec §13). These
# are the exact strings the test suite asserts, and they are referenced
# only by the public methods so a typo here is caught at test time.
_EMPTY_COPY = "No saved chats for this IDB yet."
_SEARCH_EMPTY_COPY = "No chats match your search."
_LOADING_COPY = "Loading chats…"


# Stacked-state identifiers (used internally for state tracking — not
# part of the public surface).
_STATE_STATUS = "status"  # empty / search-empty / error
_STATE_LOADING = "loading"
_STATE_LIST = "list"


def _current_focused_widget() -> object | None:
    """Return the widget that currently has keyboard focus, or ``None``.

    The production path uses ``QApplication.focusWidget()``; the test
    path falls back to scanning row delete buttons for the stub's
    ``_has_focus`` marker when no event loop is present. The two
    paths converge on the same contract: a widget instance with
    keyboard focus, or ``None`` if focus is elsewhere / unset.
    """
    try:
        if QApplication.instance() is not None:
            return QApplication.focusWidget()
    except Exception:
        pass
    return None


def _format_updated_at(updated_at: float) -> str:
    """Format the file mtime as a short, locale-independent timestamp."""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(updated_at))


def _format_message_count(message_count: int) -> str:
    """Format the message-count suffix."""
    if message_count == 1:
        return "1 message"
    return f"{message_count} messages"


def _format_provider_model(provider: str, model: str) -> str:
    """Format the provider/model footer for the meta line."""
    parts = [p for p in (provider, model) if p]
    return _HISTORY_META_SEPARATOR.join(parts)


class HistoryRowWidget(QFrame):
    """Single metadata row: title + meta line. Plain-text only.

    The title is forced to ``Qt.TextFormat.PlainText`` so the untrusted
    storage-derived string can never render as rich HTML (spec §11.3).
    The label is word-wrapped and capped at
    :data:`_HISTORY_LABEL_MAX_WIDTH` so the row never produces a
    horizontal scrollbar at the reference 320 px panel width.
    """

    session_open_requested = Signal(str)  # emits the row's session_id
    session_delete_requested = Signal(str, str)  # (session_id, title)

    def __init__(
        self,
        entry: SessionHistoryEntry,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("history_row")
        self.setStyleSheet(get_history_row_style())
        self._entry = entry
        # Operations (row-open + delete) are enabled by default. The
        # panel toggles this when a delete is pending so the user cannot
        # race a delete-in-flight with an unintended open/delete on the
        # same row.
        self._operations_enabled = True

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(8)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)

        # Title: plain text only, word-wrap, capped width. Storage
        # boundary already sanitizes the title through
        # ``derive_history_title`` (spec §9.1), so it is safe to
        # display directly without further escaping.
        self._title = QLabel(entry.title)
        self._title.setTextFormat(Qt.TextFormat.PlainText)
        self._title.setWordWrap(True)
        self._title.setMaximumWidth(_HISTORY_LABEL_MAX_WIDTH)
        self._title.setStyleSheet(get_history_title_style())
        text_col.addWidget(self._title)

        # Meta line: timestamp · provider/model · message count. We
        # build the segments deterministically — empty fields are
        # dropped so a legacy row without provider info still renders
        # the timestamp.
        meta_bits = [
            _format_updated_at(entry.updated_at),
            _format_provider_model(entry.provider, entry.model),
            _format_message_count(entry.message_count),
        ]
        meta_text = _HISTORY_META_SEPARATOR.join(part for part in meta_bits if part)
        self._meta = QLabel(meta_text)
        self._meta.setTextFormat(Qt.TextFormat.PlainText)
        self._meta.setWordWrap(True)
        self._meta.setMaximumWidth(_HISTORY_LABEL_MAX_WIDTH)
        self._meta.setStyleSheet(get_history_meta_style())
        text_col.addWidget(self._meta)

        layout.addLayout(text_col, 1)

        # Delete affordance (Task 4). The button is a child QPushButton
        # so Qt consumes the mouse release itself — ``mouseReleaseEvent``
        # is therefore NOT called when the user clicks the button, which
        # is exactly what we want (no bubble-up to ``session_open_requested``).
        self._delete_btn = QPushButton("×")  # noqa: RUF001 — brief-specified glyph
        self._delete_btn.setObjectName("history_delete_btn")
        self._delete_btn.setToolTip("Delete chat")
        self._delete_btn.setAccessibleName(f"Delete chat: {entry.title}")
        # StrongFocus lets keyboard users reach the button via Tab and
        # activate it via Space/Return. The default button policy is
        # NoFocus which would hide the button from the focus chain.
        self._delete_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # WCAG 2.5.5 target size minimum (24x24). The token-driven QSS
        # controls the visual size for sighted users; ``setMinimumSize``
        # guarantees a 24x24 hit target regardless of the token palette
        # so motor-impaired users can still click the button.
        self._delete_btn.setMinimumSize(24, 24)
        self._delete_btn.setStyleSheet(get_history_delete_btn_style())
        self._delete_btn.clicked.connect(lambda: self.session_delete_requested.emit(entry.session_id, entry.title))
        layout.addWidget(self._delete_btn)

    @property
    def entry(self) -> SessionHistoryEntry:
        return self._entry

    def set_operation_enabled(self, enabled: bool) -> None:
        """Toggle row-open + delete affordances without touching search.

        Called by ``HistoryPanel.set_operation_pending`` so an
        in-flight delete disables its own row's controls (and any other
        row's open intent) while preserving the search box. The panel
        is the single owner of this state — row tests verify the
        default ``True`` and the disabled state via the panel API.
        """
        self._operations_enabled = bool(enabled)
        # ``setEnabled`` carries the same flag to Qt so the button
        # visually dims and refuses mouse clicks while pending.
        self._delete_btn.setEnabled(self._operations_enabled)

    def mouseReleaseEvent(self, event: object) -> None:
        """Forward a row click to ``session_open_requested``.

        Implemented as a class method (not via instance attribute
        assignment) so the dispatcher is part of the widget's stable
        API surface and survives ``PySide6``'s Shiboken dispatch path.
        A click anywhere in the row fires the open request; the
        forwarded ``session_id`` comes from the row's bound entry
        so the panel's ``session_open_requested(str)`` carries the
        right value.

        Disabled while an operation is pending so the user cannot race
        a delete-in-flight with an open on the same row. A child
        QPushButton consumes its own mouse release, so this handler is
        only invoked for clicks on the row body / text column.
        """
        if not self._operations_enabled:
            return
        self.session_open_requested.emit(self._entry.session_id)

    def shutdown(self) -> None:
        """Detach the theme subscription (idempotent)."""
        disconnect_theme(self)

    def _apply_styles(self, _tokens: object = None) -> None:
        """Re-apply per-row styles from the live tokens."""
        if getattr(self, "_title", None) is not None:
            self._title.setStyleSheet(get_history_title_style())
        if getattr(self, "_meta", None) is not None:
            self._meta.setStyleSheet(get_history_meta_style())
        if getattr(self, "_delete_btn", None) is not None:
            self._delete_btn.setStyleSheet(get_history_delete_btn_style())
        self.setStyleSheet(get_history_row_style())


class HistoryPanel(QFrame):
    """Side panel listing past chat sessions for the current IDB.

    The panel is intentionally passive: it never imports the persistence
    layer, config, threading, executors, or I/O. PanelCore owns the
    worker, request queue, generation counter, and polling timer.

    Public surface (consumed by Tasks 8-10):

    Signals:
      * ``session_open_requested(session_id: str)`` — fired on row click.
      * ``session_delete_requested(session_id: str, title: str)`` —
        fired on the row delete button.
      * ``close_requested()`` — fired on the header close button.
      * ``retry_requested()`` — fired on the Retry button.
      * ``notice_dismissed()`` — fired on the notice row's dismiss button.

    Methods:
      * ``set_entries(entries)`` — replace cached list + re-render.
      * ``set_loading()`` — show loading state, preserve cache.
      * ``set_error(message, retry_visible=True)`` — show error state.
      * ``clear()`` — reset cached entries AND the search query.
      * ``remove_entry(session_id)`` — drop one row, preserve query.
      * ``set_operation_pending(session_id)`` — disable row operations.
      * ``show_notice(message, retry_visible, dismiss_visible)`` —
        surface a transient error/info row.
      * ``clear_notice()`` — hide the notice row.
      * ``shutdown()`` — disconnect theme subscriptions (idempotent).
      * ``visible_session_ids()`` — ids of rows currently rendered.
    """

    session_open_requested = Signal(str)
    session_delete_requested = Signal(str, str)
    close_requested = Signal()
    retry_requested = Signal()
    notice_dismissed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("history_panel")
        # Cap the panel's own width so the splitter cannot grant it
        # more than 320 px even if the parent layout grows. The row
        # labels carry their own matching caps below.
        self.setMaximumWidth(HISTORY_PANEL_CONTENT_MAX_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.setStyleSheet(get_history_panel_style())

        # Cached state.
        self._entries: tuple[SessionHistoryEntry, ...] = ()
        self._row_widgets: list[HistoryRowWidget] = []
        self._state: str = _STATE_STATUS
        # When a delete is in flight, ``_pending_session_id`` names the
        # row whose row-open + delete must be disabled. ``None`` means
        # no pending operation. The panel is the single owner of this
        # state — ``HistoryRowWidget.set_operation_enabled`` is the
        # only downstream consumer.
        self._pending_session_id: str | None = None

        # === Root layout ============================================
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # === Header ==================================================
        self._header = QFrame()
        self._header.setObjectName("history_header")
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(8, 8, 8, 4)
        header_layout.setSpacing(8)

        self._title = QLabel()
        self._title.setText("Chat History")
        self._title.setStyleSheet(get_history_title_style())
        header_layout.addWidget(self._title)

        self._scope_label = QLabel()
        self._scope_label.setText("Current IDB")
        self._scope_label.setStyleSheet(get_history_scope_style())
        header_layout.addWidget(self._scope_label)
        header_layout.addStretch(1)

        self._close_btn = QPushButton()
        self._close_btn.setText("Close")
        self._close_btn.setObjectName("history_close_btn")
        self._close_btn.setStyleSheet(get_history_close_btn_style())
        self._close_btn.clicked.connect(lambda: self.close_requested.emit())
        header_layout.addWidget(self._close_btn)
        main_layout.addWidget(self._header)

        # === Search ==================================================
        self._search = QLineEdit()
        self._search.setObjectName("history_search")
        self._search.setPlaceholderText("Search conversations…")
        self._search.setStyleSheet(get_history_search_style())
        self._search.textChanged.connect(self._on_search_changed)
        main_layout.addWidget(self._search)

        # === Notice (Task 4) ========================================
        # A dedicated row above the stacked states so PanelCore can
        # surface a transient error/info message without flipping the
        # stack back to ``_status_frame`` (which would hide the cached
        # rows). Hidden by default; ``show_notice`` flips it on and
        # ``clear_notice`` / terminal-success paths flip it back.
        self._notice_frame = QFrame()
        self._notice_frame.setObjectName("history_notice")
        notice_layout = QHBoxLayout(self._notice_frame)
        notice_layout.setContentsMargins(8, 6, 8, 6)
        notice_layout.setSpacing(8)
        self._notice_label = QLabel()
        # Plain-text only — internal notice copy never carries user
        # input, but we mirror the row-title guard so the label can
        # never escape into rich text via Qt's auto-format heuristic.
        self._notice_label.setTextFormat(Qt.TextFormat.PlainText)
        self._notice_label.setWordWrap(True)
        self._notice_label.setStyleSheet(get_history_status_style())
        notice_layout.addWidget(self._notice_label, 1)
        self._notice_retry_btn = QPushButton("Retry")
        self._notice_retry_btn.setObjectName("history_notice_retry_btn")
        self._notice_retry_btn.setStyleSheet(get_history_retry_btn_style())
        self._notice_retry_btn.setVisible(False)
        # StrongFocus — keyboard users must be able to Tab into the
        # notice's retry button so a delete failure is recoverable
        # without reaching for the mouse.
        self._notice_retry_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # ``retry_requested`` is a shared signal — the panel re-emits
        # whatever the notice button fires so PanelCore does not need
        # to know whether the user clicked Retry on the status frame or
        # the notice frame.
        self._notice_retry_btn.clicked.connect(lambda: self.retry_requested.emit())
        notice_layout.addWidget(self._notice_retry_btn)
        self._notice_dismiss_btn = QPushButton("Dismiss")
        self._notice_dismiss_btn.setObjectName("history_notice_dismiss_btn")
        self._notice_dismiss_btn.setStyleSheet(get_history_close_btn_style())
        self._notice_dismiss_btn.setVisible(False)
        self._notice_dismiss_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._notice_dismiss_btn.clicked.connect(self._on_notice_dismissed)
        notice_layout.addWidget(self._notice_dismiss_btn)
        self._notice_frame.setVisible(False)
        main_layout.addWidget(self._notice_frame)

        # === Stacked states ==========================================
        self._stack = QStackedWidget()
        self._stack.setObjectName("history_stack")

        # Status states (empty / search-empty / error / loading) share
        # one frame so the visual structure stays stable across states.
        self._status_frame = QFrame()
        self._status_frame.setObjectName("history_status")
        status_layout = QVBoxLayout(self._status_frame)
        status_layout.setContentsMargins(12, 24, 12, 12)
        status_layout.setSpacing(8)
        status_layout.addStretch(1)

        self._status_label = QLabel()
        # Plain-text only — the status message is derived from internal
        # state ("No saved chats…", "Loading chats…", the error string
        # from ``set_error``) so it never carries user-supplied HTML.
        # Forcing PlainText here matches the row-title guard (spec
        # §11.3) and ensures the label is never auto-promoted to rich
        # text by Qt's heuristic.
        self._status_label.setTextFormat(Qt.TextFormat.PlainText)
        self._status_label.setWordWrap(True)
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setStyleSheet(get_history_status_style())
        status_layout.addWidget(self._status_label)

        # Retry row — centered, hidden by default. Visible only after
        # ``set_error(message, retry_visible=True)``.
        retry_row = QHBoxLayout()
        retry_row.setContentsMargins(0, 0, 0, 0)
        retry_row.setSpacing(0)
        retry_row.addStretch(1)
        self._retry_btn = QPushButton()
        self._retry_btn.setText("Retry")
        self._retry_btn.setObjectName("history_retry_btn")
        self._retry_btn.setStyleSheet(get_history_retry_btn_style())
        self._retry_btn.setVisible(False)
        # StrongFocus — keyboard users must be able to Tab to the
        # status-frame retry button after a load failure. Real Qt's
        # QWidget default focus policy is ``NoFocus`` (verified in
        # Qt docs for ``QWidget.focusPolicy``); the explicit pin
        # flips it to ``StrongFocus`` so the button enters the tab
        # chain. The pin makes the contract visible in the test
        # stub (the stub mirrors ``NoFocus`` as the default unless
        # ``setFocusPolicy`` is called).
        self._retry_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._retry_btn.clicked.connect(lambda: self.retry_requested.emit())
        retry_row.addWidget(self._retry_btn)
        retry_row.addStretch(1)
        status_layout.addLayout(retry_row)

        status_layout.addStretch(1)
        self._stack.addWidget(self._status_frame)  # index 0

        # List state — scroll area filled with rows.
        self._list_scroll = QScrollArea()
        self._list_scroll.setObjectName("history_list_scroll")
        self._list_scroll.setWidgetResizable(True)
        # Disable the horizontal scrollbar via the canonical Qt API so
        # narrow panels never widen past HISTORY_PANEL_CONTENT_MAX_WIDTH
        # even if a row's title overflows. The border / background QSS
        # stays separate so the host theme still paints the scroll area.
        self._list_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self._list_widget = QFrame()
        self._list_widget.setObjectName("history_list")
        self._list_layout = QVBoxLayout(self._list_widget)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(0)
        self._list_layout.addStretch(1)
        self._list_scroll.setWidget(self._list_widget)

        list_container = QFrame()
        list_container.setObjectName("history_list_container")
        list_container_layout = QVBoxLayout(list_container)
        list_container_layout.setContentsMargins(8, 4, 8, 4)
        list_container_layout.setSpacing(0)
        list_container_layout.addWidget(self._list_scroll)
        self._stack.addWidget(list_container)  # index 1

        main_layout.addWidget(self._stack, 1)

        # === Initial state ===========================================
        self._set_status_message(_EMPTY_COPY)
        self._stack.setCurrentWidget(self._status_frame)

        # Theme binding: re-apply styles on every ``themeChanged`` emit.
        # ``bind_theme`` runs the callback synchronously so the initial
        # paint reflects the active palette.
        bind_theme(self, self._apply_styles)

    # === Public API ====================================================

    def set_entries(self, entries: list[SessionHistoryEntry]) -> None:
        """Replace the cached entry list and re-render rows.

        ``PanelCore`` is responsible for sorting the entries newest-first
        and applying the current-IDB filter; this widget trusts what it
        was given. Any pending error state is cleared because a fresh
        delivery implies the previous failure has been resolved.
        """
        self._entries = tuple(entries)
        self._retry_btn.setVisible(False)
        # Terminal-success path: a fresh list means the prior failure
        # has been resolved and any leftover notice from an earlier
        # delete/load error is stale. Clear it without emitting
        # ``notice_dismissed`` — the user did not click dismiss.
        self.clear_notice()
        self._apply_search()

    def set_loading(self) -> None:
        """Show the loading state. Cached rows + query are preserved.

        Reopening the panel starts a background refresh; the spec says
        "Closing and reopening History preserves the last successful
        rows and search query, but reopening always starts a background
        refresh so newly saved turns can reorder the list." We do not
        drop the cached rows here — ``PanelCore`` swaps them in via
        ``set_entries`` once the new list arrives.
        """
        self._state = _STATE_LOADING
        self._set_status_message(_LOADING_COPY)
        self._retry_btn.setVisible(False)
        self._stack.setCurrentWidget(self._status_frame)

    def set_error(self, message: str, retry_visible: bool = True) -> None:
        """Show the error state with an optional Retry button.

        The Retry button is rendered only when ``retry_visible=True``;
        passing ``False`` shows the message alone (e.g. while a flush
        is still pending).
        """
        self._state = _STATE_STATUS
        self._set_status_message(message)
        self._retry_btn.setVisible(bool(retry_visible))
        # Hide existing rows so the user does not see stale data behind
        # the error message. They are re-shown when ``set_entries``
        # delivers a new list.
        for row in self._row_widgets:
            row.setVisible(False)
        self._stack.setCurrentWidget(self._status_frame)

    def clear(self) -> None:
        """Reset cached entries AND the search query.

        Used by ``PanelCore`` for explicit resets (IDB switch, initial
        empty state). Unlike ``set_loading``, this empties the rows so
        the next ``set_entries`` paints a clean slate.
        """
        self._entries = ()
        # Reset the search box so a fresh reopen starts empty. We set
        # the text directly; ``textChanged`` fires ``_apply_search``
        # which short-circuits on the empty entries list.
        self._search.setText("")
        self._render_rows([])
        self._state = _STATE_STATUS
        self._set_status_message(_EMPTY_COPY)
        self._retry_btn.setVisible(False)
        self._stack.setCurrentWidget(self._status_frame)
        # Explicit resets clear any leftover notice (e.g. an error
        # row from a previous IDB's delete failure). The dismissal
        # signal is NOT emitted here — clear is a PanelCore-driven
        # reset, not a user acknowledge.
        self.clear_notice()

    def remove_entry(self, session_id: str) -> None:
        """Drop one row from the cached list, preserving query + scroll.

        Called by ``PanelCore`` after a successful delete so the cached
        list mirrors on-disk state. The vertical scrollbar value is
        captured before the rerender and clamped to the new
        ``maximum()`` so a delete at the end of a long list does not
        flash the user back to the top.

        Search query is left untouched — the user typed it, so deleting
        a row should not force a re-typing. ``_apply_search`` re-runs
        against the trimmed list and naturally collapses the visible
        set.

        Focus restoration: when the user deletes a row via the delete
        button, the focused widget is destroyed with the row. The
        method re-points focus to the next surviving row's delete
        button (or the previous one if the deleted row was last, or
        the search box when the list is now empty) so the keyboard
        user keeps a meaningful anchor instead of landing on an
        orphaned focus. The restore is skipped when focus is NOT on
        the deleted row (e.g. ``remove_entry`` called programmatically
        from a worker callback) so callers do not get their focus
        hijacked.
        """
        scrollbar = self._list_scroll.verticalScrollBar()
        old_scroll = scrollbar.value()
        # Capture the focused widget BEFORE the rerender destroys it.
        # If focus is not on any of our delete buttons, this is a
        # programmatic call and we leave focus alone at the end.
        focused_widget = _current_focused_widget()
        if focused_widget is None:
            # Stub fallback (no ``QApplication``): scan row widgets
            # for the stub's ``_has_focus`` marker so unit tests that
            # do not bootstrap a Qt application still exercise the
            # restoration path.
            for row in self._row_widgets:
                if getattr(row._delete_btn, "_has_focus", False):
                    focused_widget = row._delete_btn
                    break
        target_row_index = next(
            (i for i, row in enumerate(self._row_widgets) if row._delete_btn is focused_widget),
            None,
        )
        # The deleted row's display index — same as ``target_row_index``
        # at this point because we have not rerendered yet.
        deleted_index = target_row_index
        self._entries = tuple(entry for entry in self._entries if entry.session_id != session_id)
        self._apply_search()
        # Clamp the pre-delete scroll position against the new maximum
        # so we never set a value larger than the bar reports after the
        # row count shrinks. A real QScrollBar silently clamps on its
        # own, but the stub raises if we exceed ``maximum``.
        scrollbar.setValue(min(old_scroll, scrollbar.maximum()))
        # Restore focus only if the caller was the keyboard user
        # (i.e. focus was on one of the row delete buttons BEFORE
        # the rerender). After the rerender the old widget reference
        # is invalid, so we look at the freshly rebuilt list.
        if deleted_index is None:
            return
        if not self._row_widgets:
            # Last row was deleted — focus moves to the search box so
            # the keyboard user keeps a meaningful anchor.
            self._search.setFocus()
            return
        # Prefer the row that took the deleted row's slot (same
        # index). If we deleted the last row, fall back to the new
        # last row.
        restore_index = min(deleted_index, len(self._row_widgets) - 1)
        self._row_widgets[restore_index]._delete_btn.setFocus()

    def set_operation_pending(self, session_id: str | None) -> None:
        """Disable row-open + delete on every row while an op is pending.

        Pass ``session_id=None`` to clear the pending state and re-enable
        all rows. ``PanelCore`` calls this with the request's
        ``session_id`` when it dispatches a delete (or any future
        per-row operation); the same call with ``None`` is the terminal
        step after the worker's result is processed.

        The search box is intentionally NOT disabled — the user can
        keep filtering the cached rows while a delete is in flight, and
        the spec considers search a presentation concern the panel
        owns independently of the operations it owns.
        """
        self._pending_session_id = session_id
        operations_allowed = session_id is None
        for row in self._row_widgets:
            row.set_operation_enabled(operations_allowed)

    def show_notice(
        self,
        message: str,
        *,
        retry_visible: bool = False,
        dismiss_visible: bool = True,
    ) -> None:
        """Surface a transient notice row above the list / status.

        The notice frame is a dedicated row above ``_stack`` (not a
        status overlay) so it can co-exist with cached rows during a
        partial failure. ``PanelCore`` calls this when a delete or load
        returns ``FAILED`` / ``WRONG_IDB`` / ``NOT_FOUND`` — the row
        stays rendered, the user keeps their scroll position, and the
        dismiss button gives them a way to clear it.

        ``retry_visible=True`` re-uses the panel's ``retry_requested``
        signal; ``dismiss_visible=True`` (default) wires the dismiss
        button to ``notice_dismissed``.

        On display the panel publishes a ``QAccessibleAnnouncementEvent``
        with ``message`` so a screen reader announces the new error to
        the user. ``clear_notice`` does NOT re-announce — terminal
        success paths must not echo the same text back to the user
        after they have already acknowledged the failure.
        """
        self._notice_label.setText(message)
        self._notice_retry_btn.setVisible(bool(retry_visible))
        self._notice_dismiss_btn.setVisible(bool(dismiss_visible))
        self._notice_frame.setVisible(True)
        # Notify screen readers. ``updateAccessibility`` is the
        # documented delivery path in PySide6 (Qt 6.5+); in-process
        # bridges read the event, AT-SPI / UIAutomation bridges
        # forward it to the host. Posting on the notice frame lets
        # the event follow the widget's lifecycle.
        QAccessible.updateAccessibility(QAccessibleAnnouncementEvent(self._notice_frame, message))

    def clear_notice(self) -> None:
        """Hide the notice row without emitting ``notice_dismissed``.

        Used by terminal-success paths (e.g. an IDB switch clears
        leftover notices) where the user did not click dismiss.
        Intentionally silent — re-announcing the same message after
        the user already saw it would be an accessibility regression.
        """
        self._notice_frame.setVisible(False)
        self._notice_retry_btn.setVisible(False)
        self._notice_dismiss_btn.setVisible(False)

    def _capture_announcements(self, sink: object) -> None:
        """Test seam: route ``QAccessible.updateAccessibility`` events
        for the notice frame into ``sink``.

        Production code never calls this — it exists so the unit
        suite can install a recorder onto the notice widget without
        subclassing ``HistoryPanel``. ``sink`` must be a callable that
        accepts ``(event, message)`` tuples (i.e. ``list.append``).

        Implementation: the test stub's ``QAccessible.updateAccessibility``
        appends to ``target._announcements`` and, when present, calls
        ``target._announcements_sink(entry)``. This method registers
        the sink and drains any events that were already recorded
        before the sink was installed.
        """
        # Drain anything already captured before this method ran.
        existing = getattr(self._notice_frame, "_announcements", None) or []
        for entry in existing:
            sink(entry)
        # Install the sink so subsequent ``updateAccessibility`` calls
        # forward into ``sink`` in addition to the raw list.
        self._notice_frame._announcements_sink = sink

    def _on_notice_dismissed(self) -> None:
        """User clicked dismiss — clear the notice and emit the signal."""
        self.clear_notice()
        self.notice_dismissed.emit()

    def shutdown(self) -> None:
        """Detach theme subscriptions on this panel and every row.

        Idempotent — safe to call from ``PanelCore.shutdown`` and from
        ``deleteLater`` paths.
        """
        disconnect_theme(self)
        for row in list(self._row_widgets):
            row.shutdown()

    def visible_session_ids(self) -> list[str]:
        """Return the IDs currently rendered as rows, in display order."""
        return [row.entry.session_id for row in self._row_widgets if row.isVisible()]

    # === Internals ====================================================

    def _on_search_changed(self, _text: str) -> None:
        """Re-render rows from the cached list with the new query."""
        # Loading trumps everything; do not touch rows until the
        # background refresh returns.
        if self._state == _STATE_LOADING:
            return
        self._apply_search()

    def _apply_search(self) -> None:
        """Render rows from ``self._entries`` + current search query."""
        query = self._search.text().strip().casefold()
        visible = [entry for entry in self._entries if query in entry.title.casefold()]
        if not self._entries:
            self._state = _STATE_STATUS
            self._set_status_message(_EMPTY_COPY)
            self._retry_btn.setVisible(False)
            self._stack.setCurrentWidget(self._status_frame)
            self._render_rows([])
            return
        if not visible:
            self._state = _STATE_STATUS
            self._set_status_message(_SEARCH_EMPTY_COPY)
            self._retry_btn.setVisible(False)
            self._stack.setCurrentWidget(self._status_frame)
            self._render_rows([])
            return
        self._state = _STATE_LIST
        # Index 1 is the list container — switching here is cheaper
        # than calling ``setCurrentWidget`` with a tracked reference.
        self._stack.setCurrentIndex(1)
        self._render_rows(visible)

    def _set_status_message(self, text: str) -> None:
        self._status_label.setText(text)

    def _render_rows(self, entries: list[SessionHistoryEntry]) -> None:
        """Replace current rows with the given entries (in given order)."""
        # Drop existing rows — including hidden ones from a previous
        # error state. ``shutdown`` is idempotent so the row's theme
        # subscription is released before we lose the reference.
        for row in list(self._row_widgets):
            self._list_layout.removeWidget(row)
            row.shutdown()
            row.deleteLater()
        self._row_widgets = []

        # Insert new rows before the trailing stretch. Operations
        # (open + delete) start enabled unless a delete is pending —
        # the row widget owns the per-row click handlers so a row with
        # ``set_operation_enabled(False)`` neither opens nor deletes
        # while the parent's pending state is active.
        operations_allowed = self._pending_session_id is None
        for entry in entries:
            row = HistoryRowWidget(entry, self._list_widget)
            row.session_open_requested.connect(self.session_open_requested.emit)
            row.session_delete_requested.connect(self.session_delete_requested.emit)
            row.set_operation_enabled(operations_allowed)
            row._apply_styles()
            self._list_layout.insertWidget(self._list_layout.count() - 1, row)
            self._row_widgets.append(row)

    def _apply_styles(self, _tokens: object = None) -> None:
        """Refresh chrome + row styles from the live tokens.

        In host-theme (IDA-native) mode the panel-level stylesheet is
        cleared via ``maybe_host_stylesheet`` so the host palette takes
        over without an explicit per-widget override.
        """
        if getattr(self, "_title", None) is not None:
            self._title.setStyleSheet(maybe_host_stylesheet(get_history_title_style()))
        if getattr(self, "_scope_label", None) is not None:
            self._scope_label.setStyleSheet(maybe_host_stylesheet(get_history_scope_style()))
        if getattr(self, "_status_label", None) is not None:
            self._status_label.setStyleSheet(maybe_host_stylesheet(get_history_status_style()))
        if getattr(self, "_retry_btn", None) is not None:
            self._retry_btn.setStyleSheet(maybe_host_stylesheet(get_history_retry_btn_style()))
        if getattr(self, "_close_btn", None) is not None:
            self._close_btn.setStyleSheet(maybe_host_stylesheet(get_history_close_btn_style()))
        if getattr(self, "_search", None) is not None:
            self._search.setStyleSheet(maybe_host_stylesheet(get_history_search_style()))
        # Notice row widgets (Task 4 a11y follow-up): the transient
        # notice frame is part of the chrome and must re-skin on theme
        # change so a host palette swap reaches the row without a
        # manual ``show_notice`` round-trip.
        if getattr(self, "_notice_label", None) is not None:
            self._notice_label.setStyleSheet(maybe_host_stylesheet(get_history_status_style()))
        if getattr(self, "_notice_retry_btn", None) is not None:
            self._notice_retry_btn.setStyleSheet(maybe_host_stylesheet(get_history_retry_btn_style()))
        if getattr(self, "_notice_dismiss_btn", None) is not None:
            self._notice_dismiss_btn.setStyleSheet(maybe_host_stylesheet(get_history_close_btn_style()))
        self.setStyleSheet(maybe_host_stylesheet(get_history_panel_style()))
        for row in self._row_widgets:
            row._apply_styles()


__all__ = [
    "HISTORY_PANEL_CONTENT_MAX_WIDTH",
    "HistoryPanel",
    "HistoryRowWidget",
]
