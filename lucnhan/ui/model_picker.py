"""Footer model picker — grouped, searchable, native Qt (contract G6).

The footer trigger opens a ``QFrame`` popup anchored **above** the trigger,
left-aligned, 8 px gap, clamped into the available screen and the dock.
Order: ``Model & thinking`` title → search → grouped model list →
``Thinking level`` control → support/busy hint.

Design constraints this file deliberately honours:

* **No fetch on open / no fetch on typing.**  The catalog is a cached
  ``dict[str, list[ModelInfo]]`` handed to :meth:`ModelPicker.set_catalog`;
  this module imports no provider, HTTP or SDK symbol at all, so search is
  a pure local substring test.
* **Identity is item data.**  Every row stores ``(connection_name,
  model_id)`` under ``UserRole``; nothing ever parses the display text, so
  two connections advertising the same model ID stay independent rows.
* **Keyboard selection is the same selection.**  Down/Up in the search
  box or on the list walks the rows the user can actually see — headers
  and rows the query hid are skipped — and Enter commits exactly one
  model through :meth:`commit_current`, which stays the single guarded
  path (busy and no-match commit nothing).  ``eventFilter`` claims
  Return/Enter, which a matching host shortcut would otherwise consume
  before the key ever reached the list.
* **Filtering hides rows, it does not rebuild them.**  The normalized
  search text is computed once per ``set_catalog`` and filtering toggles
  ``setHidden`` on the existing items.
* **No recurring work.**  No timers, no polling: the popup reacts to the
  search field, the tree and the lifecycle calls the panel already makes.
* **Commit is the panel's job.**  ``on_select`` hands a frozen
  :class:`ModelChoice` to ``SessionControllerBase.select_model``; the
  footer and context metadata are updated only after that call succeeds.

Levels come from :mod:`lucnhan.core.thinking` — the same registry the
Settings dialog uses — so popup and Settings can never disagree.  A model
absent from the registry keeps the permissive fallback and says so.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..core.thinking import (
    default_thinking_level,
    get_thinking_levels,
    has_model_thinking_levels,
)
from .qt_compat import (
    QApplication,
    QComboBox,
    QEvent,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QObject,
    QPushButton,
    QSize,
    Qt,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

#: Nominal popup geometry (GUI contract G6).
NOMINAL_WIDTH = 360
MAX_HEIGHT = 420
PADDING = 12
TRIGGER_GAP = 8
#: G3: the trigger may not eat more than 68% of the footer.
MAX_TRIGGER_WIDTH_RATIO = 0.68

CONNECTION_ROW_HEIGHT = 24
MODEL_ROW_HEIGHT = 28

NO_MATCHES_TEXT = "No matching models"
OPEN_SETTINGS_TEXT = "Open Settings to refresh models"
BUSY_HINT = "Stop the running agent to change model"
UNCONFIRMED_HINT = "Thinking levels are not confirmed for this model"
PLACEHOLDER_SEARCH = "Search models"

_ROLE_IDENTITY = Qt.ItemDataRole.UserRole
_ROLE_META = Qt.ItemDataRole.UserRole + 1


@dataclass(frozen=True)
class ModelChoice:
    """One committed selection: connection + model + thinking level."""

    connection_name: str
    model_id: str
    thinking_level: str


@dataclass(frozen=True)
class _Row:
    """A catalog row. Identity is the (connection, model) pair."""

    connection_name: str
    model_id: str
    model_name: str = ""
    provider: str = ""
    search_text: str = ""


class ModelCatalogCache:
    """Per-connection model catalog cache owned by the panel/controller.

    Populated from Settings Refresh results.  A connection is dropped when
    its endpoint / credential / connection config changes — thinking
    selection and theme switches must NOT drop it (the fetched list is
    still valid).  Only ``ModelInfo`` display metadata is stored, never a
    credential.
    """

    def __init__(self) -> None:
        self._by_connection: dict[str, list[Any]] = {}

    def store(self, connection_name: str, models: list[Any]) -> None:
        """Cache a Refresh result.  An empty list means "not fetched yet"."""
        name = (connection_name or "").strip()
        if not name:
            return
        if models:
            self._by_connection[name] = list(models)
        else:
            self._by_connection.pop(name, None)

    def get(self, connection_name: str) -> list[Any] | None:
        return self._by_connection.get((connection_name or "").strip())

    def catalog_for(self, connection_names: list[str]) -> dict[str, list[Any]]:
        """Cached catalog for the requested connections (missing = absent)."""
        out: dict[str, list[Any]] = {}
        for name in connection_names:
            models = self._by_connection.get((name or "").strip())
            if models:
                out[name] = list(models)
        return out

    def note_connection_changed(self, connection_name: str, **_changed: Any) -> None:
        """Endpoint / credential / connection config changed → drop it."""
        self._by_connection.pop((connection_name or "").strip(), None)

    def note_selection_committed(self, connection_name: str, model_id: str, level: str) -> None:
        """Thinking/model selection does not invalidate the catalog."""

    def note_theme_changed(self) -> None:
        """Theme changes do not invalidate the catalog."""


class ModelPicker(QFrame):
    """Popup listing every model of every configured connection."""

    def __init__(
        self,
        parent: QWidget | None = None,
        on_select: Callable[[ModelChoice], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("model_picker")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWindowFlags(
            Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint
        )
        self._on_select = on_select or (lambda _choice: None)
        self._on_open_settings: Callable[[], None] = lambda: None

        self._rows: list[_Row] = []
        self._haystacks: dict[int, str] = {}
        self._catalog: dict[str, list[Any]] = {}
        # True once at least one connection contributed a FETCHED list;
        # an empty/partial catalog is the unfetched state G6 describes.
        self._has_fetched_catalog = False
        self._items: list[QTreeWidgetItem | None] = []
        self._headers: dict[str, QTreeWidgetItem] = {}
        self._hidden: list[bool] = []
        self._busy = False
        self._selection = ModelChoice("", "", "")
        self._pending_identity: tuple[str, str] | None = None
        self._trigger: QWidget | None = None
        self._query = ""
        self._levels: tuple[str, ...] = ()
        self._updating = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(PADDING, PADDING, PADDING, PADDING)
        layout.setSpacing(6)

        self._title = QLabel("Model & thinking")
        self._title.setObjectName("model_picker_title")
        layout.addWidget(self._title)

        self._search = QLineEdit()
        self._search.setPlaceholderText(PLACEHOLDER_SEARCH)
        self._search.setClearButtonEnabled(True)
        self._search.setAccessibleName("Search models")
        self._search.textChanged.connect(self._on_search_changed)
        layout.addWidget(self._search)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setRootIsDecorated(False)
        self._tree.setIndentation(0)
        self._tree.setColumnCount(1)
        self._tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self._tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._tree.setMinimumHeight(CONNECTION_ROW_HEIGHT * 2)
        self._tree.itemSelectionChanged.connect(self._on_item_selected)
        self._tree.itemActivated.connect(self._on_item_activated)
        self._tree.clicked.connect(self._on_tree_clicked)
        layout.addWidget(self._tree, 1)

        self._thinking_row = QHBoxLayout()
        self._thinking_row.setContentsMargins(0, 0, 0, 0)
        self._thinking_row.setSpacing(6)
        self._thinking_label = QLabel("Thinking level")
        self._thinking_label.setObjectName("model_picker_thinking_label")
        self._thinking_row.addWidget(self._thinking_label)
        self._thinking = QComboBox()
        self._thinking.setAccessibleName("Thinking level")
        self._thinking.currentTextChanged.connect(self._on_thinking_changed)
        self._thinking_row.addWidget(self._thinking, 1)
        layout.addLayout(self._thinking_row)

        self._hint = QLabel("")
        self._hint.setObjectName("model_picker_hint")
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)

        self._settings_btn = QPushButton(OPEN_SETTINGS_TEXT)
        self._settings_btn.setObjectName("model_picker_settings")
        self._settings_btn.setVisible(False)
        self._settings_btn.clicked.connect(self.open_settings)
        layout.addWidget(self._settings_btn)

        # Qt.Popup delivers the outside click / Escape itself; the event
        # filter is what returns focus to the trigger on the way out.
        self.installEventFilter(self)
        self._search.installEventFilter(self)
        self._tree.installEventFilter(self)
        self.refresh_theme()

    # ------------------------------------------------------------------
    # Catalog
    # ------------------------------------------------------------------

    def set_catalog(self, models_by_connection: dict[str, list[Any]]) -> None:
        """Install the cached catalog and rebuild the grouped rows.

        The normalized search text is computed HERE, once per catalog; the
        filter only reads it.  The configured model is always present as a
        real row — with no fetched list at all that row *is* the list, next
        to the ``Open Settings to refresh models`` entry.
        """
        self._catalog = {name: list(models or []) for name, models in (models_by_connection or {}).items()}
        self._has_fetched_catalog = any(bool(models) for models in self._catalog.values())
        self._sync_rows()

    def _sync_rows(self) -> None:
        """Rebuild rows from the cached catalog + the configured model."""
        groups = {name: list(models) for name, models in self._catalog.items()}
        current = self._selection

        rows: list[_Row] = []
        # A configured model with no fetched list still gets a real row
        # (manual / custom / not-yet-refreshed models are valid picks).
        if current.model_id:
            known = {(name, getattr(m, "id", "")) for name, models in groups.items() for m in models}
            if (current.connection_name, current.model_id) not in known:
                groups.setdefault(current.connection_name, []).append(_Row(current.connection_name, current.model_id))

        haystacks: dict[int, str] = {}
        for name, models in groups.items():
            for model in models:
                # ``_Row`` rows (the configured model with no fetched
                # catalog) are already row-shaped; ``ModelInfo`` is not.
                if isinstance(model, _Row):
                    row = model
                else:
                    model_id = getattr(model, "id", "") or ""
                    if not model_id:
                        continue
                    row = _Row(
                        connection_name=name,
                        model_id=model_id,
                        model_name=getattr(model, "name", "") or "",
                        provider=getattr(model, "provider", "") or "",
                    )
                haystacks[len(rows)] = _normalize(f"{row.connection_name} {row.model_id} {row.model_name}")
                rows.append(row)

        self._rows = rows
        self._haystacks = haystacks
        self._rebuild_tree()
        self._restore_identity(current)

    def _rebuild_tree(self) -> None:
        tree = self._tree
        tree.clear()
        self._items = [None] * len(self._rows)
        self._hidden = [True] * len(self._rows)
        self._headers = {}
        group_items = self._headers
        for index, row in enumerate(self._rows):
            header = group_items.get(row.connection_name)
            if header is None:
                header = QTreeWidgetItem([row.connection_name])
                _style_item(header, CONNECTION_ROW_HEIGHT, bold=True)
                group_items[row.connection_name] = header
                tree.addTopLevelItem(header)
            item = QTreeWidgetItem([self._display_label(row)])
            item.setData(0, _ROLE_IDENTITY, (row.connection_name, row.model_id))
            item.setData(0, _ROLE_META, (row.model_name, row.provider))
            _style_item(item, MODEL_ROW_HEIGHT)
            header.addChild(item)
            self._items[index] = item
        self._apply_filter(self._query)

    @staticmethod
    def _display_label(row: _Row) -> str:
        """Model ID + real name metadata, plus the active checkmark."""
        text = row.model_id
        if row.model_name and row.model_name != row.model_id:
            text = f"{row.model_id} — {row.model_name}"
        return text

    # ------------------------------------------------------------------
    # Search / rows
    # ------------------------------------------------------------------

    def set_search_text(self, text: str) -> None:
        if self._search.text() != text:
            self._search.setText(text)
            return
        self._apply_filter(text)

    def search_text(self) -> str:
        return self._search.text()

    def clear_search(self) -> None:
        self._search.clear()

    def _on_search_changed(self, text: str) -> None:
        self._apply_filter(text)

    def _on_tree_clicked(self, index: int) -> None:
        """A click commits, the way a native list behaves."""
        item = self._tree.currentItem()
        if item is None:
            return
        identity = item.data(0, _ROLE_IDENTITY)
        if identity:
            self._on_identity_selected(identity[0], identity[1])
            self.commit_current()

    def _move_visible(self, key: object) -> bool:
        """Walk the *visible* rows: headers and query-hidden rows are skipped.

        ``QTreeWidget``'s own arrow handling steps over sibling tree
        items, so it walks onto a connection header — a row with no
        identity at all — and then Enter had nothing to commit.  Both
        the search box and the tree therefore share this one walk.
        """
        visible = self.visible_rows()
        if not visible:
            return True
        current = self._tree.currentItem()
        identity = current.data(0, _ROLE_IDENTITY) if current is not None else None
        current_index = next(
            (
                position
                for position, row in enumerate(visible)
                if identity and row.connection_name == identity[0] and row.model_id == identity[1]
            ),
            -1,
        )
        step = 1 if key == Qt.Key.Key_Down else -1
        target = min(max(current_index + step, 0), len(visible) - 1)
        row = visible[target]
        self.select_identity(row.connection_name, row.model_id)
        return True

    def _on_search_key(self, event: object) -> bool:
        """Navigate visible rows or commit; return True when consumed."""
        key = getattr(event, "key", lambda: None)()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # ``commit_current`` is the single guarded commit path, so a
            # busy picker commits nothing; a query with no visible row
            # has nothing to commit either.
            if self.visible_rows():
                self.commit_current()
            return True
        if key not in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            return False
        return self._move_visible(key)

    def _on_tree_key(self, event: object) -> bool:
        """Enter commits the highlighted row; Up/Down walk the same rows."""
        key = getattr(event, "key", lambda: None)()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._on_tree_clicked(0)
            return True
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            return self._move_visible(key)
        return False

    def _apply_filter(self, query: str) -> None:
        """Show/hide existing rows — never rebuild, never fetch."""
        needle = _normalize(query)
        self._query = query
        groups: set[str] = set()
        visible = 0
        hidden = self._hidden
        for index, row in enumerate(self._rows):
            item = self._items[index]
            if item is None:
                continue
            hay = self._haystacks.get(index, "")
            hit = not needle or needle in hay
            if hit:
                visible += 1
                groups.add(row.connection_name)
            # Only touch Qt when the state actually changes: setHidden on
            # an unchanged row still walks the model and repaints.
            if hit == hidden[index]:
                continue
            hidden[index] = hit
            item.setHidden(not hit)
        for name, header in self._headers.items():
            want_hidden = name not in groups
            if header.isHidden() != want_hidden:
                header.setHidden(want_hidden)

        # The list selection follows the query: when the current item
        # died with the query the first surviving *row* becomes current
        # — a connection header is not a model and must never win that
        # role — so Enter commits what the user can actually see.
        # Committing is still an explicit action; nothing is persisted.
        current = self._tree.currentItem()
        if current is not None and not current.data(0, _ROLE_IDENTITY):
            current = None
        if current is None or current.isHidden():
            for index, row in enumerate(self._rows):
                item = self._items[index]
                if item is not None and not item.isHidden():
                    self._tree.setCurrentItem(item)
                    self._on_identity_selected(row.connection_name, row.model_id)
                    break

        if self._busy:
            self._hint.setText(BUSY_HINT)
        elif visible == 0:
            self._hint.setText(NO_MATCHES_TEXT)
        else:
            self._hint.setText("" if has_model_thinking_levels(self._selection.model_id) else UNCONFIRMED_HINT)
        # G6: the entry belongs to the UNFETCHED state, which is about the
        # catalog — not about the row count.  ``_sync_rows`` always injects
        # the configured model as a real row, so gating on "no rows" hid the
        # entry for exactly the users who need it (fresh installs, ollama /
        # custom connections with no builtin list).  A search that matches
        # nothing still keeps it visible: the query, not the catalog, is the
        # reason the list looks empty.
        self._settings_btn.setVisible(not self._has_fetched_catalog)

    def model_rows(self) -> list[_Row]:
        """Every catalog row, in display order."""
        return list(self._rows)

    def visible_rows(self) -> list[_Row]:
        return [
            row
            for index, row in enumerate(self._rows)
            if self._items[index] is not None and not self._items[index].isHidden()
        ]

    def item_for(self, connection_name: str, model_id: str) -> QTreeWidgetItem | None:
        """Row for the (connection, model) pair — via item data, not text."""
        for index, row in enumerate(self._rows):
            if row.connection_name == connection_name and row.model_id == model_id:
                return self._items[index]
        return None

    def search_haystacks(self) -> dict[str, str]:
        """Exposed for tests: the normalized text cached at set_catalog."""
        return {
            f"{row.connection_name}/{row.model_id}": self._haystacks.get(index, "")
            for index, row in enumerate(self._rows)
        }

    # ------------------------------------------------------------------
    # Selection / thinking
    # ------------------------------------------------------------------

    def set_selection(self, choice: ModelChoice) -> None:
        self._selection = choice
        if self.item_for(choice.connection_name, choice.model_id) is None:
            # The configured model is not in any fetched catalog yet —
            # it still has to be a real, selectable row (G6's unfetched
            # state).  The thinking combo still has to be filled: it is the
            # first thing the popup shows, and returning early here left it
            # empty until the user picked a row or reopened the popup.
            self._sync_rows()
            self._refresh_thinking()
            return
        self._restore_identity(choice)
        self._refresh_thinking()

    def _restore_identity(self, choice: ModelChoice) -> None:
        self._pending_identity = (choice.connection_name, choice.model_id)
        self._refresh_checkmarks()
        item = self.item_for(choice.connection_name, choice.model_id)
        if item is not None and not item.isHidden():
            self._tree.setCurrentItem(item)
            self._pending_identity = None

    def _refresh_checkmarks(self) -> None:
        for index, row in enumerate(self._rows):
            item = self._items[index]
            if item is None:
                continue
            active = row.connection_name == self._selection.connection_name and row.model_id == self._selection.model_id
            base = self._display_label(row)
            item.setText(0, ("✓  " + base) if active else ("    " + base))

    def select_identity(self, connection_name: str, model_id: str) -> None:
        """Move the list selection to the (connection, model) pair."""
        item = self.item_for(connection_name, model_id)
        if item is None or item.isHidden():
            return
        self._tree.setCurrentItem(item)
        self._on_identity_selected(connection_name, model_id)

    def _on_item_selected(self) -> None:
        item = self._tree.currentItem()
        if item is None:
            return
        identity = item.data(0, _ROLE_IDENTITY)
        if identity:
            self._on_identity_selected(identity[0], identity[1])

    def _on_item_activated(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        identity = item.data(0, _ROLE_IDENTITY)
        if identity:
            self._on_identity_selected(identity[0], identity[1])
            self.commit_current()

    def _on_identity_selected(self, connection_name: str, model_id: str) -> None:
        self._pending_identity = (connection_name, model_id)
        self._refresh_thinking()

    def thinking_levels(self) -> tuple[str, ...]:
        return self._levels

    def thinking_level(self) -> str:
        return self._thinking.currentText() or "none"

    def thinking_is_confirmed(self) -> bool:
        return has_model_thinking_levels(self.current_choice().model_id)

    def set_thinking_level(self, level: str) -> None:
        index = self._thinking.findText(level)
        if index < 0:
            index = self._thinking.findText(default_thinking_level(self._levels))
        if index >= 0:
            self._thinking.setCurrentIndex(index)

    def _on_thinking_changed(self, text: str) -> None:
        if self._updating or not text:
            return
        self._selection = ModelChoice(
            self.current_choice().connection_name,
            self.current_choice().model_id,
            text,
        )
        self.commit_current()

    def _refresh_thinking(self) -> None:
        """Levels come from the shared core registry — never guessed here."""
        choice = self.current_choice()
        levels = get_thinking_levels(choice.model_id)
        self._levels = levels
        # Keep the level the user already chose when the new model accepts
        # it; otherwise use the registry default.  ``current_choice`` cannot
        # be the source here: the combo still holds the OLD model's level
        # at this point.  "none" is a real choice, so only a model-less
        # selection lets the default win.
        wanted = self._selection.thinking_level
        if wanted not in levels or not choice.model_id:
            wanted = default_thinking_level(levels)
        self._updating = True
        try:
            self._thinking.clear()
            self._thinking.addItems(list(levels))
            self._thinking.setCurrentIndex(levels.index(wanted))
        finally:
            self._updating = False
        enabled = not self._busy and bool(choice.model_id)
        self._thinking.setEnabled(enabled)
        self._thinking_label.setEnabled(enabled)

    def current_choice(self) -> ModelChoice:
        """The selection the footer would commit right now."""
        identity = self._pending_identity or (self._selection.connection_name, self._selection.model_id)
        levels = get_thinking_levels(identity[1])
        level = self._thinking.currentText() or self._selection.thinking_level or "none"
        if level not in levels:
            level = default_thinking_level(levels)
        return ModelChoice(identity[0], identity[1], level)

    def commit_current(self) -> bool:
        """Hand the current choice to the panel.  Blocked while busy."""
        if self._busy:
            self._hint.setText(BUSY_HINT)
            return False
        choice = self.current_choice()
        if not choice.connection_name or not choice.model_id:
            return False
        self._selection = choice
        self._pending_identity = (choice.connection_name, choice.model_id)
        self._refresh_checkmarks()
        self._on_select(choice)
        return True

    # ------------------------------------------------------------------
    # Busy / status
    # ------------------------------------------------------------------

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self._tree.setEnabled(not self._busy)
        self._refresh_thinking()
        self._apply_filter(self._query)
        if self._busy:
            self._hint.setText(BUSY_HINT)

    def status_text(self) -> str:
        return self._hint.text()

    def settings_entry_text(self) -> str:
        return self._settings_btn.text()

    def settings_entry_is_visible(self) -> bool:
        """True when the ``Open Settings to refresh models`` entry shows."""
        return self._settings_btn.isVisible()

    def set_open_settings_callback(self, callback: Callable[[], None]) -> None:
        self._on_open_settings = callback

    def open_settings(self) -> None:
        self._on_open_settings()

    # ------------------------------------------------------------------
    # Open / close
    # ------------------------------------------------------------------

    def open_above(self, anchor: QWidget) -> None:
        """Position above *anchor*, left-aligned, clamped into the dock/screen."""
        self._trigger = anchor
        width = self._popup_width(anchor)
        height = min(MAX_HEIGHT, max(self._tree.sizeHint().height() + 120, 160))
        # G6 nominal width is 360 px, clamped to dock-24 / screen-24.  Do
        # NOT call ``adjustSize()``: it overrides this with the layout's
        # size hint (282 px at a 1100 px dock), which silently discarded the
        # nominal width and crowded the thinking combo against the edge.
        self.resize(width, height)
        x, y = self._anchor_point(anchor, width)
        self.move(x, y)
        self.show()
        self.raise_()
        # Focus the search but keep the previous query and caret (G6:
        # reopening must not destroy what the user was narrowing).
        self._search.setFocus(Qt.FocusReason.OtherFocusReason)

    def close_popup(self) -> None:
        """Close and hand focus back to the trigger that opened it."""
        self.hide()
        trigger = self._trigger
        if trigger is not None and getattr(trigger, "isVisible", bool)():
            trigger.setFocus()

    def _popup_width(self, anchor: QWidget) -> int:
        dock = _dock_width(anchor)
        screen = self._available_width()
        limits = [NOMINAL_WIDTH, screen - 24]
        if dock:
            limits.append(dock - 24)
        return max(120, min(limits))

    def _available_width(self) -> int:
        screen = _screen_of(self)
        geometry = screen.availableGeometry() if screen is not None else None
        return int(geometry.width()) if geometry is not None else NOMINAL_WIDTH

    def _anchor_point(self, anchor: QWidget, width: int) -> tuple[int, int]:
        if not hasattr(anchor, "mapToGlobal"):  # pragma: no cover - stub path
            return (0, 0)
        bottom_left = anchor.mapToGlobal(anchor.rect().bottomLeft())
        screen = _screen_of(self)
        geometry = screen.availableGeometry() if screen is not None else None
        x = int(bottom_left.x())
        y = int(bottom_left.y()) - self.height() - TRIGGER_GAP
        if geometry is not None:
            x = min(max(x, geometry.left()), geometry.right() - width + 1)
            y = min(max(y, geometry.top()), max(geometry.top(), geometry.bottom() - self.height() + 1))
        return (x, y)

    # ------------------------------------------------------------------
    # Events / theme
    # ------------------------------------------------------------------

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self.close_popup()
                return True
            if watched is self._tree and self._on_tree_key(event):
                return True
            if watched is self._search and self._on_search_key(event):
                return True
        elif event.type() == QEvent.Type.ShortcutOverride:
            # Claim handled keys before host shortcuts can intercept them.
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Up, Qt.Key.Key_Down) and (
                watched is self._search or watched is self._tree
            ):
                event.accept()
                return True
        elif event.type() == QEvent.Type.WindowDeactivate:
            self.close_popup()
        return super().eventFilter(watched, event)

    def refresh_theme(self) -> None:
        """Re-apply token-driven chrome (theme switch; never a rebuild)."""
        from .styles import maybe_host_stylesheet
        from .theme.widgets_common import get_model_picker_popup_style

        self.setStyleSheet(maybe_host_stylesheet(get_model_picker_popup_style()))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    return (text or "").strip().lower()


def _screen_of(widget: QWidget):
    """Screen the popup is on, so the clamp matches the user's monitor."""
    app = QApplication.instance()
    if app is None:
        return None
    handle = widget.window().windowHandle()
    if handle is None or not hasattr(app, "screenAt"):
        return app.primaryScreen()
    return app.screenAt(handle.screen().geometry().center()) or app.primaryScreen()


def _dock_width(anchor: QWidget) -> int:
    """Width of the dock the trigger lives in (0 when unknown)."""
    try:
        window = anchor.window()
        parent = window.parentWidget()
        if parent is None:
            return 0
        return int(parent.width())
    except Exception:  # pragma: no cover - stub path
        return 0


def _style_item(item: QTreeWidgetItem, height: int, bold: bool = False) -> None:
    """Uniform row height (G6: header 24 px, model row 28 px).

    Qt measures every row by its size hint, so without this a long model
    ID wraps to two lines and the list stops being a list.
    """
    item.setSizeHint(0, QSize(0, height))
    font = item.font(0)
    font.setPixelSize(12)
    font.setBold(bold)
    item.setFont(0, font)
