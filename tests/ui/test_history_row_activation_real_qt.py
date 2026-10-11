"""Native Qt regressions for History row activation (review finding 8).

The stub tier (``tests/ui/test_history_panel.py``) proves the handlers
decide correctly; this file proves real Qt actually *reaches* them.
Before the fix the native probe recorded::

    {"focus_policy": 0, "opened_keyboard": [],
     "opened_after_right_click": ["synthetic-2"]}

— the row reported ``NoFocus`` and a right-click opened the chat.

No ``QtTest`` (IDA's bundled PySide6 has none): events are built from
``QKeyEvent`` / ``QMouseEvent`` and delivered with
``QApplication.sendEvent``, which is the same ``event()`` → virtual-handler
dispatch the windowing system uses.  Every case carries a positive
control (a left click / a Return press that must open) so a silently
non-delivering event cannot masquerade as "the guard works".

Run standalone — a sibling test that installs ``tests.qt_stubs``
replaces PySide6 and makes these skip::

    pytest tests/ui/test_history_row_activation_real_qt.py
"""

from __future__ import annotations

import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from lucnhan.state.history_types import SessionHistoryEntry
from lucnhan.ui.history_panel import HistoryPanel, HistoryRowWidget
from lucnhan.ui.qt_compat import QEvent, QKeyEvent, QMouseEvent, QPointF, Qt
from tests.qt_real import requires_real_qt


def _entry(session_id: str, title: str = "Analyze parser") -> SessionHistoryEntry:
    now = time.time()
    return SessionHistoryEntry(
        session_id=session_id,
        title=title,
        created_at=now,
        updated_at=now,
        provider="openai",
        model="gpt-4o",
        message_count=2,
    )


def _key(name: str):
    return getattr(Qt.Key, name)


def _button(name: str):
    return getattr(Qt.MouseButton, name)


def _settle(app, ms: int = 60) -> None:
    """Run a real event loop so focus/window activation actually lands.

    ``processEvents`` alone left earlier popup windows holding focus, so
    ``setFocus`` silently did nothing.  A short ``QEventLoop`` lets Qt
    finish activation before the assertion.
    """
    from lucnhan.ui.qt_compat import QEventLoop, QTimer

    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    app.processEvents()


@requires_real_qt
class TestHistoryRowActivationNative(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from lucnhan.ui.qt_compat import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _shown_row(self, session_id: str = "s1") -> HistoryRowWidget:
        panel = HistoryPanel()
        self.addCleanup(panel.shutdown)
        self.addCleanup(panel.deleteLater)
        panel.set_entries([_entry(session_id)])
        panel.resize(320, 480)
        panel.show()
        panel.activateWindow()
        panel.raise_()
        _settle(self.app)
        row = panel._row_widgets[0]
        row.setFocus()
        _settle(self.app)
        self.assertIs(
            self.app.focusWidget(),
            row,
            "row must hold keyboard focus for the key cases",
        )
        self._panel = panel
        self._row = row
        return row

    def _key_event(self, kind, key, text: str = "") -> object:
        return QKeyEvent(kind, key, Qt.KeyboardModifier.NoModifier, text)

    def _mouse_event(self, kind, button) -> object:
        return QMouseEvent(kind, QPointF(5.0, 5.0), button, button, Qt.KeyboardModifier.NoModifier)

    def _click(self, row: HistoryRowWidget, button) -> None:
        self.app.sendEvent(row, self._mouse_event(QEvent.Type.MouseButtonPress, button))
        self.app.sendEvent(row, self._mouse_event(QEvent.Type.MouseButtonRelease, button))
        _settle(self.app)

    def _press(self, widget, key, text: str = "") -> None:
        """Send a real key press plus the matching release.

        ``QApplication.sendEvent`` is the same dispatch the windowing
        system uses. The release is sent too because the host matches
        shortcuts on the press — and so an activated widget must not
        act twice.
        """
        self.assertIs(self.app.focusWidget(), widget, "key press needs a focused widget")
        self.app.sendEvent(widget, self._key_event(QEvent.Type.KeyPress, key, text))
        self.app.sendEvent(widget, self._key_event(QEvent.Type.KeyRelease, key, text))
        _settle(self.app)

    def test_row_reports_strong_focus_and_takes_keyboard_focus(self) -> None:
        row = self._shown_row()
        self.assertEqual(row.focusPolicy(), Qt.FocusPolicy.StrongFocus)
        self.assertTrue(row.hasFocus())

    def test_return_enter_and_space_each_open_the_row_once(self) -> None:
        cases = ((_key("Key_Return"), "\r"), (_key("Key_Enter"), ""), (_key("Key_Space"), " "))
        for key, text in cases:
            with self.subTest(key=key):
                row = self._shown_row()
                opened: list[str] = []
                row.session_open_requested.connect(opened.append)

                self._press(row, key, text)

                self.assertEqual(opened, ["s1"], f"{key!r} must open the row exactly once")

    def test_only_a_left_click_opens_the_row(self) -> None:
        row = self._shown_row()
        opened: list[str] = []
        row.session_open_requested.connect(opened.append)

        for button in (_button("RightButton"), _button("MiddleButton")):
            with self.subTest(button=button):
                self._click(row, button)
        self.assertEqual(opened, [], "right/middle select or paste, they never open a chat")

        self._click(row, _button("LeftButton"))
        self.assertEqual(opened, ["s1"], "positive control: a left click still opens")

    def test_delete_button_activation_never_opens_the_row(self) -> None:
        row = self._shown_row()
        opened: list[str] = []
        deleted: list[tuple[str, str]] = []
        row.session_open_requested.connect(opened.append)
        row.session_delete_requested.connect(lambda session_id, title: deleted.append((session_id, title)))

        # Space is what a plain QPushButton activates on — it is only a
        # QDialog's auto-default button that answers Return. Asserting
        # Return here would demand a behaviour Qt never had.
        row._delete_btn.setFocus()
        _settle(self.app)
        self._press(row._delete_btn, _key("Key_Space"), " ")

        self.assertEqual(deleted, [("s1", "Analyze parser")])
        self.assertEqual(opened, [], "delete must stay its own action")

        # Positive control on the same panel: the row itself still opens.
        row.setFocus()
        _settle(self.app)
        self._press(row, _key("Key_Return"), "\r")
        self.assertEqual(opened, ["s1"])

    def test_pending_operation_blocks_keyboard_open(self) -> None:
        row = self._shown_row()
        opened: list[str] = []
        row.session_open_requested.connect(opened.append)

        self._panel.set_operation_pending("s1")
        self._press(row, _key("Key_Return"), "\r")
        self._click(row, _button("LeftButton"))
        self.assertEqual(opened, [])

        self._panel.set_operation_pending(None)
        self._press(row, _key("Key_Return"), "\r")
        self.assertEqual(opened, ["s1"], "positive control: re-enabled rows open again")


if __name__ == "__main__":
    unittest.main()
