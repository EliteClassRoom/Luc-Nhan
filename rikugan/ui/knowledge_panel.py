"""Knowledge tab: table + detail view of memories / entities / relations.

This is the user-facing browser for the raw knowledge store. It does
not run a worker thread; refreshes happen on demand (the *Refresh*
button) and on relevant events (research note saved, exploration
finding, knowledge retrieval). The user can:

* Switch the type filter (All / Memories / Entities / Relations / Notes).
* Search across all fields with a free-text box.
* Toggle the ``Show retrieved knowledge in chat`` checkbox, which is
  persisted to ``RikuganConfig`` immediately.
* Click a row to see the raw JSON + a Markdown-rendered preview.

Heavy lifting (file I/O, ranking) is delegated to
:mod:`rikugan.memory`. The widget is dumb on purpose — no background
polls, no IDA calls, no provider round-trips.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .qt_compat import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    Signal,
)
from .styles import get_placeholder_style
from .theme.applicator import bind_theme, disconnect_theme

_TYPE_FILTERS = ("All", "Memories", "Entities", "Relations", "Notes")


@dataclass
class _Row:
    kind: str  # "memory" | "entity" | "relation" | "note"
    id: str
    title: str
    secondary: str  # tags / predicate / genre
    confidence: float
    updated: str
    status: str = ""  # hypothesis verdict label, "" for non-hypotheses
    raw: dict = field(default_factory=dict)  # for the detail pane


_STATUS_LABELS = {
    "verified": "✓ verified",
    "wrong": "✗ wrong",
    "unverified": "? unverified",
}


def _hypothesis_status_label(memory) -> str:
    """Return the status label for a hypothesis memory; empty otherwise."""
    if str(_getattr(memory, "type", "")) != "hypothesis":
        return ""
    status = str(_getattr(memory, "status", "unverified") or "unverified")
    return _STATUS_LABELS.get(status, f"? {status}")

class KnowledgePanel(QWidget):
    """Browser widget for the raw knowledge store.

    Emits:
        refresh_requested: when the user clicks *Refresh* or the panel
            wants to re-pull the store. The host (panel_core) is
            expected to call :meth:`populate` when ready.
        show_retrieved_changed(bool): when the user toggles the chat
            visibility checkbox. The host should persist this to
            ``RikuganConfig``.
    """

    refresh_requested = Signal()
    show_retrieved_changed = Signal(bool)

    def __init__(self, parent: QWidget = None):
        super().__init__(parent)
        self.setObjectName("knowledge_panel")

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(4)

        # Toolbar
        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search… (hex, function name, tag, free text)")
        self._search.textChanged.connect(self._apply_filter)
        toolbar.addWidget(self._search, 1)

        self._filter_combo = QComboBox()
        self._filter_combo.addItems(_TYPE_FILTERS)
        self._filter_combo.currentTextChanged.connect(self._apply_filter)
        toolbar.addWidget(self._filter_combo)

        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self.refresh_requested.emit)
        toolbar.addWidget(self._refresh_btn)

        self._show_chk = QCheckBox("Show retrieved knowledge in chat")
        self._show_chk.setToolTip(
            "When enabled, each turn surfaces a compact indicator showing "
            "what was retrieved from the knowledge store for that turn."
        )
        self._show_chk.toggled.connect(self.show_retrieved_changed.emit)
        toolbar.addWidget(self._show_chk)

        main_layout.addLayout(toolbar)

        # Counts label
        self._counts_label = QLabel("0 memories / 0 entities / 0 relations / 0 notes")
        self._counts_label.setStyleSheet(get_placeholder_style())
        main_layout.addWidget(self._counts_label)

        # Main table
        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["Type", "ID/Title", "Tags/Predicate", "Status", "Confidence", "Updated"]
        )
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        h = self._table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self._table.itemSelectionChanged.connect(self._on_row_selected)
        main_layout.addWidget(self._table, 3)

        # Detail pane
        self._detail = QPlainTextEdit()
        self._detail.setReadOnly(True)
        self._detail.setPlaceholderText("Select a row to see its raw JSON / content.")
        main_layout.addWidget(self._detail, 2)

        # Internal state
        self._rows: list[_Row] = []
        self._disabled: bool = False

        # Subscribe to theme changes so the counts label and the
        # optional disabled-state banner re-paint when the user
        # switches palettes.  The table / detail pane rely on Qt's
        # palette cascade (no widget-local stylesheet), so they
        # follow automatically.
        bind_theme(self, self._apply_styles)

    def _apply_styles(self, _tokens: object = None) -> None:
        """Re-apply the counts label and the disabled-state banner.

        The table and detail pane intentionally have no widget-local
        stylesheets — they rely on the parent's palette cascade so a
        theme switch updates them through ``QApplication.setStyle``
        automatically.  The counts label and banner, however, had
        a hardcoded ``get_placeholder_style()`` stylesheet applied
        at construction; we re-issue it here so they don't get
        stuck on the old palette.
        """
        if getattr(self, "_counts_label", None) is not None:
            self._counts_label.setStyleSheet(get_placeholder_style())
        if getattr(self, "_banner_label", None) is not None:
            self._banner_label.setStyleSheet(get_placeholder_style())

    def shutdown(self) -> None:
        """Detach the theme subscription (idempotent)."""
        disconnect_theme(self)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_show_retrieved(self, value: bool) -> None:
        """Programmatically sync the checkbox (avoids re-emitting)."""
        blocker = self._show_chk.blockSignals(True)
        try:
            self._show_chk.setChecked(bool(value))
        finally:
            self._show_chk.blockSignals(blocker)

    def set_counts(self, counts: dict) -> None:
        """Update the counts label and apply filtering."""
        mem = counts.get("memories", 0)
        ent = counts.get("entities", 0)
        rel = counts.get("relations", 0)
        notes = counts.get("notes", 0)
        self._counts_label.setText(f"{mem} memories / {ent} entities / {rel} relations / {notes} notes")

    def set_disabled_message(self, message: str) -> None:
        """Render an empty-state placeholder in the table area."""
        self._rows = []
        self._table.setRowCount(0)
        self._detail.setPlainText(message)
        self._counts_label.setText("—")

    def set_disabled_state(self, disabled: bool) -> None:
        """Toggle the master ``knowledge_enabled`` state.

        When *disabled* is True, the panel renders a clear "raw
        knowledge memory is disabled" banner and clears the table.
        The user can still interact with the toolbar (search/filter
        chrome stays visible) so toggling the option back on in
        Settings immediately re-enables data display.
        """
        self._disabled = bool(disabled)
        if not hasattr(self, "_banner_label"):
            # Banner inserted between toolbar and counts label so the
            # user sees the disabled state at the top of the panel.
            self._banner_label = QLabel(self)
            self._banner_label.setObjectName("knowledge_disabled_banner")
            self._banner_label.setStyleSheet(get_placeholder_style())
            self._banner_label.setWordWrap(True)
            # The main_layout has: toolbar(0), counts_label(1),
            # table(2), detail(3). Insert the banner at index 1.
            try:
                self.layout().insertWidget(1, self._banner_label)
            except Exception:
                pass
        if self._disabled:
            self._banner_label.setText(
                "Raw knowledge memory is disabled. "
                "Re-enable it in Settings → Behavior to resume writes and the Knowledge tab."
            )
            self._banner_label.setVisible(True)
            self._rows = []
            self._table.setRowCount(0)
            self._counts_label.setText("—")
        else:
            self._banner_label.setVisible(False)

    def populate(
        self,
        memories,
        entities,
        relations,
        notes=None,
    ) -> None:
        """Replace the table contents with the supplied records.

        Each argument is a list of dataclass instances (or dicts);
        we only read the attributes we know about, so test stubs
        work as well as live records.
        """
        notes = notes or []
        rows: list[_Row] = []
        for m in memories:
            rows.append(
                _Row(
                    kind="memory",
                    id=_getattr(m, "id", ""),
                    title=_getattr(m, "title", ""),
                    secondary=", ".join(_getattr(m, "tags", []) or []),
                    confidence=float(_getattr(m, "confidence", 0.0) or 0.0),
                    updated=_getattr(m, "updated_at", "") or _getattr(m, "created_at", ""),
                    status=_hypothesis_status_label(m),
                    raw=_to_dict(m),
                )
            )
        for e in entities:
            addr = _getattr(e, "address", "") or ""
            rows.append(
                _Row(
                    kind="entity",
                    id=_getattr(e, "id", ""),
                    title=(_getattr(e, "display_name", "") or _getattr(e, "name", "")) + (f" @ {addr}" if addr else ""),
                    secondary=", ".join(_getattr(e, "tags", []) or []),
                    confidence=1.0,
                    updated="",
                    status="",
                    raw=_to_dict(e),
                )
            )
        for r in relations:
            rows.append(
                _Row(
                    kind="relation",
                    id=_getattr(r, "id", ""),
                    title=f"{_getattr(r, 'src', '')} → {_getattr(r, 'dst', '')}",
                    secondary=_getattr(r, "predicate", ""),
                    confidence=float(_getattr(r, "confidence", 0.0) or 0.0),
                    updated="",
                    status="",
                    raw=_to_dict(r),
                )
            )
        for n in notes:
            if isinstance(n, str):
                rows.append(
                    _Row(
                        kind="note",
                        id=os.path.basename(str(n)) if n else "note",
                        title=(n[:80] + "…") if len(n) > 80 else n,
                        secondary="note excerpt",
                        confidence=1.0,
                        updated="",
                        status="",
                        raw={"body": n},
                    )
                )
        self._rows = rows
        self._render_table()

    def showEvent(self, event):
        super().showEvent(event)
        # Trigger an initial refresh when the panel is first shown so
        # the user does not see a stale empty table.
        if not getattr(self, "_initial_refreshed", False):
            self._initial_refreshed = True
            self.refresh_requested.emit()

    # Internals
    # ------------------------------------------------------------------

    def _apply_filter(self) -> None:
        self._render_table()

    def _render_table(self) -> None:
        type_filter = self._filter_combo.currentText() or "All"
        needle = (self._search.text() or "").lower().strip()

        visible: list[_Row] = []
        for r in self._rows:
            if type_filter != "All" and r.kind != type_filter.lower().rstrip("s"):
                # The combo says "Memories" → match "memory", "Relations" → "relation", etc.
                if not r.kind.startswith(type_filter[:-1].lower()):
                    continue
            if needle:
                haystack = " ".join([r.id, r.title, r.secondary, r.kind]).lower()
                if needle not in haystack:
                    continue
            visible.append(r)

        self._table.setRowCount(len(visible))
        for row, item in enumerate(visible):
            kind_cell = QTableWidgetItem(item.kind)
            id_cell = QTableWidgetItem(f"{item.id}\n{item.title}")
            id_cell.setToolTip(item.id)
            secondary_cell = QTableWidgetItem(item.secondary)
            status_cell = QTableWidgetItem(item.status)
            if item.status:
                status_cell.setToolTip(item.status)
            conf_cell = QTableWidgetItem(f"{item.confidence:.2f}" if item.confidence else "")
            updated_cell = QTableWidgetItem(item.updated)
            self._table.setItem(row, 0, kind_cell)
            self._table.setItem(row, 1, id_cell)
            self._table.setItem(row, 2, secondary_cell)
            self._table.setItem(row, 3, status_cell)
            self._table.setItem(row, 4, conf_cell)
            self._table.setItem(row, 5, updated_cell)
            kind_cell.setData(0x0100, item.raw)

    def _on_row_selected(self) -> None:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            self._detail.setPlainText("")
            return
        idx = rows[0].row()
        cell = self._table.item(idx, 0)
        if cell is None:
            return
        raw = cell.data(0x0100)
        if raw is None:
            return
        try:
            self._detail.setPlainText(json.dumps(raw, indent=2, ensure_ascii=False, default=str))
        except Exception:
            self._detail.setPlainText(str(raw))


def _getattr(obj, name: str, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _to_dict(obj) -> dict:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return dict(obj.__dict__) if hasattr(obj, "__dict__") else {}


__all__ = ["KnowledgePanel"]
