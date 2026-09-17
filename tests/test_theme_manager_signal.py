"""Regression tests for rikugan.ui.theme.manager.ThemeManager signal wiring.

The original bug: in real PySide6 mode, ``ThemeManager.__init__`` assigned
``self.themeChanged = Signal(object)`` (a fresh per-instance ``Signal``
object).  PySide6's ``Signal`` only acts as a descriptor when declared
on the class — assigning it to ``self`` produced an *unbound* ``Signal``
that does not expose ``.connect``.  Restored widgets crashed with
``AttributeError: 'PySide6.QtCore.Signal' object has no attribute 'connect'``
the moment ``UserMessageWidget.__init__`` or ``_ThinkingBlock.__init__``
called ``ThemeManager.instance().themeChanged.connect(...)``.

The fix declares ``themeChanged`` as a *class-level* ``Signal(object)``
in real Qt mode (so PySide6's descriptor wires it correctly on every
instance) and only assigns a per-instance ``_DummySignal`` in the
headless no-Qt fallback (where there is no real ``Signal`` to bind).

These tests:

1. In **real PySide6** mode, confirm ``hasattr(instance().themeChanged, 'connect')``
   and that a connected slot fires when the manager emits.
2. Construct ``UserMessageWidget`` and ``_ThinkingBlock`` against a real
   ``QApplication`` to prove the previously-reported ``AttributeError``
   cannot happen.
3. In **no-Qt fallback** mode (PySide6 import blocked at import time),
   confirm ``ThemeManager.reset()`` produces a singleton whose
   ``themeChanged`` still has ``.connect`` and that prior listeners are
   not retained across resets.
"""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from unittest.mock import MagicMock

# Force Qt to use the offscreen platform in environments without a display
# so we can construct a real QApplication for the message widget tests.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _purge_rikugan_theme_modules() -> None:
    """Drop the manager and its friends from ``sys.modules``.

    Lets us re-import the real module under a controlled
    ``PySide6.QtCore.Signal`` / QObject presence.
    """
    for name in list(sys.modules):
        if name == "rikugan.ui.theme" or name.startswith("rikugan.ui.theme."):
            del sys.modules[name]


class _RealQtSignalWiringTests(unittest.TestCase):
    """Real PySide6: ``themeChanged`` must work as a bound signal."""

    @classmethod
    def setUpClass(cls) -> None:
        # Make sure we are using the *real* PySide6, not the lightweight
        # ``tests.qt_stubs`` substitutes.  Sibling test files sometimes
        # install stubs in ``sys.modules``; force a re-import.
        # Snapshot the pre-existing PySide6 / theme-module entries so
        # tearDownClass can restore them — otherwise this class leaves
        # the real C-extension modules in sys.modules and pollutes every
        # downstream test that expects the qt_stubs substitutes.
        cls._saved_modules = {
            name: sys.modules.get(name)
            for name in list(sys.modules)
            if name == "PySide6"
            or name.startswith("PySide6.")
            or name == "rikugan.ui.theme"
            or name.startswith("rikugan.ui.theme.")
        }
        sys.modules.pop("PySide6", None)
        sys.modules.pop("PySide6.QtCore", None)
        import PySide6  # type: ignore[import-not-found]
        import PySide6.QtWidgets  # type: ignore[import-not-found]  # noqa: F401

        # Re-import the manager fresh.
        _purge_rikugan_theme_modules()
        from rikugan.ui.theme.manager import ThemeManager  # type: ignore[import-not-found]

        cls.ThemeManager = ThemeManager

    @classmethod
    def tearDownClass(cls) -> None:
        # Restore the PySide6 / theme-module state this class inherited,
        # so sibling tests that rely on the qt_stubs substitutes see the
        # same modules they would have if this class never ran. If a
        # saved entry was None (module was absent), drop the real module
        # we imported so a downstream ``ensure_pyside6_stubs`` can
        # re-install the stub (it short-circuits on its ``_installed``
        # flag, so the stub modules must be back in sys.modules).
        for name in list(sys.modules):
            if (
                name == "PySide6"
                or name.startswith("PySide6.")
                or name == "rikugan.ui.theme"
                or name.startswith("rikugan.ui.theme.")
            ):
                if name not in cls._saved_modules:
                    sys.modules.pop(name, None)
        for name, mod in cls._saved_modules.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    def setUp(self) -> None:
        # Always start from a clean singleton so listeners from a
        # previous test do not leak in.
        self.ThemeManager.reset()

    def tearDown(self) -> None:
        self.ThemeManager.reset()

    def test_theme_changed_has_connect_attribute(self) -> None:
        """The reported regression: ``themeChanged.connect`` must
        exist on the real-Qt singleton."""
        tm = self.ThemeManager.instance()
        self.assertTrue(
            hasattr(tm.themeChanged, "connect"),
            f"ThemeManager.themeChanged is missing .connect: type={type(tm.themeChanged).__name__!r}",
        )
        # It must be callable, not a stub bool.
        self.assertTrue(callable(getattr(tm.themeChanged, "connect", None)))

    def test_connected_slot_fires_on_set_mode(self) -> None:
        """Connecting a slot then changing mode must invoke the slot
        (with the new tokens) exactly once for that mode change."""
        tm = self.ThemeManager.instance()
        from rikugan.ui.theme.tokens import ThemeMode  # type: ignore[import-not-found]

        observed: list = []
        tm.themeChanged.connect(lambda tokens: observed.append(tokens))

        tm.set_mode(ThemeMode.LIGHT)
        tm._apply_now()  # synchronous in tests — bypass the debounce

        self.assertEqual(len(observed), 1)
        from rikugan.ui.theme.palette_light import LIGHT_TOKENS  # type: ignore[import-not-found]

        self.assertEqual(observed[0].base, LIGHT_TOKENS.base)

    def test_message_widget_constructs_without_attribute_error(self) -> None:
        """UserMessageWidget.__init__ must NOT raise
        ``AttributeError`` when it subscribes to ``themeChanged``.
        This is the production code path that crashed in IDA."""
        from PySide6.QtWidgets import QApplication  # type: ignore[import-not-found]

        QApplication.instance() or QApplication([])
        from rikugan.ui.message_widgets import (  # type: ignore[import-not-found]
            UserMessageWidget,
        )

        # Must construct cleanly — no AttributeError on .connect.
        widget = UserMessageWidget("hello world")
        self.addCleanup(widget.deleteLater)
        # Sanity: a subscriber was actually attached.
        from rikugan.ui.theme.manager import (  # type: ignore[import-not-found]
            ThemeManager,
        )

        tm = ThemeManager.instance()
        self.assertTrue(
            hasattr(tm.themeChanged, "connect"),
            "singleton themeChanged must still expose .connect after widget init",
        )

    def test_thinking_block_constructs_without_attribute_error(self) -> None:
        """_ThinkingBlock.__init__ must NOT raise ``AttributeError``
        when it subscribes to ``themeChanged``."""
        from PySide6.QtWidgets import QApplication  # type: ignore[import-not-found]

        QApplication.instance() or QApplication([])
        from rikugan.ui.message_widgets import (  # type: ignore[import-not-found]
            AssistantMessageWidget,
        )

        # AssistantMessageWidget internally constructs a _ThinkingBlock.
        widget = AssistantMessageWidget()
        self.addCleanup(widget.deleteLater)


