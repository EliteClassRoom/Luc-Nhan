"""ModelPicker search-box keyboard navigation (review finding 9).

The native probe recorded, with focus in the search box::

    before_down "alpha" / after_down "alpha"
    commits_enter_in_search [] / commits_enter_in_tree []

``QLineEdit`` consumes Down/Return, so the list never moved and Enter
committed nothing; calling the tree handler directly *did* commit, which
proved the commit path was fine and only the key routing was missing.

Real Qt only — the popup is a ``Qt.Popup`` and every case below needs
focus, key delivery and the live tree, none of which ``tests.qt_stubs``
can model.  Events are ``QKeyEvent`` + ``QApplication.sendEvent``
(IDA's bundled PySide6 ships no ``QtTest``).  The catalog is handed in
through ``set_catalog`` — no provider fetch is possible from here.

Run standalone (skips when a sibling installed ``tests.qt_stubs``)::

    pytest tests/ui/test_model_picker_search_keys.py
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from lucnhan.core.types import ModelInfo
from lucnhan.ui.model_picker import (
    _ROLE_IDENTITY,
    BUSY_HINT,
    NO_MATCHES_TEXT,
    ModelChoice,
    ModelPicker,
)
from lucnhan.ui.qt_compat import QEvent, QKeyEvent, QKeySequence, QShortcut, Qt
from tests.qt_real import requires_real_qt

# Two connections, duplicate model id under a filtered query: the
# selection must carry the (connection, model) pair, never a display text.
_CATALOG = {
    "alpha-conn": [ModelInfo("alpha-one", "Alpha One", "alpha"), ModelInfo("shared", "Shared", "alpha")],
    "beta-conn": [ModelInfo("beta-one", "Beta One", "beta"), ModelInfo("shared", "Shared", "beta")],
}


def _key(name: str):
    return getattr(Qt.Key, name)


@requires_real_qt
class TestPickerSearchKeyboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from lucnhan.ui.qt_compat import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        from lucnhan.ui.qt_compat import QLineEdit, QVBoxLayout, QWidget

        self.chosen: list[ModelChoice] = []
        self.host = QWidget()
        layout = QVBoxLayout(self.host)
        layout.setContentsMargins(0, 0, 0, 0)
        self.trigger = QLineEdit()
        layout.addWidget(self.trigger)
        self.host.resize(900, 600)
        self.host.move(0, 0)
        self.host.show()
        self.host.activateWindow()
        self.host.raise_()
        self._settle()

        self.picker = ModelPicker(parent=self.host, on_select=self.chosen.append)
        self.picker.set_catalog(_CATALOG)
        self.picker.set_selection(ModelChoice("alpha-conn", "alpha-one", "high"))
        self.picker.open_above(self.trigger)
        self._settle()
        self.picker._search.setFocus()
        self._settle()
        self.addCleanup(self.picker.close_popup)
        self.addCleanup(self.picker.deleteLater)
        self.addCleanup(self.host.deleteLater)
        self.assertIs(
            self.app.focusWidget(),
            self.picker._search,
            "the popup must own keyboard focus once opened",
        )

    def _settle(self, ms: int = 60) -> None:
        """Run a real event loop so popup activation and focus land.

        ``processEvents`` alone let a previously opened popup keep focus,
        so ``setFocus`` on the search box silently did nothing.
        """
        from lucnhan.ui.qt_compat import QEventLoop, QTimer

        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()
        self.app.processEvents()

    def _type(self, text: str) -> None:
        self.picker._search.setText(text)
        self._settle()

    def _press(self, name: str, text: str = "") -> None:
        assert self.app.focusWidget() is self.picker._search, "search keys need the search box focused"
        self._send(self.picker._search, name, text)

    def _press_tree(self, name: str, text: str = "") -> None:
        assert self.app.focusWidget() is self.picker._tree, "tree keys need the list focused"
        self._send(self.picker._tree, name, text)

    def _send(self, target, name: str, text: str = "") -> None:
        """Send a real key press + release through the platform dispatch."""
        for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            self.app.sendEvent(
                target,
                QKeyEvent(kind, _key(name), Qt.KeyboardModifier.NoModifier, text),
            )
        self._settle()

    def _highlighted(self) -> tuple[str, str]:
        """Identity of the item Qt highlights, read off the item itself.

        ``current_choice()`` answers from the picker's pending identity,
        which a highlight can precede — the highlighted item is what the
        user sees and what Enter must commit.
        """
        item = self.picker._tree.currentItem()
        self.assertIsNotNone(item, "the list must highlight a row")
        identity = item.data(0, _ROLE_IDENTITY)
        self.assertIsNotNone(identity, "a connection header is not a selectable model")
        self.assertFalse(item.isHidden(), "navigation must not stop on a hidden row")
        return identity

    # -- Down / Up ------------------------------------------------------

    def test_down_selects_the_next_visible_row(self) -> None:
        self._type("shared")
        # The query itself already highlights the first visible row, so
        # Down must move from there to the second one.
        self.assertEqual(self._highlighted(), ("alpha-conn", "shared"))
        self._press("Key_Down")

        self.assertEqual(self._highlighted(), ("beta-conn", "shared"))
        self.assertEqual(self.picker._tree.currentItem(), self.picker.item_for("beta-conn", "shared"))

    def test_up_from_the_first_row_clamps_and_still_commits_that_row(self) -> None:
        """No wrap-around to a hidden row: Up on row one commits row one."""
        self._type("shared")
        self._press("Key_Down")
        self._press("Key_Up")
        self._press("Key_Up")
        self.assertEqual(self._highlighted(), ("alpha-conn", "shared"))

        self._press("Key_Return", "\r")
        self.assertEqual(self.chosen, [ModelChoice("alpha-conn", "shared", "high")])

    def test_navigation_walks_the_visible_rows_in_order_and_clamps(self) -> None:
        """Down visits every visible MODEL row once and stops at the last."""
        self._type("shared")
        expected = [(row.connection_name, row.model_id) for row in self.picker.visible_rows()]
        # The query already highlights the first visible row; every press
        # after that advances one row and then clamps.
        walked = [self._highlighted()]
        for _ in range(len(expected) + 2):
            self._press("Key_Down")
            walked.append(self._highlighted())

        self.assertEqual(walked, expected + [expected[-1]] * 3)

    # -- Enter ----------------------------------------------------------

    def test_enter_in_search_commits_the_visible_row_once(self) -> None:
        self._type("shared")
        self._press("Key_Down")

        self._press("Key_Return", "\r")

        self.assertEqual(self.chosen, [ModelChoice("beta-conn", "shared", "high")])

    def test_search_navigation_owns_matching_application_shortcuts(self) -> None:
        triggered: list[str] = []
        for key in ("Down", "Up"):
            shortcut = QShortcut(QKeySequence(key), self.host)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(lambda name=key: triggered.append(name))
            shortcut.activatedAmbiguously.connect(lambda name=key: triggered.append(name))
            self.addCleanup(shortcut.deleteLater)
        self._type("shared")
        self._press("Key_Down")
        self.assertEqual(self._highlighted(), ("beta-conn", "shared"))
        self._press("Key_Up")
        self.assertEqual(self._highlighted(), ("alpha-conn", "shared"))
        self._press("Key_Return", "\r")
        self.assertEqual(self.chosen, [ModelChoice("alpha-conn", "shared", "high")])
        self.assertEqual(triggered, [])

    def test_enter_with_a_single_match_commits_it_without_arrows(self) -> None:
        self._type("beta-one")

        self._press("Key_Return", "\r")

        self.assertEqual(self.chosen, [ModelChoice("beta-conn", "beta-one", "high")])

    def test_duplicate_model_ids_keep_distinct_connections(self) -> None:
        """Same model id under two connections = two commits, not one row."""
        self._type("shared")
        self._press("Key_Return", "\r")
        self._press("Key_Down")
        self._press("Key_Return", "\r")

        self.assertEqual(
            self.chosen,
            [ModelChoice("alpha-conn", "shared", "high"), ModelChoice("beta-conn", "shared", "high")],
        )

    # -- no results / busy ----------------------------------------------

    def test_no_visible_row_commits_nothing(self) -> None:
        self._type("shared")
        self._press("Key_Return", "\r")
        self.assertEqual(len(self.chosen), 1)

        self._type("nothing-matches-this")
        self.assertEqual(self.picker.visible_rows(), [])
        self._press("Key_Down")
        self._press("Key_Return", "\r")

        self.assertEqual(len(self.chosen), 1, "a query with no row must commit nothing")
        self.assertEqual(self.picker.status_text(), NO_MATCHES_TEXT)

    def test_busy_blocks_search_keyboard_commit(self) -> None:
        self._type("beta-one")
        self.picker.set_busy(True)
        self._settle()

        self._press("Key_Return", "\r")

        self.assertEqual(self.chosen, [], "a running agent owns the model")
        self.assertEqual(self.picker.status_text(), BUSY_HINT)

    # -- preserved behaviour --------------------------------------------

    def test_search_box_tab_chain_reaches_the_tree_then_the_thinking_combo(self) -> None:
        """Focus can still leave the search box for list and thinking."""
        self.assertTrue(self.picker._search.hasFocus())

        self.picker._tree.setFocus()
        self._settle()
        self.assertIs(self.app.focusWidget(), self.picker._tree)

        self.picker._thinking.setFocus()
        self._settle()
        self.assertIs(self.app.focusWidget(), self.picker._thinking)

    def test_tree_keys_still_drive_selection_and_commit(self) -> None:
        """Native tree navigation is untouched: arrows move, Return commits."""
        self._type("shared")
        self.picker._tree.setFocus()
        self._settle()
        self.assertIs(self.app.focusWidget(), self.picker._tree)
        before = self._highlighted()

        self._press_tree("Key_Down")
        moved = self._highlighted()
        self.assertNotEqual(moved, before)

        self._press_tree("Key_Return", "\r")
        self.assertEqual(self.chosen, [ModelChoice(moved[0], moved[1], "high")])

    def test_escape_still_closes_and_returns_focus_to_the_trigger(self) -> None:
        self._press("Key_Escape")

        self.assertFalse(self.picker.isVisible())
        self.assertTrue(self.trigger.hasFocus())


if __name__ == "__main__":
    unittest.main()
