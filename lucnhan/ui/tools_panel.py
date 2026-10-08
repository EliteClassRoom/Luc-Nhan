"""Tools panel: container for Agents, A2A bridge, and Knowledge panel.

The Bulk Renamer tab has been removed from the visible Tools surface so
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
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from .styles import (
    get_placeholder_style,
    get_tools_panel_header_style,
    get_tools_panel_style,
)
from .theme.applicator import bind_theme, disconnect_theme


class ToolsPanel(QWidget):
    """Standalone tools window containing tabs: Agents, A2A, Knowledge."""

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

        # Tab widget
        self._tabs = QTabWidget()
        self._tabs.setObjectName("tools_tabs")
        # Lazy tab population: route ``currentChanged`` so we can
        # tell ``panel_core`` which tab became visible.
        self._tabs.currentChanged.connect(self._on_tab_changed)
        _early_log("tools_panel:tabs_created")

        # Per-tab initialization flags. Set True once a tab's heavy
        # widget has been created/replaced by panel_core. Keeps the
        # placeholder until the user actually selects the tab (or
        # panel_core explicitly requests it via ``show_tools_panel``).
        self._agents_initialized = False
        self._a2a_initialized = False
        self._knowledge_initialized = False

        # Placeholder tabs. Real widgets are injected by
        # ``set_*_widget`` methods called from panel_core.
        # ``_placeholder_labels`` tracks every QLabel we own so
        # ``_apply_styles`` can re-paint them on a theme change.
        self._placeholder_labels: list[QLabel] = []

        # Agents tab — always visible first.
        self._agents_placeholder = QLabel("Not loaded")
        self._agents_placeholder.setStyleSheet(get_placeholder_style())
        self._agents_placeholder.setWordWrap(True)
        self._tabs.addTab(self._agents_placeholder, "Agents")
        self._placeholder_labels.append(self._agents_placeholder)

        # A2A bridge placeholder — the real widget is created on
        # first A2A-tab selection so A2A discovery (PATH, config,
        # optional HTTP agent-card fetch) doesn't block startup.
        self._a2a_widget = QLabel("Click Refresh to discover external agents")
        self._a2a_widget.setStyleSheet(get_placeholder_style())
        self._a2a_widget.setWordWrap(True)
        self._tabs.addTab(self._a2a_widget, "A2A")
        # ``_a2a_widget`` may be replaced by ``set_a2a_widget`` —
        # track it for theme refresh separately from the static
        # placeholders so a real widget replacement is detected.
        # When replaced, ``set_a2a_widget`` updates this attribute
        # and ``_apply_styles`` reads the current value.

        # Knowledge tab — default visible tab so opening Tools lands on
        # a cheap, read-only view instead of triggering function
        # enumeration.  A placeholder keeps the tab order stable so
        # the index contract above stays valid.
        self._knowledge_widget = QLabel("Not loaded")
        self._knowledge_widget.setStyleSheet(get_placeholder_style())
        self._knowledge_widget.setWordWrap(True)
        self._tabs.addTab(self._knowledge_widget, "Knowledge")
        self._placeholder_labels.append(self._knowledge_widget)

        # Land on Knowledge by default.  ``addTab`` leaves the index
        # at 0 (Agents), so setCurrentIndex must run after the tabs
        # are populated.
        self._tabs.setCurrentIndex(self.TAB_KNOWLEDGE)

        main_layout.addWidget(self._tabs)

        # Subscribe to theme changes now that every visual element
        # is constructed.  ``bind_theme`` runs the callback
        # synchronously, so the initial paint reflects the live
        # palette even when the panel is built during a theme
        # transition.
        bind_theme(self, self._apply_styles)

        _early_log(f"tools_panel:init:done:elapsed_ms={int((time.monotonic() - _t0) * 1000)}")

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
        # Panel shell QSS (tab widget styling etc.).
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

        The hook is wired via ``_tabs.currentChanged``. The panel
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
        """Replace the widget at the given tab index.

        Returns the widget that was previously shown at ``index``
        (after detaching it from the tab widget) so callers can keep
        a strong reference if needed. The returned widget has already
        been removed from the tab and had ``shutdown()`` called on it
        when applicable, but has NOT been deleted — the caller owns
        its lifetime after this call.
        """
        old = self._tabs.widget(index)
        current_index = self._tabs.currentIndex()
        signals_were_blocked = self._tabs.blockSignals(True)
        try:
            self._tabs.removeTab(index)
            self._tabs.insertTab(index, widget, label)
            self._tabs.setCurrentIndex(current_index)
        finally:
            self._tabs.blockSignals(signals_were_blocked)
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
        """Propagate shutdown to lazy tab widgets.

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
        for index in range(self._tabs.count()):
            widget = self._tabs.widget(index)
            if widget is None:
                continue
            if hasattr(widget, "shutdown"):
                try:
                    widget.shutdown()
                except Exception:  # defensive — teardown must not raise
                    pass
