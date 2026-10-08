"""Bulk function renaming UI for the Renamer tab."""

from __future__ import annotations

from dataclasses import dataclass

from .qt_compat import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QIntValidator,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    Qt,
    QTableWidget,
    QTableWidgetItem,
    QTimer,
    QVBoxLayout,
    QWidget,
    Signal,
)
from .styles import (
    get_bulk_btn_style,
    get_bulk_check_style,
    get_bulk_combo_style,
    get_bulk_filter_style,
    get_bulk_mode_label_style,
    get_bulk_num_input_style,
    get_bulk_progress_style,
    get_bulk_radio_style,
    get_bulk_selection_label_style,
    get_bulk_start_btn_style,
    get_bulk_status_colors,
    get_bulk_stop_btn_style,
    get_bulk_table_style,
)
from .theme.applicator import bind_theme, disconnect_theme

# Column indices
_COL_CHECK = 0
_COL_ADDR = 1
_COL_NAME = 2
_COL_SIZE = 3
_COL_NEWNAME = 4
_COL_STATUS = 5


@dataclass
class FunctionEntry:
    """A function loaded into the renamer table."""

    address: int
    name: str
    is_import: bool
    size_bytes: int


class _NumericTableItem(QTableWidgetItem):
    """Table item that sorts numerically instead of lexicographically."""

    def __init__(self, text: str, sort_value: int):
        super().__init__(text)
        self._sort_value = sort_value

    def __lt__(self, other: QTableWidgetItem) -> bool:
        if isinstance(other, _NumericTableItem):
            return self._sort_value < other._sort_value
        return super().__lt__(other)


