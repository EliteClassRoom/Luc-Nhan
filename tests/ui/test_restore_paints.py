"""Regression tests for the async chat-history restore.

Bug 2 (the critical defect):

  ``ChatView.restore_from_messages_async`` builds widgets on a
  ``RestoreWorker`` QThread and pushes ``(kind, payload)`` tuples onto
  ``worker.queue``.  A 50 ms main-thread ``QTimer`` (drain loop) was the
  sole consumer.  When the worker finishes in under one timer tick
  (typical for small/short sessions), ``QThread.finished`` is delivered
  to the main thread FIRST.  ``_on_worker_finished`` then stops the
  drain timer and ``deleteLater``s every remaining ``MessagePlaceholder``
  — so the queued chunks are never drained and zero widgets are built.

These tests cover aspects the Bug 2 fix tests added to
``tests/ui/test_chat_view_restore.py`` do not:

  1. The MessagePlaceholder tracking dict + layout must end up empty
     after the restore (catches a chunk the drain missed).
  2. Clicking the "Load older" button must grow the cap and re-run
     the async restore (catches a regression where the cap path
     regresses).

A standalone regression suite keeps the assertion surface honest —
``test_chat_view_restore.py`` is owned by the RestoreRace slice and
covers the same restore path; if a future regression slips past one
file, the other still catches it.
"""

from __future__ import annotations

import os
import unittest

# Headless test environments (CI without a display) need an
# offscreen Qt platform. Set it before any Qt import so the very
# first ``QApplication`` instance picks it up.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Re-import safety: drop any ``types.ModuleType`` stubs a sibling test
# may have left behind so we import the real chat_view / message_widgets.
import sys as _sys

_STUB_TARGETS = (
    "rikugan.core.types",
    "rikugan.agent.turn",
    "rikugan.ui.chat_view",
    "rikugan.ui.styles",
    "rikugan.ui.theme",
    "rikugan.ui.theme.manager",
    "rikugan.ui.theme.tokens",
    "rikugan.ui.theme.palette_dark",
    "rikugan.ui.theme.palette_light",
    "rikugan.ui.theme.palette_ida",
    "rikugan.ui.markdown",
    "rikugan.ui.message_widgets",
    "rikugan.ui.plan_view",
    "rikugan.ui.tool_widgets",
    "rikugan.ui.qt_compat",
    "rikugan.ui.input_area",
    "rikugan.ui.context_bar",
)
for _name in list(_sys.modules):
    if _name in _STUB_TARGETS:
        _sys.modules.pop(_name, None)

# Repair real ``QFont`` if a sibling test clobbered it. The root
# ``tests/conftest.py`` backs up the real QFont on
# ``PySide6._real_qfont_backup``; re-install it so subsequent code
# that uses ``QFont`` (e.g. inside the chat view) doesn't trip on a
# stub class.
try:
    import PySide6  # type: ignore[import-not-found]

    _real_qfont = getattr(PySide6, "_real_qfont_backup", None)
    if _real_qfont is not None:
        import PySide6.QtGui  # type: ignore[import-not-found]

        PySide6.QtGui.QFont = _real_qfont
except ImportError:
    pass

from rikugan.core.types import Message, Role
from rikugan.ui.chat_view import (
    _RESTORE_DEFAULT_MAX_RENDERED,
    ChatView,
    MessagePlaceholder,
)
from rikugan.ui.message_widgets import (
    AssistantMessageWidget,
    UserMessageWidget,
)
from tests.qt_real import live_class, requires_real_qt


def _user_widget_cls() -> type:
    """Live ``UserMessageWidget`` class.

    Resolved per call: another test file may purge ``rikugan.ui.*`` from
    ``sys.modules`` at import time, which re-imports the module and
    yields a class object that is NOT the one the live widget tree
    used. ``findChildren`` matches by exact type, so a stale reference
    reports zero widgets on a perfectly painted chat.
    """
    return live_class("rikugan.ui.message_widgets.UserMessageWidget")


def _assistant_widget_cls() -> type:
    """Live ``AssistantMessageWidget`` class — see :func:`_user_widget_cls`."""
    return live_class("rikugan.ui.message_widgets.AssistantMessageWidget")


def _chat_view_cls() -> type:
    """Live ``ChatView`` class — see :func:`_user_widget_cls`."""
    return live_class("rikugan.ui.chat_view.ChatView")


def _user_message(content: str, msg_id: str = "") -> Message:
    msg = Message(role=Role.USER, content=content)
    msg.id = msg_id
    return msg


def _assistant_message(content: str, msg_id: str = "") -> Message:
    msg = Message(role=Role.ASSISTANT, content=content)
    msg.id = msg_id
    return msg


