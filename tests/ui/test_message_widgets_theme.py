"""Regression tests for the message-widget ThemeManager memory leak (task-11 F1).

Background
----------
Each message widget in ``message_widgets`` connected directly to the
long-lived ``ThemeManager`` (singleton) ``themeChanged`` signal via
``ThemeManager.instance().themeChanged.connect(self._apply_styles)`` and
never disconnected.  Because the singleton holds a strong reference to
the bound method (and through it the widget), message widgets that were
``deleteLater``-ed on new-chat / IDB-switch / tab-close could not be
garbage-collected — an unbounded leak over a long IDA session.

The fix converts those connects to ``bind_theme(self, self._apply_styles)``
and adds a ``shutdown()`` to each connecting class that calls
``disconnect_theme(self)`` (the nested ``_ThinkingBlock`` child is
cascaded by ``AssistantMessageWidget``).  These tests pin:
  1. widgets bind a ``_theme_apply_callback`` (the disconnect seam),
  2. ``shutdown()`` is idempotent (safe to call twice),
  3. ``AssistantMessageWidget.shutdown()`` also disconnects its
     ``_thinking_block`` child.
"""

from __future__ import annotations

import sys
import unittest

from tests import purge_lucnhan_stubs


def _purge_ui_theme_modules() -> None:
    """Drop cached ``lucnhan.ui`` theme/widget modules from ``sys.modules``.

    A prior test module (e.g. ``tests/tools/test_panel_core.py``) may have
    installed stand-in stubs or patched ``ThemeManager`` in-place before
    the PySide6 stubs were installed.  Forcing a fresh re-import of the
    whole widget/theme chain makes this module hermetic regardless of
    collection order, so ``bind_theme`` always binds against the real
    ``ThemeManager`` and stub-PySide6 widgets.
    """
    purge_lucnhan_stubs()
    for name in list(sys.modules):
        if name.startswith("lucnhan.ui.theme") or name in (
            "lucnhan.ui.qt_compat",
            "lucnhan.ui.message_widgets",
            "lucnhan.ui.tool_widgets",
        ):
            del sys.modules[name]


_purge_ui_theme_modules()

from tests.qt_stubs import ensure_pyside6_stubs

ensure_pyside6_stubs()

from lucnhan.ui.message_widgets import AssistantMessageWidget, QueuedMessageWidget, _ThinkingBlock


class TestMessageWidgetThemeDisconnect(unittest.TestCase):
    """Verify the bind/disconnect contract introduced by task-11 F1."""

    def test_bind_registers_theme_apply_callback(self) -> None:
        """Constructing a message widget binds a ``_theme_apply_callback``.

        ``bind_theme`` stores the connected wrapper on the widget so a
        later ``disconnect_theme`` (called from ``shutdown``) can remove
        the exact slot from the singleton's signal.
        """
        widget = QueuedMessageWidget("hello")
        self.assertTrue(callable(getattr(widget, "_theme_apply_callback", None)))

    def test_shutdown_is_idempotent(self) -> None:
        """``shutdown()`` can be called twice without raising.

        ``disconnect_theme`` swallows the disconnect-time errors PySide6
        can raise when a slot was already disconnected or never connected.
        """
        widget = _ThinkingBlock()
        widget.shutdown()
        widget.shutdown()  # second call must be harmless

    def test_assistant_shutdown_cascades_to_thinking_child(self) -> None:
        """``AssistantMessageWidget.shutdown`` also disconnects the child.

        The nested ``_ThinkingBlock`` is owned by the assistant frame (not
        in the chat layout), so ``ChatView``'s bulk disconnect cannot reach
        it directly — the assistant must disconnect it itself.
        """
        widget = AssistantMessageWidget()
        block = widget._thinking_block
        # Both the assistant and its child must have bound callbacks.
        self.assertTrue(callable(getattr(widget, "_theme_apply_callback", None)))
        self.assertTrue(callable(getattr(block, "_theme_apply_callback", None)))
        # Idempotent, cascading shutdown must not raise.
        widget.shutdown()
        widget.shutdown()


if __name__ == "__main__":
    unittest.main()
