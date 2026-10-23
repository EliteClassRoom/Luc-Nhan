"""Real-PySide6 geometry checks for the adaptive History / Mutations panes.

GUI contract G5.  The Qt stubs cannot model splitter / stack geometry, so
this file drives the real widgets offscreen and asserts what the user
actually sees:

  1. Wide (> 640): one auxiliary pane docks at the G2 nominal width.
  2. Narrow (<= 640): the SAME pane widget covers the content region and
     the composer / footer / header stay visible and clickable — the
     overlay is never a modal and never covers them.
  3. Only one pane is ever active.
  4. Resizing across the breakpoint keeps the open pane, its rows and the
     composer draft.

Run standalone (``pytest tests/ui/test_adaptive_panes.py``) or in a
session where ``tests.qt_stubs`` has not replaced PySide6.
"""

from __future__ import annotations

import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

_app = QApplication.instance() or QApplication(sys.argv)

from lucnhan.ui.history_panel import HistoryPanel
from lucnhan.ui.mutation_log_view import MutationLogPanel
from tests.qt_real import requires_real_qt

# G5 nominal wide-pane width from GUI contract G2.
_NOMINAL_PANE_WIDTH = 295


def _workspace():
    """Build a miniature of the workspace composition.

    Layout mirrors ``_build_ui``: [header | context] + content area +
    composer + footer, where the content area holds the Chat/Tools stack
    next to the pane stack — the same seam the production panel builds.
    """
    root = QWidget()
    root.setObjectName("root")
    layout = QVBoxLayout(root)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)

    header = QLabel("header")
    context = QLabel("context")
    layout.addWidget(header)
    layout.addWidget(context)

    content = QWidget()
    content_layout = QHBoxLayout(content)
    content_layout.setContentsMargins(0, 0, 0, 0)
    content_layout.setSpacing(0)

    chat = QLabel("conversation")
    mode_stack = QStackedWidget()
    mode_stack.addWidget(chat)

    history = HistoryPanel()
    mutation = MutationLogPanel()
    pane_stack = QStackedWidget()
    pane_stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    pane_stack.addWidget(mutation)
    pane_stack.addWidget(history)
    pane_stack.setVisible(False)

    content_layout.addWidget(mode_stack, 1)
    content_layout.addWidget(pane_stack, 0)
    layout.addWidget(content, 1)

    composer = QPlainTextEdit()
    footer = QLabel("footer")
    layout.addWidget(composer)
    layout.addWidget(footer)
    return root, content, mode_stack, pane_stack, history, mutation, composer


