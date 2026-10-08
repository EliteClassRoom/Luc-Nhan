"""Agent tree view for the Agents tab (bulk-rename manager)."""

from __future__ import annotations

from dataclasses import dataclass

from .qt_compat import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    Qt,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    Signal,
)
from .styles import (
    get_agent_btn_style,
    get_agent_combo_style,
    get_agent_preview_style,
    get_agent_status_colors,
    get_agent_status_label_style,
    get_agent_tree_style,
)
from .theme.applicator import bind_theme, disconnect_theme


@dataclass
class AgentInfo:
    """Snapshot of an agent's state for display."""

    agent_id: str
    name: str
    agent_type: str
    status: str = "PENDING"
    turns: int = 0
    elapsed_seconds: float = 0.0
    summary: str = ""
    category: str = ""


class AgentTreeWidget(QWidget):
    """Tree-based view of running and completed sub-agents."""

    cancel_requested = Signal(str)  # agent_id
    inject_summary_requested = Signal(str)  # agent_id

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("agent_tree_widget")

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(4)

        # Toolbar
        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)

        self._kill_btn = QPushButton("Kill Selected")
        self._kill_btn.setStyleSheet(get_agent_btn_style())
        self._kill_btn.clicked.connect(self._on_kill_selected)
        toolbar.addWidget(self._kill_btn)

        self._clean_btn = QPushButton("Clean")
        self._clean_btn.setStyleSheet(get_agent_btn_style())
        self._clean_btn.setToolTip("Remove selected finished agents (or all finished if none selected)")
        self._clean_btn.clicked.connect(self._on_clean)
        toolbar.addWidget(self._clean_btn)

        self._filter_combo = QComboBox()
        self._filter_combo.setFixedWidth(130)
        self._filter_combo.setStyleSheet(get_agent_combo_style())
        self._filter_combo.addItems(["All Agents", "General", "Bulk Rename"])
        self._filter_combo.currentTextChanged.connect(self._apply_filter)
        toolbar.addWidget(self._filter_combo)

        toolbar.addStretch()

        self._status_label = QLabel("0 running / 0 completed")
        self._status_label.setStyleSheet(get_agent_status_label_style())
        toolbar.addWidget(self._status_label)

        main_layout.addLayout(toolbar)

        # Tree widget
        self._tree = QTreeWidget()
        self._tree.setObjectName("agent_tree")
        self._tree.setStyleSheet(get_agent_tree_style())
        self._tree.setHeaderLabels(["Name", "Type", "Status", "Turns", "Time"])
        self._tree.setColumnWidth(0, 150)
        self._tree.setColumnWidth(1, 100)
        self._tree.setColumnWidth(2, 80)
        self._tree.setColumnWidth(3, 50)
        self._tree.setColumnWidth(4, 60)
        self._tree.setAlternatingRowColors(True)
        self._tree.setRootIsDecorated(False)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._tree.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._tree.itemSelectionChanged.connect(self._on_item_selected)
        main_layout.addWidget(self._tree)

        # Output preview
        self._preview = QTextEdit()
        self._preview.setObjectName("agent_preview")
        self._preview.setReadOnly(True)
        self._preview.setFixedHeight(80)
        self._preview.setStyleSheet(get_agent_preview_style())
        self._preview.setPlaceholderText("Select an agent to preview its output...")
        main_layout.addWidget(self._preview)

        # Internal agent tracking: agent_id -> AgentInfo
        self._agents: dict[str, AgentInfo] = {}
        # Map agent_id -> QTreeWidgetItem
        self._items: dict[str, QTreeWidgetItem] = {}
        # Incremental status counters
        self._running_count: int = 0
        self._completed_count: int = 0

        # Subscribe to theme changes so the tree, preview, combo,
        # buttons, and per-row status colours all repaint when the
        # user switches palettes.
        bind_theme(self, self._apply_styles)

    def _on_kill_selected(self) -> None:
        """Cancel the currently selected agent(s)."""
        items = self._tree.selectedItems()
        if not items:
            return
        for item in items:
            agent_id = item.data(0, Qt.ItemDataRole.UserRole)
            if agent_id:
                info = self._agents.get(agent_id)
                if info and info.status in ("PENDING", "RUNNING"):
                    self.cancel_requested.emit(agent_id)

    def _on_clean(self) -> None:
        """Remove finished agents from the tree.

        If agents are selected, only remove those that are finished.
        If nothing is selected, remove all finished agents.
        """
        _FINISHED = {"COMPLETED", "FAILED", "CANCELLED"}
        selected = self._tree.selectedItems()

        if selected:
            to_remove = []
            for item in selected:
                agent_id = item.data(0, Qt.ItemDataRole.UserRole)
                if agent_id:
                    info = self._agents.get(agent_id)
                    if info and info.status in _FINISHED:
                        to_remove.append(agent_id)
        else:
            to_remove = [aid for aid, info in self._agents.items() if info.status in _FINISHED]

        for agent_id in to_remove:
            # Decrement counters for removed agent
            info = self._agents.get(agent_id)
            if info:
                if info.status == "RUNNING":
                    self._running_count -= 1
                elif info.status == "COMPLETED":
                    self._completed_count -= 1
            item = self._items.pop(agent_id, None)
            if item is not None:
                idx = self._tree.indexOfTopLevelItem(item)
                if idx >= 0:
                    self._tree.takeTopLevelItem(idx)
            self._agents.pop(agent_id, None)

        self._update_status_counts()

    def update_agent(self, info: AgentInfo) -> None:
        """Add or update a tree item for the given agent."""
        # Get old status for counter adjustment
        old_status = None
        if info.agent_id in self._agents:
            old_status = self._agents[info.agent_id].status

        # Decrement old counters
        if old_status == "RUNNING":
            self._running_count -= 1
        elif old_status == "COMPLETED":
            self._completed_count -= 1

        # Update agent info
        self._agents[info.agent_id] = info

        if info.agent_id in self._items:
            item = self._items[info.agent_id]
        else:
            item = QTreeWidgetItem(self._tree)
            item.setData(0, Qt.ItemDataRole.UserRole, info.agent_id)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self._items[info.agent_id] = item

        item.setText(0, info.name)
        item.setText(1, info.agent_type)
        item.setText(2, info.status)
        item.setText(3, str(info.turns))
        item.setText(4, self._format_elapsed(info.elapsed_seconds))

        # Status color
        status_colors = get_agent_status_colors()
        color = status_colors.get(info.status, "#d4d4d4")
        from .qt_compat import QColor

        item.setForeground(2, QColor(color))

        # Increment new counters
        if info.status == "RUNNING":
            self._running_count += 1
        elif info.status == "COMPLETED":
            self._completed_count += 1

        # Apply current category filter to this item
        filter_text = self._filter_combo.currentText()
        if filter_text == "Bulk Rename":
            item.setHidden(info.category != "bulk_rename")
        elif filter_text == "General":
            item.setHidden(info.category == "bulk_rename")
        else:
            item.setHidden(False)

        self._update_status_counts()

        # Auto-update preview if this agent is selected
        selected = self._tree.selectedItems()
        if selected and selected[0].data(0, Qt.ItemDataRole.UserRole) == info.agent_id:
            self._preview.setPlainText(info.summary or "(no output yet)")

    def _apply_filter(self, _text: str = "") -> None:
        """Show/hide tree items based on the selected category filter."""
        selected = self._filter_combo.currentText()
        for agent_id, item in self._items.items():
            info = self._agents.get(agent_id)
            if info is None:
                continue
            if selected == "All Agents":
                item.setHidden(False)
            elif selected == "Bulk Rename":
                item.setHidden(info.category != "bulk_rename")
            elif selected == "General":
                item.setHidden(info.category == "bulk_rename")
        self._update_status_counts()

    def _on_item_selected(self) -> None:
        """Show the summary of the selected agent in the preview pane."""
        items = self._tree.selectedItems()
        if not items:
            self._preview.clear()
            return
        agent_id = items[0].data(0, Qt.ItemDataRole.UserRole)
        info = self._agents.get(agent_id)
        if info:
            self._preview.setPlainText(info.summary or "(no output yet)")
        else:
            self._preview.clear()

    def _update_status_counts(self) -> None:
        """Refresh the running / completed counts label."""
        self._status_label.setText(f"{self._running_count} running / {self._completed_count} completed")

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        """Format elapsed seconds as m:ss."""
        mins = int(seconds) // 60
        secs = int(seconds) % 60
        return f"{mins}:{secs:02d}"

    def _apply_styles(self, _tokens: object = None) -> None:
        """Re-apply toolbar / tree / preview styles from the live tokens.

        Also walks every existing tree item and refreshes its
        status-cell foreground colour against the new palette.  The
        running / completed counters are not touched — they are
        runtime state owned by ``update_agent`` and must survive
        the theme switch.
        """
        if getattr(self, "_kill_btn", None) is not None:
            self._kill_btn.setStyleSheet(get_agent_btn_style())
        if getattr(self, "_clean_btn", None) is not None:
            self._clean_btn.setStyleSheet(get_agent_btn_style())
        if getattr(self, "_filter_combo", None) is not None:
            self._filter_combo.setStyleSheet(get_agent_combo_style())
        if getattr(self, "_status_label", None) is not None:
            self._status_label.setStyleSheet(get_agent_status_label_style())
        if getattr(self, "_tree", None) is not None:
            self._tree.setStyleSheet(get_agent_tree_style())
        if getattr(self, "_preview", None) is not None:
            self._preview.setStyleSheet(get_agent_preview_style())
        # Refresh per-row status colours.  Existing items keep their
        # text — only the status-cell foreground changes.
        self._refresh_row_status_colors()

    def _refresh_row_status_colors(self) -> None:
        """Repaint the status column on every known tree item.

        Reads each item's stored ``AgentInfo.status`` (via
        ``self._agents`` keyed by the item's ``UserRole`` data) and
        looks the colour up in the current ``AGENT_STATUS_COLORS``
        dict.  Items whose agent has been removed are skipped so a
        mid-clean theme switch does not crash.
        """
        try:
            from .qt_compat import QColor
        except Exception:
            return
        status_colors = get_agent_status_colors()
        for agent_id, item in self._items.items():
            info = self._agents.get(agent_id)
            if info is None:
                continue
            color = status_colors.get(info.status, "#d4d4d4")
            item.setForeground(2, QColor(color))

    def shutdown(self) -> None:
        """Detach the theme subscription (idempotent)."""
        disconnect_theme(self)