class _AsyncRestoreHarness:
    """Run ``restore_from_messages_async`` against a real ChatView and
    pump the event loop until either widgets land or a deadline passes.

    The pump is a bounded loop calling ``app.processEvents()`` with a
    deadline (``timeout_seconds``).  ``time.sleep`` is NEVER used as
    the primary wait primitive — every wait is real Qt event-loop
    turns so cross-thread signals, ``QTimer`` ticks, and
    ``QThread.finished`` slots are dispatched deterministically.
    """

    def __init__(self, view: ChatView, timeout_seconds: float = 2.0):
        from PySide6.QtWidgets import QApplication

        self._view = view
        self._app = QApplication.instance()
        self._deadline = timeout_seconds

    def pump_until_widgets_appear(
        self,
        expected_user: int,
        expected_assistant: int,
    ) -> tuple[int, int]:
        """Pump events until the expected user/assistant widget counts
        are present in the view's layout, or until the deadline expires.

        Returns the (user_count, assistant_count) actually observed.
        Raises ``AssertionError`` if the deadline expires before the
        expected counts are reached (so a failing pump surfaces the
        defect loudly instead of silently producing a low count).
        """
        import time

        from PySide6.QtCore import QDeadlineTimer

        app = self._app
        deadline = QDeadlineTimer(int(self._deadline * 1000))
        last_user = -1
        last_assistant = -1
        # Drain ALL events once before the loop so any "finished-before-
        # timer" race fires its slots on the same turn we entered the
        # pump (matches the production path's behaviour).
        app.processEvents()
        while not deadline.hasExpired():
            users = self._view.findChildren(_user_widget_cls())
            assistants = self._view.findChildren(_assistant_widget_cls())
            last_user = len(users)
            last_assistant = len(assistants)
            if last_user >= expected_user and last_assistant >= expected_assistant:
                # Spin a couple more processEvents to allow the
                # finished sentinel to land and tear down the timer.
                for _ in range(5):
                    app.processEvents()
                # And confirm the restore is in its clean-finished
                # state (no leaked ``_in_restore`` flag).
                if not self._view._in_restore:
                    return last_user, last_assistant
            app.processEvents()
            # Yield to the OS scheduler briefly so a slow CI box can
            # deliver timer ticks.  We deliberately use a tiny sleep
            # (1 ms) only as a CPU-yielding hint, not as a wait
            # primitive — the deadline is the real authority.
            time.sleep(0.001)
        # Deadline reached. Surface the observed counts so the failure
        # message names the defect (zero widgets built).
        raise AssertionError(
            f"restore did not paint widgets within {self._deadline}s "
            f"(observed user={last_user}, assistant={last_assistant}; "
            f"expected user>={expected_user}, assistant>={expected_assistant}). "
            f"This is Bug 2: the drain timer is killed before the "
            f"worker's queued chunks are processed, leaving the chat "
            f"empty."
        )