@requires_real_qt
class TestAdaptivePaneGeometry(unittest.TestCase):
    def setUp(self) -> None:
        self.root, self.content, self.mode_stack, self.stack, self.history, self.mutation, self.composer = _workspace()
        self.root.resize(1100, 700)
        self.root.show()
        for _ in range(3):
            _app.processEvents()

    def _pane_width(self) -> int:
        return self.stack.width()

    def test_wide_docks_pane_at_nominal_width(self) -> None:
        self.stack.setCurrentWidget(self.history)
        self.stack.setVisible(True)
        self._reconfigure(narrow=False)
        for _ in range(3):
            _app.processEvents()
        self.assertLessEqual(abs(self._pane_width() - _NOMINAL_PANE_WIDTH), 2, self._pane_width())
        self.assertGreater(self.content.width(), _NOMINAL_PANE_WIDTH)

    def test_narrow_pane_covers_content_region_only(self) -> None:
        self.stack.setCurrentWidget(self.history)
        self.stack.setVisible(True)
        # Resize first, then configure: the narrow pin is the content
        # region's *measured* width, so the region must already be at
        # its final size (this is exactly why production re-pins the
        # width from the live region on every resize).
        self.root.resize(460, 700)
        for _ in range(3):
            _app.processEvents()
        self._reconfigure(narrow=True)
        for _ in range(3):
            _app.processEvents()
        # Same widget, now filling the content region: the conversation
        # keeps only its minimum column, the pane takes the rest.
        self.assertEqual(self.stack.width() + self.mode_stack.width(), self.content.width())
        self.assertEqual(self.mode_stack.width(), self.mode_stack.minimumSizeHint().width())
        self.assertTrue(self.history.isVisible())
        # ...while header / context / composer / footer stay reachable.
        self.assertTrue(self.composer.isVisible())
        self.assertLess(self.composer.height(), 200)

    def test_shrink_inside_narrow_keeps_the_pane_inside_the_region(self) -> None:
        """Re-pin on every narrow resize, not just on the breakpoint crossing.

        A 640 -> 460 shrink that kept the old pin would let the pane
        overflow the content region and clip its own Close button.
        """
        self.stack.setCurrentWidget(self.history)
        self.stack.setVisible(True)
        self.root.resize(640, 700)
        for _ in range(3):
            _app.processEvents()
        self._reconfigure(narrow=True)
        for _ in range(3):
            _app.processEvents()
        self.root.resize(460, 700)
        for _ in range(3):
            _app.processEvents()
        self._reconfigure(narrow=True)
        for _ in range(3):
            _app.processEvents()
        # The invariant that matters is containment, not the exact
        # pixel width: whatever the pin recomputes to, the pane plus the
        # conversation's minimum column must exactly fill the region.
        self.assertEqual(self.stack.width() + self.mode_stack.width(), self.content.width())
        self.assertEqual(self.mode_stack.width(), self.mode_stack.minimumSizeHint().width())
        check_right = self.history._close_btn.mapTo(self.root, self.history._close_btn.rect().topRight()).x()
        self.assertLessEqual(check_right, self.content.width())
        self.assertTrue(self.history.isVisible())

    # --- Escape routing (GUI contract G5 must not cost the composer Esc)
    #
    # These dispatch REAL key events through Qt's shortcut system.  A
    # scope test alone cannot catch the regression this guards: a
    # WidgetWithChildrenShortcut parented to the panel root still
    # contains the composer, so Qt consumes Escape at the shortcut layer
    # and InputArea.keyPressEvent's cancel path never runs — the stubbed
    # "pane stayed open" assertion would still have passed.

    @staticmethod
    def _esc() -> object:
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent

        return QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_Escape,
            Qt.KeyboardModifier.NoModifier,
        )

    @staticmethod
    def _press_escape(target) -> None:
        """Dispatch a REAL Escape through Qt's shortcut + focus chain."""
        QApplication.sendEvent(target, TestAdaptivePaneGeometry._esc())
        _app.processEvents()

    def test_escape_in_composer_cancels_instead_of_closing_pane(self) -> None:
        """Composer focused + pane open: the editor must SEE Escape.

        Asserts the editor actually received the key (a real cancel
        callback fired), not merely that the pane stayed open.
        """
        from PySide6.QtGui import QKeySequence, QShortcut  # Qt6: QShortcut lives in QtGui

        self.stack.setCurrentWidget(self.history)
        self.stack.setVisible(True)
        self.history.set_entries([_entry("a", "Analyze Parser")])

        composer = self.composer  # QPlainTextEdit stand-in
        cancels: list[int] = []

        class _Editor(QPlainTextEdit):
            """Stands in for ``InputArea``: Esc -> cancel callback."""

            def keyPressEvent(self, event) -> None:  # Qt API name
                if event.key() == Qt.Key.Key_Escape:
                    cancels.append(1)
                    return
                super().keyPressEvent(event)

        editor = _Editor()
        self.root.layout().removeWidget(composer)
        self.root.layout().addWidget(editor)

        # Exactly the production wiring: shortcut parented to the pane
        # stack, widget-scoped.
        closes: list[int] = []
        sc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self.stack)
        sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc.activated.connect(lambda: closes.append(1))

        self.root.show()
        editor.setFocus()
        _app.processEvents()
        self.assertTrue(editor.hasFocus())

        self._press_escape(editor)
        self.assertEqual(cancels, [1], "composer must receive Esc and cancel")
        self.assertEqual(closes, [], "the pane must not consume the composer Esc")
        self.assertTrue(self.history.isVisible(), "pane must stay open")

    def test_escape_in_pane_closes_it(self) -> None:
        """Pane focused + pane open: Escape closes the pane."""
        from PySide6.QtGui import QKeySequence, QShortcut  # Qt6: QShortcut lives in QtGui

        self.stack.setCurrentWidget(self.history)
        self.stack.setVisible(True)
        closes: list[int] = []
        sc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self.stack)
        sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc.activated.connect(lambda: closes.append(1))

        self.root.show()
        target = self.history._search  # a real focusable child of the pane
        target.setFocus()
        _app.processEvents()
        self.assertTrue(target.hasFocus())

        self._press_escape(target)
        self.assertEqual(closes, [1], "pane must receive Esc when it owns focus")

    def test_pane_shortcut_scope_excludes_the_composer(self) -> None:
        """Structural guard: the scope must not contain the composer.

        This is the regression that made the shortcut swallow the
        composer's key: the composer is a sibling of the pane stack, so a
        shortcut parented to anything ABOVE the pane would include it in
        its WidgetWithChildren scope and Qt would consume Escape before
        the editor saw it.
        """
        import inspect

        from lucnhan.ui.panel_core import LucNhanPanelCore

        src = inspect.getsource(LucNhanPanelCore._install_shortcuts)
        self.assertIn("QShortcut(QKeySequence(Qt.Key.Key_Escape), self._pane_stack)", src)
        self.assertNotIn("QShortcut(QKeySequence(Qt.Key.Key_Escape), self)", src)

    def test_panes_are_mutually_exclusive(self) -> None:
        self.stack.setCurrentWidget(self.history)
        self.stack.setVisible(True)
        _app.processEvents()
        self.assertTrue(self.history.isVisibleTo(self.root))
        self.assertFalse(self.mutation.isVisibleTo(self.root))
        self.stack.setCurrentWidget(self.mutation)
        _app.processEvents()
        self.assertTrue(self.mutation.isVisibleTo(self.root))
        self.assertFalse(self.history.isVisibleTo(self.root))

    def test_resize_across_breakpoint_keeps_pane_and_draft(self) -> None:
        self.history.set_entries([_entry("a", "Analyze Parser")])
        self.history._search.setText("parse")
        self.composer.setPlainText("draft that must survive")
        self.stack.setCurrentWidget(self.history)
        self.stack.setVisible(True)
        self._reconfigure(narrow=True)
        self.root.resize(460, 700)
        for _ in range(3):
            _app.processEvents()
        self._reconfigure(narrow=False)
        self.root.resize(1100, 700)
        for _ in range(3):
            _app.processEvents()
        self.assertTrue(self.history.isVisibleTo(self.root))
        self.assertEqual(self.history.visible_session_ids(), ["a"])
        self.assertEqual(self.history._search.text(), "parse")
        self.assertEqual(self.composer.toPlainText(), "draft that must survive")

    def test_pane_is_not_a_top_level_window(self) -> None:
        """G5: the overlay lives inside the content area, never a modal."""
        self.stack.setCurrentWidget(self.history)
        self.stack.setVisible(True)
        _app.processEvents()
        self.assertFalse(self.history.isWindow())
        self.assertEqual(self.history.windowType(), Qt.WindowType.Widget)
        # The pane is parented into the content area, not a new window.
        self.assertIs(self.history.window(), self.root.window())

    def _reconfigure(self, narrow: bool) -> None:
        """Mirror ``_apply_pane_presentation`` plus the caller's split."""
        layout = self.content.layout()
        # Wide: the splitter owns the ratio (3:1 conversation : 1 pane,
        # the pane then pinned at its nominal width).
        # Narrow: the stack's stretch wins and the conversation falls
        # back to its minimum hint — the covered column.
        layout.setStretchFactor(self.mode_stack, 1)
        layout.setStretchFactor(self.stack, 1)
        chat_min = self.mode_stack.minimumSizeHint().width()
        self.stack.setMinimumWidth(
            max(self.content.width() - chat_min, _NOMINAL_PANE_WIDTH) if narrow else _NOMINAL_PANE_WIDTH
        )
        self.stack.setMaximumWidth(16777215 if narrow else _NOMINAL_PANE_WIDTH)
        self.history.set_overlay_presentation(narrow)
        layout.invalidate()
        _app.processEvents()


def _entry(session_id: str, title: str):
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _E:
        session_id: str
        title: str
        created_at: float = 0.0
        updated_at: float = 0.0
        provider: str = ""
        model: str = ""
        message_count: int = 0

    return _E(session_id=session_id, title=title)


if __name__ == "__main__":
    unittest.main()
