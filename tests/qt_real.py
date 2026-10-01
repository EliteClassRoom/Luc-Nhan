"""Shared guard for tests that need the REAL PySide6, not ``qt_stubs``.

Several test modules in this suite call
:func:`tests.qt_stubs.ensure_pyside6_stubs`, which replaces the
``PySide6.*`` submodules in :data:`sys.modules` with minimal
``types.ModuleType`` fakes.  The stubs model the API surface the
*stub-based* tests need and deliberately omit the rest — no
``QScrollArea.viewport``, no ``QCoreApplication.sendPostedEvents``, no
``PySide6.QtTest``.

A test that constructs a real ``ChatView``, posts a real
``QMouseEvent``, or pumps a real event loop therefore cannot run in a
session where a sibling already installed the stubs.  Without an
explicit guard such a test fails with a confusing
``AttributeError`` on a private Qt detail instead of a clear "skipped,
Qt is stubbed here" — so the helper below detects that state and skips.

The sentinel is ``QScrollArea.viewport``: present on the real shiboken
class, absent from the stub.  ``hasattr`` is the only reliable
discriminator because both the real and stubbed classes report
``__module__ == "PySide6.QtWidgets"``.
"""

from __future__ import annotations

import importlib
import unittest


def real_qt_available() -> bool:
    """Return ``True`` when the real PySide6 Qt classes are importable.

    ``False`` means a sibling test has installed the ``qt_stubs``
    fakes, so real-widget behaviour cannot be exercised in this
    session.  Callers should skip rather than fail.
    """
    try:
        from PySide6.QtWidgets import QScrollArea
    except Exception:
        return False
    return hasattr(QScrollArea, "viewport")


_SKIP_REASON = (
    "real PySide6 is unavailable: a sibling test module installed "
    "tests.qt_stubs, which replaces PySide6.* with minimal fakes. "
    "Run this file in a session without the stubs (e.g. "
    "`pytest tests/ui/<this_file>.py` on its own)."
)

#: ``unittest`` decorator for test classes that drive real Qt widgets.
requires_real_qt = unittest.skipUnless(real_qt_available(), _SKIP_REASON)


def live_class(dotted: str) -> type:
    """Resolve ``dotted`` against the CURRENTLY-LOADED module, every call.

    Several test files purge ``rikugan.ui.*`` from :data:`sys.modules`
    at import time (to drop a sibling's ``types.ModuleType`` stubs).
    When two such files are collected in one session the second purge
    triggers a genuine re-import, so the module object a test captured
    at import time can be a DIFFERENT object from the one the live
    widget tree was built from.

    That is invisible until it bites: ``QObject.findChildren`` matches
    by exact type, so a widget built by the freshly imported
    ``MessageWidget`` is not found by a reference to the previously
    imported one — and the assertion reports "0 widgets rendered" even
    though the chat painted perfectly.

    Resolving at call time (inside the test body) always yields the
    class object the live tree actually uses, which makes these tests
    order-independent.

    Raises ``ImportError`` if the module cannot be imported at all.
    """
    module_name, _, attr = dotted.rpartition(".")
    module = importlib.import_module(module_name)
    return getattr(module, attr)


__all__ = ["live_class", "real_qt_available", "requires_real_qt"]
