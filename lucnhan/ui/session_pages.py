"""Session pages: a stack of chat views driven by the session tab bar.

The conversation region used to be a ``QTabWidget`` whose tab bar was
re-parented into the session strip above it.  ``QTabWidget`` still laid
out a tab-bar row for the bar it owns, and that reserved row rendered as
a host-colored band between the strip and the first message (measured
natively at 28 px).  The bar never *looked* like it belonged to the
widget, so the reservation was pure wasted space and a wrong-colored gap.

This module is the thin replacement: a ``QStackedWidget`` of pages plus
exactly the slice of the ``QTabWidget`` API ``panel_core`` calls, with the
bar owned outside and driving the stack directly.  Nothing reserves a bar
row, and there is exactly one close path.

The bar is also what forwards selection and closing, so the panel talks to
one object (``SessionPages``) instead of a widget plus a detached bar.
Not a general re-implementation: an unsupported attribute is a loud
``AttributeError`` from ``QStackedWidget`` rather than a silent no-op.
"""

from __future__ import annotations

from typing import Any

from .qt_compat import QStackedWidget


class SessionPages(QStackedWidget):
    """Chat pages driven by the session strip's tab bar.

    The bar is created by the panel (``_AddButtonTabBar``) and installed
    into the strip; this class only mirrors the tab labels onto it, so the
    strip's bar is the same object the panel has always styled and
    clicked.  The stack never lays a bar out, so no tab-bar row is
    reserved and nothing paints over the transcript.
    """

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.setObjectName("session_pages")
        self._bar: Any = None
        # Guards the bar <-> stack handshake against bouncing back.
        self._syncing = False
        # A tab widget frames its page with the bar's height; a stack must
        # not inherit any padding from that convention, so the page fills
        # this widget exactly (measured: the old row left 28 px of
        # page padding above the first message).
        self.setContentsMargins(0, 0, 0, 0)
        layout = self.layout()
        if layout is not None:
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)

    # --- tab-bar access -------------------------------------------------

    def setTabBar(self, bar: Any) -> None:
        """Adopt the strip's tab bar and let it drive the stack."""
        self._bar = bar
        self._bar.currentChanged.connect(self._on_bar_current_changed)

    def tabBar(self) -> Any:
        """The adopted tab bar (the one the strip hosts)."""
        return self._bar

    def _on_bar_current_changed(self, index: int) -> None:
        if self._syncing or not (0 <= index < self.count()):
            return
        self._syncing = True
        try:
            super().setCurrentIndex(index)
        finally:
            self._syncing = False

    # --- the QTabWidget slice the panel uses ----------------------------

    def setCurrentIndex(self, index: int) -> None:
        """Select a tab, moving the strip's highlight with it.

        A programmatic selection (opening a tab, focusing an older one,
        rebuilding a restored session) must move the bar too, or the strip
        keeps highlighting the previous tab while a different page is
        shown.  ``QTabBar.addTab`` does not move the selection when a tab
        is appended after the current one, so this cannot be left implicit.

        The bar emits ``currentChanged``, which switches the stack through
        ``_on_bar_current_changed``; ``_syncing`` keeps that from bouncing
        back here, and the stack's own ``currentChanged`` still fires
        exactly once with the selected index.
        """
        if not (0 <= index < self.count()):
            return
        if self._bar is not None and self._bar.currentIndex() != index:
            self._bar.setCurrentIndex(index)  # drives the stack via the signal
        if super().currentIndex() != index:
            self._syncing = True
            try:
                super().setCurrentIndex(index)
            finally:
                self._syncing = False

    def currentIndex(self) -> int:
        """The selected index, read from the bar so the two never differ."""
        if self._bar is not None:
            return self._bar.currentIndex()
        return super().currentIndex()

    def addTab(self, widget: Any, label: str) -> int:
        # No forced selection, matching QTabWidget: adding a tab never
        # steals the current one (the bar selects the first tab it is
        # given on its own).  Callers that mean to show the new tab call
        # ``setCurrentIndex`` explicitly, once the tab is prepared.
        index = self.addWidget(widget)
        self._bar.addTab(label)
        return index

    def insertTab(self, index: int, widget: Any, label: str) -> int:
        # Clamp like Qt, then keep the bar in step with the stack.
        index = max(0, min(index, self.count()))
        real = self.insertWidget(index, widget)
        self._bar.insertTab(real, label)
        return real

    def removeTab(self, index: int) -> Any:
        # Remove the page first so the stack's current index is already
        # clamped when the bar retargets the selection to a neighbour
        # (QTabWidget keeps the strip in step on close).
        widget = self.widget(index)
        if widget is not None:
            self.removeWidget(widget)
        self._bar.removeTab(index)
        self.setCurrentIndex(self._bar.currentIndex())
        return widget

    def indexOf(self, widget: Any) -> int:
        for index in range(self.count()):
            if super().widget(index) is widget:
                return index
        return -1

    def widget(self, index: int) -> Any:
        if 0 <= index < self.count():
            return super().widget(index)
        return None

    def setTabText(self, index: int, text: str) -> bool:
        if 0 <= index < self.count():
            self._bar.setTabText(index, text)
            return True
        return False

    def tabText(self, index: int) -> str:
        return self._bar.tabText(index)

    def currentWidget(self) -> Any:
        return self.widget(self.currentIndex())
