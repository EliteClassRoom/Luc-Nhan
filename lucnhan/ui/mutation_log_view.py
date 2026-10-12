"""Mutation log panel: displays the history of mutating tool calls with undo support."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .qt_compat import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    Qt,
    QVBoxLayout,
    QWidget,
    Signal,
)
from .styles import (
    get_history_close_btn_style,
    get_mutation_badge_style,
    get_mutation_count_style,
    get_mutation_desc_style,
    get_mutation_indicator_style,
    get_mutation_meta_style,
    get_mutation_title_style,
    get_mutation_undo_btn_style,
)
from .theme.applicator import bind_theme, disconnect_theme

if TYPE_CHECKING:
    from ..agent.mutation import MutationRecord


class MutationEntryWidget(QFrame):
    """Single mutation record (GUI contract G5: operation / target / time / status).

    The operation is the record's ``description`` (what the agent did),
    the ``tool_name`` badge is the concrete operation, and the status is
    the reversible / not-reversible indicator paired with its word —
    status is never colour-only (G9).  Clicking a row does NOT undo it;
    Undo stays the explicit button in the pane header.
    """

    undo_clicked = Signal(int)  # emits the entry index

    def __init__(self, index: int, record: MutationRecord, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("mutation_entry")
        self._index = index
        self._record = record

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        # Reversibility indicator
        self._indicator = QLabel("↩" if record.reversible else "⊘")
        self._indicator.setFixedWidth(20)
        self._indicator.setStyleSheet(get_mutation_indicator_style(record.reversible))
        status = "Reversible" if record.reversible else "Not reversible"
        self._indicator.setToolTip(status)
        layout.addWidget(self._indicator)

        # Operation line + time meta line, mirroring the History rows so
        # both panes read as the same compact row shape.
        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)

        self._desc = QLabel(record.description)
        # Plain text only — the description is tool/event text, never
        # trusted as markup.
        self._desc.setTextFormat(Qt.TextFormat.PlainText)
        self._desc.setWordWrap(True)
        self._desc.setStyleSheet(get_mutation_desc_style())
        self._desc.setToolTip(record.description)
        text_col.addWidget(self._desc)

        ts = time.strftime("%H:%M:%S", time.localtime(record.timestamp))
        self._meta = QLabel(f"[{ts}] · {status}")
        self._meta.setTextFormat(Qt.TextFormat.PlainText)
        self._meta.setWordWrap(True)
        self._meta.setStyleSheet(get_mutation_meta_style())
        text_col.addWidget(self._meta)
        layout.addLayout(text_col, 1)

        # Tool name badge
        self._tool_badge = QLabel(record.tool_name)
        self._tool_badge.setStyleSheet(get_mutation_badge_style())
        layout.addWidget(self._tool_badge)

    def shutdown(self) -> None:
        """Detach the theme subscription (idempotent)."""
        disconnect_theme(self)

    @property
    def record(self) -> MutationRecord:
        return self._record

    def _apply_styles(self, _tokens: object = None) -> None:
        """Re-apply indicator / description / badge styles from the live tokens.

        ``record`` is fixed at construction time, so we read
        ``reversible`` from it directly when repainting the indicator
        glyph (success colour for reversible, muted_text for
        irreversible).  Description and badge always use the
        token-driven getters so a theme switch updates them
        immediately.
        """
        if getattr(self, "_indicator", None) is not None:
            self._indicator.setStyleSheet(get_mutation_indicator_style(self._record.reversible))
        if getattr(self, "_desc", None) is not None:
            self._desc.setStyleSheet(get_mutation_desc_style())
        if getattr(self, "_meta", None) is not None:
            self._meta.setStyleSheet(get_mutation_meta_style())
        if getattr(self, "_tool_badge", None) is not None:
            self._tool_badge.setStyleSheet(get_mutation_badge_style())


class MutationLogPanel(QFrame):
    """Panel showing the mutation history with undo support.

    GUI contract G5 order: header (name, count/scope, Close) -> records
    (operation / target / time / status).  ``Undo Last`` is explicit and
    lives in its own action row below the records so it can never be
    triggered by a stray row click — clicking a row only selects it.
    """

    undo_requested = Signal(int)  # emits count to undo
    close_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("mutation_log_panel")

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Header
        self._header = QFrame()
        self._header.setObjectName("mutation_log_header")
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(12, 8, 12, 8)

        self._title = QLabel("Mutations")
        self._title.setStyleSheet(get_mutation_title_style())
        header_layout.addWidget(self._title)

        self._count_label = QLabel("0 mutations")
        self._count_label.setStyleSheet(get_mutation_count_style())
        # GUI contract G3: the count is secondary metadata — collapse it
        # first under width pressure so the pane name and Close stay
        # readable in a narrow pane.
        self._count_label.setMinimumWidth(0)
        self._count_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        header_layout.addWidget(self._count_label)

        header_layout.addStretch()

        self._close_btn = QPushButton("Close")
        self._close_btn.setObjectName("mutation_close_btn")
        self._close_btn.setStyleSheet(get_history_close_btn_style())
        self._close_btn.setToolTip("Close the mutations pane")
        self._close_btn.setAccessibleName("Close the mutations pane")
        self._close_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._close_btn.clicked.connect(lambda: self.close_requested.emit())
        header_layout.addWidget(self._close_btn)

        main_layout.addWidget(self._header)

        # Scroll area for entries
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self._entries_widget = QWidget()
        self._entries_layout = QVBoxLayout(self._entries_widget)
        self._entries_layout.setContentsMargins(4, 4, 4, 4)
        self._entries_layout.setSpacing(2)
        self._entries_layout.addStretch()

        self._scroll.setWidget(self._entries_widget)
        main_layout.addWidget(self._scroll, 1)

        # Action strip below the records: Undo is explicit and separated
        # from the rows (GUI contract G5) so a row click can never undo.
        self._action_row = QFrame()
        self._action_row.setObjectName("mutation_action_row")
        action_layout = QHBoxLayout(self._action_row)
        action_layout.setContentsMargins(8, 6, 8, 8)
        action_layout.addStretch()
        self._undo_btn = QPushButton("Undo Last")
        self._undo_btn.setObjectName("mutation_undo_btn")
        self._undo_btn.setStyleSheet(get_mutation_undo_btn_style())
        self._undo_btn.setToolTip("Reverse the most recent reversible mutation")
        self._undo_btn.setAccessibleName("Undo the most recent reversible mutation")
        self._undo_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._undo_btn.clicked.connect(lambda: self.undo_requested.emit(1))
        self._undo_btn.setEnabled(False)
        action_layout.addWidget(self._undo_btn)
        main_layout.addWidget(self._action_row)

        self._entries: list[MutationEntryWidget] = []

        # Subscribe to theme changes now that every visual element is
        # built.  ``bind_theme`` runs the callback synchronously so the
        # initial paint reflects the active palette.  Entries added via
        # :meth:`add_mutation` are wired to the same signal by the
        # parent's ``_apply_styles`` walker (each entry subscribes to
        # the theme signal on its own so it survives a panel teardown
        # that drops the parent subscription first).
        bind_theme(self, self._apply_styles)

    def add_mutation(self, record: MutationRecord) -> None:
        """Add a new mutation entry to the log."""
        index = len(self._entries)
        entry = MutationEntryWidget(index, record, self._entries_widget)
        # Apply the current theme to the new entry immediately so
        # it doesn't render with stale construction-time colours if
        # the theme changed between panel construction and the first
        # mutation.
        entry._apply_styles()
        # Insert before the stretch
        self._entries_layout.insertWidget(self._entries_layout.count() - 1, entry)
        self._entries.append(entry)
        self._update_count()

    def entry_count(self) -> int:
        """How many mutation records the log currently holds."""
        return len(self._entries)

    def remove_last(self, count: int = 1) -> None:
        """Remove the last N entries (after undo)."""
        for _ in range(min(count, len(self._entries))):
            entry = self._entries.pop()
            self._entries_layout.removeWidget(entry)
            entry.deleteLater()
        self._update_count()

    def clear_all(self) -> None:
        """Clear all entries."""
        for entry in self._entries:
            self._entries_layout.removeWidget(entry)
            entry.deleteLater()
        self._entries.clear()
        self._update_count()

    def _update_count(self) -> None:
        n = len(self._entries)
        self._count_label.setText(f"{n} mutation{'s' if n != 1 else ''}")
        self._undo_btn.setEnabled(n > 0 and any(e.record.reversible for e in self._entries))

    def _apply_styles(self, _tokens: object = None) -> None:
        """Refresh panel chrome (title / count / undo button) and walk entries.

        The undo button's enabled state is intentionally NOT touched
        here — it depends on the entries' reversibility, not on the
        theme.  ``_update_count`` is the single owner of that flag,
        and the constructor sets the initial disabled state.
        """
        if getattr(self, "_title", None) is not None:
            self._title.setStyleSheet(get_mutation_title_style())
        if getattr(self, "_count_label", None) is not None:
            self._count_label.setStyleSheet(get_mutation_count_style())
        if getattr(self, "_undo_btn", None) is not None:
            self._undo_btn.setStyleSheet(get_mutation_undo_btn_style())
        if getattr(self, "_close_btn", None) is not None:
            self._close_btn.setStyleSheet(get_history_close_btn_style())
        for entry in self._entries:
            entry._apply_styles()

    def shutdown(self) -> None:
        """Detach the theme subscription (idempotent)."""
        disconnect_theme(self)