@requires_real_qt
class TestRestoreConsumesAllPlaceholders(unittest.TestCase):
    """Every MessagePlaceholder inserted by ``restore_from_messages_async``
    must be replaced by real widgets (or collapsed for hidden specs)
    once the restore completes.

    This is the most direct assertion of Bug 2: in the broken code the
    ``_on_worker_finished`` slot ``deleteLater``s every remaining
    placeholder BEFORE the drain has had a chance to consume the
    queued chunks, so any spec that hadn't been drained yet has its
    placeholder destroyed without ever getting a real widget — and
    the corresponding chunk in the queue then ``pop``s a missing
    placeholder from ``self._placeholders`` (the dict entry was
    already removed by ``deleteLater``'s caller) and silently no-ops.

    With the fix, every chunk's spec lands on the drain BEFORE
    ``_on_worker_finished`` runs (or the drain re-fires from a fresh
    timer), so all placeholders are replaced.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls._qapp = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.view = _chat_view_cls()()
        self.view.resize(640, 480)
        self.addCleanup(self.view.shutdown)
        self.addCleanup(self.view.deleteLater)

    def test_no_placeholders_remain_after_two_message_restore(self) -> None:
        """The Bug 2 failure mode: placeholders are deleteLater'd by
        ``_on_worker_finished`` while the drain timer is being killed,
        so the queued chunks find no placeholder to replace."""
        messages = [
            _user_message("hello", msg_id="u1"),
            _assistant_message("hi", msg_id="a1"),
        ]
        self.view.restore_from_messages_async(messages)
        # Sanity: the placeholders were inserted up front.
        self.assertEqual(
            len(self.view._placeholders),
            2,
            "restore_from_messages_async must insert one placeholder per message.",
        )
        # Pump the event loop until the restore completes.
        harness = _AsyncRestoreHarness(self.view, timeout_seconds=2.0)
        harness.pump_until_widgets_appear(expected_user=1, expected_assistant=1)
        # And the layout must hold no leftover placeholders.
        from PySide6.QtWidgets import QApplication

        QApplication.instance().processEvents()
        # Every placeholder the view inserted at the start of the
        # restore must be gone — either replaced by a real widget or
        # collapsed to zero height.  Any survivor means the drain did
        # not consume every chunk.
        self.assertEqual(
            self.view._placeholders,
            {},
            "Every MessagePlaceholder must be popped from the "
            "tracking dict once the restore completes; survivors "
            "mean a chunk the drain did not consume (Bug 2).",
        )
        # And: every MessagePlaceholder still alive as a Qt widget
        # inside the layout is a leak — none should remain once the
        # real widgets have been inserted.
        layout = self.view._layout
        layout_placeholders: list[MessagePlaceholder] = []
        if layout is not None:
            for i in range(layout.count()):
                item = layout.itemAt(i)
                widget = item.widget() if item is not None else None
                if isinstance(widget, MessagePlaceholder):
                    layout_placeholders.append(widget)
        self.assertEqual(
            layout_placeholders,
            [],
            "No MessagePlaceholder must remain in the layout after the "
            "restore completes; a survivor is a chunk the drain did "
            "not consume (Bug 2).",
        )


@requires_real_qt
class TestLoadOlderClickGrowsCap(unittest.TestCase):
    """Clicking the "Load older" button must grow the cap and re-render.

    The button is shown only when the session has more messages than
    the current render cap; clicking it doubles the cap and re-runs
    ``restore_from_messages_async`` so the user can page back into the
    history without paying the full restore cost up front.  A regression
    that breaks the cap-growth arithmetic (e.g. an off-by-one in
    ``_next_cap``) would be invisible to a single-restore test.

    This test goes beyond ``test_chat_view_restore.py``'s
    ``test_load_older_grows_cap_and_re_renders`` by also asserting the
    Load older button itself becomes clickable after the initial restore
    (catches a regression where the button is hidden or never created).
    """

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls._qapp = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.view = _chat_view_cls()()
        self.view.resize(640, 480)
        self.addCleanup(self.view.shutdown)
        self.addCleanup(self.view.deleteLater)

    def test_load_older_button_appears_and_grows_cap_on_click(self) -> None:
        # Build a session MUCH larger than the default cap so the
        # doubling math does not clamp to the total.  4x the cap means
        # one click → 2x, two clicks → 4x = total.  The first click
        # tests the doubling arithmetic in isolation.
        total = _RESTORE_DEFAULT_MAX_RENDERED * 4
        messages: list[Message] = []
        for i in range(total):
            if i % 2 == 0:
                messages.append(_user_message(f"q{i}", msg_id=f"u{i}"))
            else:
                messages.append(_assistant_message(f"a{i}", msg_id=f"a{i}"))
        self.view.restore_from_messages_async(messages)
        harness = _AsyncRestoreHarness(self.view, timeout_seconds=3.0)
        # At least one chunk's worth of widgets must land.
        harness.pump_until_widgets_appear(
            expected_user=1,
            expected_assistant=1,
        )
        # The "Load older" button must be present.
        from PySide6.QtWidgets import QApplication

        QApplication.instance().processEvents()
        self.assertIsNotNone(
            self.view._load_older_btn,
            "When the session exceeds the default render cap, the "
            "ChatView must surface a 'Load older' button at the top "
            "of the layout.",
        )
        # Capture the cap before clicking — the click must grow it.
        cap_before = self.view._restore_max_rendered
        self.assertEqual(
            cap_before,
            _RESTORE_DEFAULT_MAX_RENDERED,
            "The render cap must start at the default for a fresh "
            "session.",
        )
        # Click it and verify the cap doubled.
        self.view._load_older_btn.click()
        QApplication.instance().processEvents()
        cap_after = self.view._restore_max_rendered
        self.assertGreater(
            cap_after,
            cap_before,
            "Clicking 'Load older' must grow the render cap (it is "
            "a user affordance to page back into older history).",
        )
        # The cap must have exactly doubled (session is large enough
        # that no clamping kicked in).
        self.assertEqual(
            cap_after,
            cap_before * 2,
            "_next_cap must double the current cap (the user reaches "
            "the beginning in log2(N) clicks).",
        )


if __name__ == "__main__":
    unittest.main()