class BulkRenamerWidget(QWidget):
    """Bulk function renaming interface with filtering and batch controls."""

    start_requested = Signal(list, str, int, int)  # jobs, mode, batch_size, max_concurrent
    pause_requested = Signal()
    cancel_requested = Signal()
    undo_requested = Signal()
    seek_requested = Signal(object)  # address (64-bit int, can't use Signal(int))
    refresh_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("bulk_renamer_widget")
        self._loading = False  # guard to suppress filter during load

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(4)

        # --- Top bar: filter + selection controls ---
        top_bar = QHBoxLayout()
        top_bar.setSpacing(4)

        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("Filter by name or address...")
        self._filter_edit.setStyleSheet(get_bulk_filter_style())
        # Debounce text changes — running ``_on_filter_changed`` on
        # every keystroke scans every row.  For large IDBs that scan
        # is the dominant cost of the filter UI.  A 100ms single-shot
        # timer coalesces rapid typing into one scan.
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(100)
        self._filter_timer.timeout.connect(self._on_filter_changed)
        self._filter_edit.textChanged.connect(self._schedule_filter)
        top_bar.addWidget(self._filter_edit, 1)

        self._filter_combo = QComboBox()
        self._filter_combo.setStyleSheet(get_bulk_combo_style())
        self._filter_combo.addItems(["All Functions", "Auto-named Only", "User-renamed", "Imports"])
        self._filter_combo.currentIndexChanged.connect(self._on_filter_changed)
        top_bar.addWidget(self._filter_combo)

        self._selection_label = QLabel("0 / 0 selected")
        self._selection_label.setStyleSheet(get_bulk_selection_label_style())
        top_bar.addWidget(self._selection_label)

        main_layout.addLayout(top_bar)

        # --- Table ---
        self._table = QTableWidget()
        self._table.setObjectName("renamer_table")
        self._table.setStyleSheet(get_bulk_table_style())
        self._table.setColumnCount(6)
        self._table.setHorizontalHeaderLabels(["", "Address", "Current Name", "Size", "New Name", "Status"])
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.verticalHeader().setVisible(False)
        self._table.setSortingEnabled(True)

        # Header checkbox for column 0 (select all / deselect all)
        self._header_check = QCheckBox()
        self._header_check.setStyleSheet(get_bulk_check_style())
        self._header_check.setChecked(False)
        self._header_check.stateChanged.connect(self._on_header_check_changed)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(0, 30)
        self._table.setColumnWidth(1, 110)
        self._table.setColumnWidth(2, 180)
        self._table.setColumnWidth(3, 60)
        self._table.setColumnWidth(5, 80)
        # Disable sort indicator on checkbox column
        header.setSortIndicatorShown(True)

        # Place the checkbox widget over the first header section
        self._header_check.setParent(self._table.horizontalHeader())
        self._header_check.setGeometry(8, 3, 16, 16)
        header.sectionResized.connect(self._reposition_header_check)

        self._table.itemChanged.connect(self._on_item_changed)
        self._table.cellClicked.connect(self._on_cell_clicked)
        self._table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        main_layout.addWidget(self._table)

        # --- Analysis controls ---
        analysis_bar = QHBoxLayout()
        analysis_bar.setSpacing(6)

        # Promoted to instance attributes so ``_apply_styles`` can
        # repaint them on a theme change.  Previously these were
        # local variables only, which meant a light/dark switch
        # after construction left the labels on their
        # construction-time palette.
        self._mode_label = QLabel("Mode:")
        self._mode_label.setStyleSheet(get_bulk_mode_label_style())
        analysis_bar.addWidget(self._mode_label)

        self._quick_radio = QRadioButton("Quick")
        self._quick_radio.setStyleSheet(get_bulk_radio_style())
        self._quick_radio.setChecked(True)
        analysis_bar.addWidget(self._quick_radio)

        self._deep_radio = QRadioButton("Deep")
        self._deep_radio.setStyleSheet(get_bulk_radio_style())
        self._deep_radio.toggled.connect(lambda: self._update_selection_count())
        analysis_bar.addWidget(self._deep_radio)

        analysis_bar.addSpacing(12)

        self._batch_label = QLabel("Batch:")
        self._batch_label.setStyleSheet(get_bulk_mode_label_style())
        self._batch_label.setToolTip("Quick: functions per LLM prompt. Deep: ignored (1 agent per function).")
        analysis_bar.addWidget(self._batch_label)

        self._batch_input = QLineEdit("10")
        self._batch_input.setStyleSheet(get_bulk_num_input_style())
        self._batch_input.setValidator(QIntValidator(1, 999999))
        self._batch_input.setFixedWidth(50)
        self._batch_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._batch_input.setToolTip("Quick: functions per LLM prompt. Deep: ignored (1 agent per function).")
        self._batch_input.textChanged.connect(lambda: self._update_selection_count())
        analysis_bar.addWidget(self._batch_input)

        self._concurrent_label = QLabel("Jobs:")
        self._concurrent_label.setStyleSheet(get_bulk_mode_label_style())
        self._concurrent_label.setToolTip("Max parallel agents/requests running at the same time")
        analysis_bar.addWidget(self._concurrent_label)

        self._concurrent_input = QLineEdit("3")
        self._concurrent_input.setStyleSheet(get_bulk_num_input_style())
        self._concurrent_input.setValidator(QIntValidator(1, 999999))
        self._concurrent_input.setFixedWidth(50)
        self._concurrent_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._concurrent_input.setToolTip("Max parallel agents/requests running at the same time")
        analysis_bar.addWidget(self._concurrent_input)

        analysis_bar.addStretch()
        main_layout.addLayout(analysis_bar)

        # --- Action bar ---
        action_bar = QHBoxLayout()
        action_bar.setSpacing(4)

        self._start_btn = QPushButton("Start")
        self._start_btn.setStyleSheet(get_bulk_start_btn_style())
        self._start_btn.clicked.connect(self._on_start)
        action_bar.addWidget(self._start_btn)

        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setStyleSheet(get_bulk_stop_btn_style())
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop)
        action_bar.addWidget(self._stop_btn)

        self._pause_btn = QPushButton("Pause")
        self._pause_btn.setStyleSheet(get_bulk_btn_style())
        self._pause_btn.setEnabled(False)
        self._pause_btn.clicked.connect(self._on_pause_toggle)
        action_bar.addWidget(self._pause_btn)

        self._undo_btn = QPushButton("Undo All")
        self._undo_btn.setStyleSheet(get_bulk_btn_style())
        self._undo_btn.setEnabled(False)
        self._undo_btn.clicked.connect(self.undo_requested.emit)
        action_bar.addWidget(self._undo_btn)

        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.setStyleSheet(get_bulk_btn_style())
        self._refresh_btn.clicked.connect(self.refresh_requested.emit)
        action_bar.addWidget(self._refresh_btn)

        self._loading_label = QLabel("")
        self._loading_label.setStyleSheet(get_bulk_selection_label_style())
        self._loading_label.hide()
        action_bar.addWidget(self._loading_label)

        self._progress = QProgressBar()
        self._progress.setStyleSheet(get_bulk_progress_style())
        self._progress.setFixedHeight(18)
        self._progress.setValue(0)
        action_bar.addWidget(self._progress, 1)

        self._progress_label = QLabel("0 / 0")
        self._progress_label.setStyleSheet(get_bulk_selection_label_style())
        action_bar.addWidget(self._progress_label)

        main_layout.addLayout(action_bar)

        # Internal state
        self._entries: list[FunctionEntry] = []
        self._addr_to_entry: dict[int, int] = {}  # address -> index in _entries
        self._paused = False

        # Subscribe to theme changes now that every control is built.
        # ``bind_theme`` runs the callback synchronously so the
        # initial paint reflects the active palette.
        bind_theme(self, self._apply_styles)

    def _reposition_header_check(self, _idx: int = 0, _old: int = 0, _new: int = 0) -> None:
        """Keep the header checkbox centred in the first header section."""
        x = (self._table.columnWidth(0) - 16) // 2
        self._header_check.setGeometry(x, 3, 16, 16)

    def _on_header_check_changed(self, state: int) -> None:
        """Toggle all visible row checkboxes based on header checkbox."""
        checked = state == Qt.CheckState.Checked.value
        self._table.blockSignals(True)
        for row in range(self._table.rowCount()):
            if not self._table.isRowHidden(row):
                item = self._table.item(row, _COL_CHECK)
                if item:
                    item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self._table.blockSignals(False)
        self._update_selection_count()

    def _reset_header_check(self) -> None:
        """Reset the header checkbox to unchecked (without emitting signals)."""
        self._header_check.blockSignals(True)
        self._header_check.setChecked(False)
        self._header_check.blockSignals(False)

    def set_refresh_enabled(self, enabled: bool) -> None:
        """Enable or disable the Refresh button."""
        self._refresh_btn.setEnabled(enabled)

    def set_running_state(self, running: bool) -> None:
        """Set all action buttons to reflect whether an engine is running."""
        self._start_btn.setEnabled(not running)
        self._refresh_btn.setEnabled(not running)
        self._stop_btn.setEnabled(running)
        self._pause_btn.setEnabled(running)
        if not running:
            self._pause_btn.setText("Pause")
            self._paused = False

    def clear_functions(self) -> None:
        """Cancel any in-flight load, clear all table contents, and reset UI to idle."""
        self.cancel_function_load()
        self._table.setRowCount(0)
        self._entries.clear()
        self._addr_to_entry.clear()
        self._reset_header_check()
        self.hide_loading_state()
        self._update_selection_count()
        self.set_running_state(False)

    def show_loading_state(self, message: str = "Loading functions...") -> None:
        """Show the loading-state label (e.g. while chunked enumeration runs)."""
        self._loading_label.setText(message)
        self._loading_label.show()

    def hide_loading_state(self) -> None:
        """Hide the loading-state label."""
        self._loading_label.hide()
        self._loading_label.setText("")

    def begin_function_load(self) -> None:
        """Prepare the table for externally chunked function loading."""
        self.cancel_function_load()
        self._loading = True
        self._reset_header_check()
        self.show_loading_state("Loading functions...")
        self._table.blockSignals(True)
        self._table.setSortingEnabled(False)
        self._table.setRowCount(0)
        self._entries.clear()
        self._addr_to_entry.clear()

    def append_function_chunk(self, chunk: list[dict]) -> None:
        """Append one externally enumerated function chunk to the table."""
        if not chunk:
            return
        if not self._loading:
            self.begin_function_load()
        start = self._table.rowCount()
        self._table.setRowCount(start + len(chunk))
        self._populate_rows(chunk, start, start + len(chunk), source_start=start)

    def finish_function_load(self) -> None:
        """Complete a function load and restore normal table behaviour."""
        self._finish_load()

    def fail_function_load(self, message: str) -> None:
        """Abort a failed function load, clear stale data, and show the error."""
        self._cancel_chunked_load()
        self._table.setRowCount(0)
        self._entries.clear()
        self._addr_to_entry.clear()
        self._restore_after_load(message)

    def cancel_function_load(self) -> None:
        """Cancel any in-flight widget-side function load."""
        self._cancel_chunked_load()
        if self._loading:
            self._restore_after_load("")

    # Rows to insert per timer tick during chunked loading.
    _LOAD_CHUNK_SIZE = 200

    def load_functions(self, functions: list[dict]) -> None:
        """Populate the table from a list of function dicts.

        Each dict: {"address": int, "name": str, "is_import": bool, "size_bytes": int}

        For large lists the rows are inserted in chunks via a QTimer so the UI
        thread stays responsive (prevents the "blank panel" freeze).
        """
        # Cancel any in-flight chunked load
        self._cancel_chunked_load()

        self._loading = True
        self._table.setSortingEnabled(False)
        self._table.blockSignals(True)
        self._table.setRowCount(0)
        self._entries.clear()
        self._addr_to_entry.clear()

        self._table.setRowCount(len(functions))

        if len(functions) <= self._LOAD_CHUNK_SIZE:
            # Small list — populate synchronously for snappy feel
            self._populate_rows(functions, 0, len(functions))
            self._finish_load()
        else:
            # Large list — process in chunks to keep UI alive
            self._pending_functions = functions
            self._load_cursor = 0
            self._load_timer = QTimer(self)
            self._load_timer.setInterval(0)  # process next chunk ASAP
            self._load_timer.timeout.connect(self._load_next_chunk)
            self._load_timer.start()

    def _load_next_chunk(self) -> None:
        """Insert the next chunk of rows."""
        funcs = self._pending_functions
        start = self._load_cursor
        end = min(start + self._LOAD_CHUNK_SIZE, len(funcs))

        self._populate_rows(funcs, start, end, source_start=0)
        self._load_cursor = end

        if end >= len(funcs):
            self._finish_load()
            self._cancel_chunked_load()

    def _cancel_chunked_load(self) -> None:
        """Stop and clean up any in-flight chunked load timer."""
        timer = getattr(self, "_load_timer", None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
            self._load_timer = None
        self._pending_functions = []
        self._load_cursor = 0

    def _load_chunk_at(self, chunk: list[dict], offset: int) -> None:
        """Populate rows for a chunk of function data at *offset*.

        Called from the host panel during chunked enumeration.  Delegates
        to ``_populate_rows`` for both table population and entry tracking.
        """
        self._populate_rows(chunk, offset, offset + len(chunk))

    def _populate_rows(
        self,
        functions: list[dict],
        start: int,
        end: int,
        source_start: int = 0,
    ) -> None:
        """Insert rows [start, end) into the table.

        *source_start* is the 0-based index in *functions* that corresponds
        to row *start*:

        - Widget-chunked ``load_functions()`` passes the full list and
          calls with ``source_start=0`` (default) so that ``functions[row]``
          works correctly.
        - ``append_function_chunk()`` passes only the current chunk and
          calls with ``source_start=start`` so that ``functions[row - start]``
          indexes within the chunk correctly.

        For chunked loading, ensures ``_entries`` has enough capacity and
        places entries at the correct list indices.
        """
        for row in range(start, end):
            func = functions[row - source_start]
            entry = FunctionEntry(
                address=func["address"],
                name=func["name"],
                is_import=func.get("is_import", False),
                size_bytes=func.get("size_bytes", 0),
            )
            if row == len(self._entries):
                self._entries.append(entry)
            else:
                self._entries[row] = entry
            self._addr_to_entry[entry.address] = row

            sb = entry.size_bytes

            # Checkbox column
            check_item = QTableWidgetItem()
            is_auto = self._is_auto_named(entry.name)
            check_item.setCheckState(
                Qt.CheckState.Checked if (is_auto and not entry.is_import) else Qt.CheckState.Unchecked
            )
            check_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            self._table.setItem(row, _COL_CHECK, check_item)

            # Address (numeric sort, store address in UserRole for lookup)
            addr_item = _NumericTableItem(f"0x{entry.address:X}", entry.address)
            addr_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            addr_item.setData(Qt.ItemDataRole.UserRole, entry.address)
            addr_item.setToolTip(f"0x{entry.address:016X}")
            self._table.setItem(row, _COL_ADDR, addr_item)

            # Current name
            name_item = QTableWidgetItem(entry.name)
            name_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self._table.setItem(row, _COL_NAME, name_item)

            # Size (bytes) — numeric sort
            size_item = _NumericTableItem(str(sb) if sb else "0", sb)
            size_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._table.setItem(row, _COL_SIZE, size_item)

            # New name (initially empty)
            new_item = QTableWidgetItem("")
            new_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self._table.setItem(row, _COL_NEWNAME, new_item)

            # Status
            status_item = QTableWidgetItem("")
            status_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self._table.setItem(row, _COL_STATUS, status_item)

    def _finish_load(self) -> None:
        """Re-enable table features after load completes."""
        self._reset_header_check()
        self._restore_after_load("")
        self._update_selection_count()
        # Reapply any active filter — the table was rebuilt so the filter
        # state was lost.  This also correctly resets the selection-count
        # header after the refresh.
        self._on_filter_changed()

    def _restore_after_load(self, message: str) -> None:
        """Restore table state after success, failure, or cancellation."""
        self._table.blockSignals(False)
        self._table.setSortingEnabled(True)
        self._loading = False
        if message:
            self.show_loading_state(message)
        else:
            self.hide_loading_state()

    def update_job(self, address: int, new_name: str, status: str, error: str) -> None:
        """Update a row by address with new name, status, and optional error."""
        row = self._find_row_for_address(address)
        if row is None:
            return

        # Block signals to prevent sorting/item-change side-effects
        self._table.blockSignals(True)

        new_item = self._table.item(row, _COL_NEWNAME)
        if new_item:
            new_item.setText(new_name if new_name else "")

        status_item = self._table.item(row, _COL_STATUS)
        if status_item:
            display = error if error else status
            status_item.setText(display)
            status_colors = get_bulk_status_colors()
            color = status_colors.get(status, "#d4d4d4")
            from .qt_compat import QColor

            status_item.setForeground(QColor(color))

        self._table.blockSignals(False)

    def _find_row_for_address(self, address: int) -> int | None:
        """Find the current visual row for a given address (sort-safe)."""
        for row in range(self._table.rowCount()):
            addr_item = self._table.item(row, _COL_ADDR)
            if addr_item is not None and addr_item.data(Qt.ItemDataRole.UserRole) == address:
                return row
        return None

    def set_progress(self, current: int, total: int) -> None:
        """Update the progress bar and label."""
        if total > 0:
            self._progress.setMaximum(total)
            self._progress.setValue(current)
        else:
            self._progress.setMaximum(1)
            self._progress.setValue(0)
        self._progress_label.setText(f"{current} / {total}")

        # Enable undo if any work has been done
        self._undo_btn.setEnabled(current > 0)

        # Toggle buttons based on completion
        if current >= total and total > 0:
            self._start_btn.setEnabled(True)
            self._refresh_btn.setEnabled(True)
            self._stop_btn.setEnabled(False)
            self._pause_btn.setEnabled(False)
            self._pause_btn.setText("Pause")
            self._paused = False

    def _on_cell_clicked(self, row: int, column: int) -> None:
        """Handle single-click: toggle checkboxes for multi-select."""
        if column == _COL_CHECK:
            selected_rows = {idx.row() for idx in self._table.selectionModel().selectedRows()}
            if len(selected_rows) > 1 and row in selected_rows:
                clicked_item = self._table.item(row, _COL_CHECK)
                if clicked_item is None:
                    return
                new_state = clicked_item.checkState()
                self._table.blockSignals(True)
                for r in selected_rows:
                    item = self._table.item(r, _COL_CHECK)
                    if item:
                        item.setCheckState(new_state)
                self._table.blockSignals(False)
                self._update_selection_count()

    def _on_cell_double_clicked(self, row: int, column: int) -> None:
        """Double-click on Address, Name, or New Name navigates to that function."""
        if column in (_COL_ADDR, _COL_NAME, _COL_NEWNAME):
            entry = self._entry_for_row(row)
            if entry is not None:
                self.seek_requested.emit(entry.address)

    def _on_pause_toggle(self) -> None:
        """Toggle pause/resume and update button text."""
        self._paused = not self._paused
        self._pause_btn.setText("Resume" if self._paused else "Pause")
        self.pause_requested.emit()

    def _on_stop(self) -> None:
        """Stop the running renamer engine."""
        self._stop_btn.setEnabled(False)
        self._pause_btn.setEnabled(False)
        self._pause_btn.setText("Pause")
        self._start_btn.setEnabled(True)
        self._refresh_btn.setEnabled(True)
        self._paused = False
        self.cancel_requested.emit()

    def _entry_for_row(self, row: int) -> FunctionEntry | None:
        """Get the FunctionEntry for a visual table row (sort-safe)."""
        addr_item = self._table.item(row, _COL_ADDR)
        if addr_item is None:
            return None
        addr = addr_item.data(Qt.ItemDataRole.UserRole)
        if addr is None:
            return None
        idx = self._addr_to_entry.get(addr)
        return self._entries[idx] if idx is not None else None

    def _schedule_filter(self, _text: str = "") -> None:
        """Restart the debounce timer for the text-filter changes.

        Connected to ``self._filter_edit.textChanged``.  The actual
        scan happens in :meth:`_on_filter_changed` once the timer
        fires, so a rapid sequence of keystrokes only triggers one
        table walk.
        """
        # Restart the single-shot timer on every keystroke.  A running
        # single-shot timer resets its deadline on ``.start()`` — that
        # is the standard Qt debounce idiom.
        try:
            self._filter_timer.start()
        except Exception:
            # Fallback: run synchronously if the timer object is gone
            # (e.g. during widget teardown).
            self._on_filter_changed()

    def _on_filter_changed(self) -> None:
        """Filter table rows based on text filter and combo selection.

        Skipped during a chunked load (``_loading``) so we don't pay
        for an O(rows) scan on every chunk.  ``_finish_load`` runs
        this once after the last chunk is inserted.
        """
        if self._loading:
            return

        text = self._filter_edit.text().strip().lower()
        combo_idx = self._filter_combo.currentIndex()

        for row in range(self._table.rowCount()):
            entry = self._entry_for_row(row)
            if entry is None:
                continue
            name = entry.name.lower()

            # Text filter — match name or hex address
            text_match = not text or text in name or text in f"0x{entry.address:x}" or text in f"0x{entry.address:X}"

            # Combo filter
            combo_match = True
            if combo_idx == 1:  # Auto-named Only
                combo_match = self._is_auto_named(entry.name)
            elif combo_idx == 2:  # User-renamed
                combo_match = not self._is_auto_named(entry.name) and not entry.is_import
            elif combo_idx == 3:  # Imports
                combo_match = entry.is_import

            self._table.setRowHidden(row, not (text_match and combo_match))

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        """Track checkbox state changes."""
        if item.column() == _COL_CHECK:
            self._update_selection_count()

    def _get_selected_jobs(self) -> list[dict]:
        """Return list of dicts with address and current_name for checked rows."""
        jobs = []
        for row in range(self._table.rowCount()):
            check_item = self._table.item(row, _COL_CHECK)
            if check_item and check_item.checkState() == Qt.CheckState.Checked:
                entry = self._entry_for_row(row)
                if entry is not None:
                    jobs.append(
                        {
                            "address": entry.address,
                            "current_name": entry.name,
                        }
                    )
        return jobs

    def _batch_value(self) -> int:
        """Parse batch size from the text input, default 10."""
        try:
            return max(1, int(self._batch_input.text()))
        except (ValueError, TypeError):
            return 10

    def _concurrent_value(self) -> int:
        """Parse concurrent jobs from the text input, default 3."""
        try:
            return max(1, int(self._concurrent_input.text()))
        except (ValueError, TypeError):
            return 3

    def _on_start(self) -> None:
        """Collect selected functions and emit start_requested."""
        jobs = self._get_selected_jobs()
        if not jobs:
            return
        mode = "deep" if self._deep_radio.isChecked() else "quick"
        batch_size = self._batch_value()
        max_concurrent = self._concurrent_value()

        self._start_btn.setEnabled(False)
        self._refresh_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._pause_btn.setEnabled(True)
        self._pause_btn.setText("Pause")
        self._paused = False

        # Mark selected jobs as queued
        for job in jobs:
            self.update_job(job["address"], "", "queued", "")

        self.set_progress(0, len(jobs))
        self.start_requested.emit(jobs, mode, batch_size, max_concurrent)

    def _update_selection_count(self) -> None:
        """Update the selection count label with subagent estimation."""
        total = self._table.rowCount()
        selected = 0
        for row in range(total):
            item = self._table.item(row, _COL_CHECK)
            if item and item.checkState() == Qt.CheckState.Checked:
                selected += 1

        if self._deep_radio.isChecked() and selected > 0:
            self._selection_label.setText(f"{selected} / {total} selected \u2022 {selected} subagents")
        else:
            batch = self._batch_value()
            batches = (selected + batch - 1) // batch if selected > 0 else 0
            if selected > 0:
                self._selection_label.setText(f"{selected} / {total} selected \u2022 {batches} batch(es)")
            else:
                self._selection_label.setText(f"{selected} / {total} selected")

    def select_and_filter_address(self, address: int) -> None:
        """Filter to a specific address and check it — used by send_to_bulk_rename."""
        addr_str = f"0x{address:x}"
        self._filter_edit.setText(addr_str)

        row = self._find_row_for_address(address)
        if row is not None:
            self._table.blockSignals(True)
            item = self._table.item(row, _COL_CHECK)
            if item:
                item.setCheckState(Qt.CheckState.Checked)
            self._table.blockSignals(False)
            self._update_selection_count()

    @staticmethod
    def _is_auto_named(name: str) -> bool:
        """Heuristic: detect auto-generated function names."""
        prefixes = ("sub_", "fn_", "loc_", "j_", "nullsub_", "unknown_", "FUN_")
        return name.startswith(prefixes)

    def _apply_styles(self, _tokens: object = None) -> None:
        """Re-apply every per-widget QSS from the live tokens.

        Walks each known control in order and resets its stylesheet
        from the matching token-driven getter.  Then re-paints the
        Status column on existing rows so a theme switch after a
        batch of jobs are queued / completed updates their colours
        against the new palette without losing the status text.

        Runtime state that the user has built up is NOT reset:
        selection count, filter text, header checkbox, progress bar
        value, button enabled state.  ``_apply_styles`` only touches
        visual properties.
        """
        # Top bar.
        if getattr(self, "_filter_edit", None) is not None:
            self._filter_edit.setStyleSheet(get_bulk_filter_style())
        if getattr(self, "_filter_combo", None) is not None:
            self._filter_combo.setStyleSheet(get_bulk_combo_style())
        if getattr(self, "_selection_label", None) is not None:
            self._selection_label.setStyleSheet(get_bulk_selection_label_style())
        # Table.
        if getattr(self, "_table", None) is not None:
            self._table.setStyleSheet(get_bulk_table_style())
        if getattr(self, "_header_check", None) is not None:
            self._header_check.setStyleSheet(get_bulk_check_style())
        # Analysis bar — promote the previously-local labels so we can
        # repaint them too.
        if getattr(self, "_mode_label", None) is not None:
            self._mode_label.setStyleSheet(get_bulk_mode_label_style())
        if getattr(self, "_batch_label", None) is not None:
            self._batch_label.setStyleSheet(get_bulk_mode_label_style())
        if getattr(self, "_concurrent_label", None) is not None:
            self._concurrent_label.setStyleSheet(get_bulk_mode_label_style())
        if getattr(self, "_quick_radio", None) is not None:
            self._quick_radio.setStyleSheet(get_bulk_radio_style())
        if getattr(self, "_deep_radio", None) is not None:
            self._deep_radio.setStyleSheet(get_bulk_radio_style())
        if getattr(self, "_batch_input", None) is not None:
            self._batch_input.setStyleSheet(get_bulk_num_input_style())
        if getattr(self, "_concurrent_input", None) is not None:
            self._concurrent_input.setStyleSheet(get_bulk_num_input_style())
        # Action bar.
        if getattr(self, "_start_btn", None) is not None:
            self._start_btn.setStyleSheet(get_bulk_start_btn_style())
        if getattr(self, "_stop_btn", None) is not None:
            self._stop_btn.setStyleSheet(get_bulk_stop_btn_style())
        if getattr(self, "_pause_btn", None) is not None:
            self._pause_btn.setStyleSheet(get_bulk_btn_style())
        if getattr(self, "_undo_btn", None) is not None:
            self._undo_btn.setStyleSheet(get_bulk_btn_style())
        if getattr(self, "_refresh_btn", None) is not None:
            self._refresh_btn.setStyleSheet(get_bulk_btn_style())
        if getattr(self, "_loading_label", None) is not None:
            self._loading_label.setStyleSheet(get_bulk_selection_label_style())
        if getattr(self, "_progress", None) is not None:
            self._progress.setStyleSheet(get_bulk_progress_style())
        if getattr(self, "_progress_label", None) is not None:
            self._progress_label.setStyleSheet(get_bulk_selection_label_style())
        # Re-apply status colours on existing rows.  The status text
        # is read from the cell so the foreground re-tints against
        # the new palette without losing the in-flight result.
        self._refresh_row_status_colors()

    def _refresh_row_status_colors(self) -> None:
        """Repaint the Status column on every visible row.

        Walks ``self._table`` row-by-row, reads the cell text, and
        looks up the matching colour in the current
        ``BULK_STATUS_COLORS`` dict.  Rows with an empty status
        cell are skipped (their foreground defaults to the table's
        inherited text colour).

        Block-table signals for the duration of the walk so a
        status refresh does not re-fire ``_on_item_changed`` and
        mutate selection counts.
        """
        if getattr(self, "_table", None) is None:
            return
        try:
            from .qt_compat import QColor
        except Exception:
            return
        status_colors = get_bulk_status_colors()
        self._table.blockSignals(True)
        try:
            for row in range(self._table.rowCount()):
                item = self._table.item(row, _COL_STATUS)
                if item is None or not item.text():
                    continue
                # Map the displayed text back to a status key.
                # The cell text is the status name itself, except
                # for ``error`` rows where ``update_job`` writes the
                # error message instead of the status.  In that case
                # fall back to the ``error`` colour.
                text = item.text()
                color = status_colors.get(text, status_colors.get("error", "#d4d4d4"))
                item.setForeground(QColor(color))
        finally:
            self._table.blockSignals(False)

    def shutdown(self) -> None:
        """Detach the theme subscription (idempotent).

        Matches the panel-level teardown contract: callers do not
        need to handle ``disconnect_theme`` exceptions because
        :func:`lucnhan.ui.theme.applicator.disconnect_theme` already
        swallows the ``RuntimeError``/``TypeError``/``SystemError``
        family that PySide6 raises during teardown.
        """
        disconnect_theme(self)
