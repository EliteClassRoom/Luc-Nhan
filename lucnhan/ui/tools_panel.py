"""Tools panel: horizontal navigation over Agents, A2A, Knowledge.

The Bulk Renamer page has been removed from the visible Tools surface so
opening Tools does not eagerly enumerate every function in the binary.
The Renamer UI/backend modules remain in the repository for a future
re-enable, but no Tools entry point builds them.

Can be shown as an independent window (QDialog) or embedded in a layout.
"""

from __future__ import annotations

import time

from ..core.early_log import _early_log
from .qt_compat import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    Qt,
    QVBoxLayout,
    QWidget,
)
from .styles import (
    get_placeholder_style,
    get_tools_panel_header_style,
    get_tools_panel_style,
)
from .theme.applicator import bind_theme, disconnect_theme
from .theme.widgets_common import get_tools_subnav_style

#: GUI contract G8 — horizontal subnavigation order.
_TOOL_PAGES: tuple[str, ...] = ("Agents", "A2A", "Knowledge")


class ToolsPanel(QWidget):
    """Tools window with horizontal navigation: Agents, A2A, Knowledge."""

    # Public tab index contract.  Tests and external callers should
    # reference these constants instead of hard-coded integers so the
    # order can be reshuffled without code edits in callers.
    TAB_AGENTS = 0
    TAB_A2A = 1
    TAB_KNOWLEDGE = 2
    TAB_COUNT = 3

    def __init__(self, parent: QWidget | None = None):
        _early_log("tools_panel:init:entry")
        _t0 = time.monotonic()
        super().__init__(parent)
        self.setObjectName("tools_panel")
        self.setWindowTitle("Luc Nhan Tools")
        self.setStyleSheet(get_tools_panel_style())
        # No minimum size — this widget is embedded in IDA dockable forms
        # and IDA sidebars, which can be any size.

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Header bar with title (hidden when docked in IDA)
        self._header = QFrame()
        self._header.setObjectName("tools_panel_header")
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(12, 8, 12, 8)

        # ``_title`` is stored on self so ``_apply_styles`` can
        # repaint it on a theme change.  Previously the title was a
        # local variable only, so a theme switch after construction
        # left the header label on its construction-time palette.
        self._title = QLabel("Tools")
        self._title.setStyleSheet(get_tools_panel_header_style())
        header_layout.addWidget(self._title)
        header_layout.addStretch()

        main_layout.addWidget(self._header)

        # Horizontal subnavigation (GUI contract G8).  Checkable
        # buttons in a scroll area rather than a QTabBar so the active
        # page keeps the ``selection`` fill + accent text and the strip
        # scrolls instead of overflowing a narrow dock.
        self._subnav = self._build_subnav()
        main_layout.addWidget(self._subnav)

        # Page host.  Same lazy contract as the old QTabWidget: one
        # placeholder per page, replaced by ``set_*_widget`` when
        # panel_core lazily initialises that page.
        self._page_host = QStackedWidget()
        self._page_host.setObjectName("tools_pages")
        self._page_host.currentChanged.connect(self._on_tab_changed)
        _early_log("tools_panel:pages_created")

        # Per-page initialization flags. Set True once a page's heavy
        # widget has been created/replaced by panel_core. Keeps the
        # placeholder until the user actually selects the page (or
        # panel_core explicitly requests it via ``show_tools_panel``).
        self._agents_initialized = False
        self._a2a_initialized = False
        self._knowledge_initialized = False

        # Placeholder pages. Real widgets are injected by
        # ``set_*_widget`` methods called from panel_core.
        # ``_placeholder_labels`` tracks every QLabel we own so
        # ``_apply_styles`` can re-paint them on a theme change.
        self._placeholder_labels: list[QLabel] = []

        # Agents page — always visible first.
        self._agents_placeholder = QLabel("Not loaded")
        self._agents_placeholder.setStyleSheet(get_placeholder_style())
        self._agents_placeholder.setWordWrap(True)
        self._page_host.insertWidget(self.TAB_AGENTS, self._agents_placeholder)
        self._placeholder_labels.append(self._agents_placeholder)

        # A2A bridge placeholder — the real widget is created on
        # first A2A-page selection so A2A discovery (PATH, config,
        # optional HTTP agent-card fetch) doesn't block startup.
        self._a2a_widget = QLabel("Click Refresh to discover external agents")
        self._a2a_widget.setStyleSheet(get_placeholder_style())
        self._a2a_widget.setWordWrap(True)
        self._page_host.insertWidget(self.TAB_A2A, self._a2a_widget)
        # ``_a2a_widget`` may be replaced by ``set_a2a_widget`` —
        # track it for theme refresh separately from the static
        # placeholders so a real widget replacement is detected.
        # When replaced, ``set_a2a_widget`` updates this attribute
        # and ``_apply_styles`` reads the current value.

        # Knowledge page — default visible page so opening Tools lands on
        # a cheap, read-only view instead of triggering function
        # enumeration.  A placeholder keeps the page order stable so the
        # index contract above stays valid.
        self._knowledge_widget = QLabel("Not loaded")
        self._knowledge_widget.setStyleSheet(get_placeholder_style())
        self._knowledge_widget.setWordWrap(True)
        self._page_host.insertWidget(self.TAB_KNOWLEDGE, self._knowledge_widget)
        self._placeholder_labels.append(self._knowledge_widget)

        # Land on Knowledge by default.  ``insertWidget`` leaves the
        # index at 0 (Agents), so setCurrentIndex must run after the
        # pages are populated.
        self._select_page(self.TAB_KNOWLEDGE)

        main_layout.addWidget(self._page_host, 1)

        # Subscribe to theme changes now that every visual element
        # is constructed.  ``bind_theme`` runs the callback
        # synchronously, so the initial paint reflects the live
        # palette even when the panel is built during a theme
        # transition.
        bind_theme(self, self._apply_styles)

        _early_log(f"tools_panel:init:done:elapsed_ms={int((time.monotonic() - _t0) * 1000)}")

    def _build_subnav(self) -> QWidget:
        """Horizontal page strip (G8): padding 5/10, gap 3, min-height 29."""
        nav = QWidget()
        nav.setObjectName("tools_subnav")
        nav.setStyleSheet(get_tools_subnav_style())
        outer = QHBoxLayout(nav)
        outer.setContentsMargins(12, 5, 12, 5)
        outer.setSpacing(3)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(3)
        self._nav_buttons: list[QPushButton] = []
        for index, title in enumerate(_TOOL_PAGES):
            btn = QPushButton(title)
            btn.setObjectName("tools_subnav_btn")
            btn.setCheckable(True)
            btn.setChecked(index == 0)
            btn.setMinimumHeight(29)
            btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            btn.setAccessibleName(f"Tools page: {title}")
            btn.setToolTip(f"{title} tools")
            btn.clicked.connect(lambda _checked=False, page=index: self._select_page(page))
            row_layout.addWidget(btn)
            self._nav_buttons.append(btn)
        row_layout.addStretch(1)
        scroll.setWidget(row)
        outer.addWidget(scroll)
        return nav

    def _select_page(self, index: int) -> None:
        """Activate page ``index`` and keep the subnav selection in sync."""
        host = getattr(self, "_page_host", None)
        if host is None or not (0 <= index < len(_TOOL_PAGES)):
            return
        for position, btn in enumerate(getattr(self, "_nav_buttons", [])):
            btn.setChecked(position == index)
        if host.currentIndex() != index:
            host.setCurrentIndex(index)
        else:
            self._on_tab_changed(index)
        btn = getattr(self, "_nav_buttons", [])
        if index < len(btn):
            parent = btn[index].parentWidget()
            if parent is not None:
                parent.ensureWidgetVisible(btn[index], 0, 0)

    @property
    def _tabs(self) -> QStackedWidget:
        """Compatibility shim for the pre-G8 ``QTabWidget`` attribute.

        Callers (and tests) that still read ``_tabs.currentIndex()`` /
        ``setCurrentIndex()`` get the page host.  No tab bar is
        rendered — navigation is the horizontal subnav.
        """
        return self._page_host

    def set_current_page(self, index: int) -> None:
        """Public navigation entry point used by ``panel_core``."""
        self._select_page(index)

    def current_page(self) -> int:
        """Index of the page currently shown."""
        host = getattr(self, "_page_host", None)
        return host.currentIndex() if host is not None else self.TAB_AGENTS

    def _apply_styles(self, _tokens: object = None) -> None:
        """Re-apply panel QSS, header title, and placeholders on theme change.

        Walks both the static placeholder list (``_placeholder_labels``)
        and the current A2A/Knowledge widget references so a tab that
        was replaced by ``set_*_widget`` after the placeholder list was
        built still gets a fresh stylesheet on the next theme switch.

        In host-theme mode the placeholder QSS would normally be
        cleared by ``maybe_host_stylesheet``; the panel-level QSS
        already does this for the tools shell itself, so the
        placeholders follow the same pattern.
        """
        # Panel shell QSS.
        self.setStyleSheet(get_tools_panel_style())
        # Header title (was a local var before, now ``self._title``).
        if getattr(self, "_title", None) is not None:
            self._title.setStyleSheet(get_tools_panel_header_style())
        # Static placeholders.
        for lbl in getattr(self, "_placeholder_labels", []):
            lbl.setStyleSheet(get_placeholder_style())
        # A2A widget — may still be a placeholder QLabel or may have
        # been replaced by ``set_a2a_widget``.  ``QLabel`` placeholders
        # carry the placeholder style; real A2A widgets manage their
        # own theme subscription, so we only re-paint placeholders.
        a2a = getattr(self, "_a2a_widget", None)
        if isinstance(a2a, QLabel):
            a2a.setStyleSheet(get_placeholder_style())
        # Same for the Knowledge slot.
        kw = getattr(self, "_knowledge_widget", None)
        if isinstance(kw, QLabel):
            kw.setStyleSheet(get_placeholder_style())

    def _on_tab_changed(self, index: int) -> None:
        """Forward tab selection to panel_core so it can lazy-init that tab.

        The hook is wired via ``_page_host.currentChanged``. The panel
        core installs an ``on_tab_activated`` callback via
        :meth:`set_tab_activation_callback` after the panel is
        embedded or docked; until then the signal is a no-op.
        """
        callback = getattr(self, "_on_tab_activated_cb", None)
        if callback is None:
            return
        try:
            callback(index)
        except Exception as e:  # defensive — never crash Qt
            from ..core.logging import log_debug

            log_debug(f"tools_panel tab activation callback failed: {e}")

    def set_tab_activation_callback(self, callback) -> None:
        """Register a callback invoked whenever a tab becomes active.

        ``panel_core`` uses this to drive per-tab lazy initialization.
        Storing the callback as a plain attribute avoids a Qt signal
        round-trip and lets the callback know whether the panel is
        embedded or docked.
        """
        self._on_tab_activated_cb = callback

    def _replace_tab(self, index: int, widget: QWidget, label: str) -> QWidget | None:
        """Replace the widget at the given page index.

        Returns the widget that was previously shown at ``index``
        (after detaching it from the page host) so callers can keep
        a strong reference if needed. The returned widget has already
        been removed and had ``shutdown()`` called on it
        when applicable, but has NOT been deleted — the caller owns
        its lifetime after this call.

        ``label`` is unused by the subnav (the strip owns the page
        names) but is kept in the signature so existing callers keep
        working.
        """
        host = self._page_host
        old = host.widget(index)
        current_index = host.currentIndex()
        signals_were_blocked = host.blockSignals(True)
        try:
            host.removeWidget(old)
            host.insertWidget(index, widget)
            host.setCurrentIndex(current_index)
        finally:
            host.blockSignals(signals_were_blocked)
        if old is not None and old is not widget:
            # Shut down the old widget first so any background threads
            # / timers tied to it stop while the replacement is still
            # alive. ``hasattr`` keeps the helper robust against plain
            # placeholder QLabels that do not implement shutdown().
            if hasattr(old, "shutdown"):
                try:
                    old.shutdown()
                except Exception:  # defensive — teardown must not raise
                    pass
            old.deleteLater()
        return old

    def set_agents_widget(self, widget: QWidget) -> None:
        """Replace the Agents tab content."""
        self._replace_tab(self.TAB_AGENTS, widget, "Agents")
        self._agents_initialized = True

    def set_a2a_widget(self, widget: QWidget) -> None:
        """Replace the A2A tab content (replaces our default bridge).

        Also updates ``self._a2a_widget`` so the panel always knows
        which widget is active in the A2A slot. ``shutdown()`` only
        needs this when the caller later destroys the panel.
        """
        self._replace_tab(self.TAB_A2A, widget, "A2A")
        self._a2a_widget = widget
        self._a2a_initialized = True

    def set_knowledge_widget(self, widget: QWidget) -> None:
        """Replace the Knowledge tab content (default tab).

        Knowledge lives at :data:`TAB_KNOWLEDGE` so external callers
        and panel_core agree on a stable index.
        """
        self._replace_tab(self.TAB_KNOWLEDGE, widget, "Knowledge")
        self._knowledge_widget = widget
        self._knowledge_initialized = True

    def hide_header(self) -> None:
        """Hide the title bar (used when embedded in a dockable form)."""
        self._header.setVisible(False)

    def shutdown(self) -> None:
        """Propagate shutdown to lazily-built page widgets.

        Walks every tab and shuts down any widget that exposes a
        ``shutdown()`` method (the heavy Real widgets — not the
        lightweight ``QLabel`` placeholders). Failures are swallowed
        so a misbehaving tab cannot block panel teardown.

        Also detaches our own theme subscription so a late theme
        emit during teardown does not dereference a partially
        destroyed panel.  Tab widgets are responsible for their own
        ``shutdown`` paths.
        """
        disconnect_theme(self)
        host = getattr(self, "_page_host", None)
        for index in range(host.count()):
            widget = host.widget(index)
            if widget is None:
                continue
            if hasattr(widget, "shutdown"):
                try:
                    widget.shutdown()
                except Exception:  # defensive — teardown must not raise
                    pass