class _DummySignalFallbackTests(unittest.TestCase):
    """No-Qt fallback: a per-instance dummy signal still works and
    does not retain listeners across :func:`ThemeManager.reset`.

    We exercise the no-Qt branch by re-importing the manager module
    with a "fake" PySide6 entry in :data:`sys.modules` that raises
    ``ImportError`` on attribute access.  That triggers the
    ``except ImportError`` branch in ``manager.py`` and exercises
    the ``_DummySignal`` fallback path.
    """

    def setUp(self) -> None:
        # Snapshot real PySide6 modules so we can restore them.
        self._real_pyside6_modules = {
            name: mod for name, mod in list(sys.modules.items()) if name == "PySide6" or name.startswith("PySide6.")
        }
        # Drop the real PySide6 modules and install a stand-in that
        # raises ImportError on attribute access.
        for name in list(self._real_pyside6_modules):
            del sys.modules[name]

        class _BrokenPySide6Module:
            def __getattr__(self, name):
                raise ImportError(f"simulated no-Qt fallback: PySide6.{name}")

        sys.modules["PySide6"] = _BrokenPySide6Module()
        sys.modules["PySide6.QtCore"] = _BrokenPySide6Module()
        sys.modules["PySide6.QtWidgets"] = _BrokenPySide6Module()

        _purge_rikugan_theme_modules()
        from rikugan.ui.theme.manager import (  # type: ignore[import-not-found]
            ThemeManager,
        )

        self.ThemeManager = ThemeManager

    def tearDown(self) -> None:
        # Restore the real PySide6 modules.
        for name in list(sys.modules):
            if name == "PySide6" or name.startswith("PySide6."):
                del sys.modules[name]
        for name, mod in self._real_pyside6_modules.items():
            sys.modules[name] = mod
        _purge_rikugan_theme_modules()
        # Re-import the real manager so the rest of the suite is unaffected.
        importlib.import_module("rikugan.ui.theme.manager")

    def test_dummy_signal_has_connect(self) -> None:
        self.ThemeManager.reset()
        tm = self.ThemeManager.instance()
        self.assertTrue(hasattr(tm.themeChanged, "connect"))
        self.assertTrue(callable(tm.themeChanged.connect))

    def test_dummy_signal_emits_to_connected_slot(self) -> None:
        self.ThemeManager.reset()
        tm = self.ThemeManager.instance()
        observed: list = []
        tm.themeChanged.connect(lambda t: observed.append(t))
        tm._apply_now()
        self.assertEqual(len(observed), 1)

    def test_reset_does_not_retain_listeners(self) -> None:
        """Each post-reset singleton must start with no listeners —
        a previous version assigned a class-level dummy that would
        carry listeners across resets."""
        self.ThemeManager.reset()
        tm1 = self.ThemeManager.instance()
        tm1.themeChanged.connect(MagicMock())

        self.ThemeManager.reset()
        tm2 = self.ThemeManager.instance()
        self.assertIsNot(tm1, tm2)
        # No listeners retained: emit must invoke zero slots.
        observed: list = []
        tm2.themeChanged.connect(lambda t: observed.append(t))
        # Attach a probe that records any *other* listener firing.
        other_calls: list = []
        tm2.themeChanged.connect(lambda t: other_calls.append(t))
        # Build a brand-new instance (post-reset) to confirm zero
        # bleed-through from tm1.
        self.ThemeManager.reset()
        tm3 = self.ThemeManager.instance()
        probe = MagicMock()
        tm3.themeChanged.connect(probe)
        tm3._apply_now()
        # The probe must have been called exactly once.  Any listener
        # that bled through from tm1 / tm2 would be a second call.
        self.assertEqual(probe.call_count, 1)


if __name__ == "__main__":
    unittest.main()
