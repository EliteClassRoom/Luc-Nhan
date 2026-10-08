"""IDAThemeWatcher — polls QApplication.palette() and notifies ThemeManager.

Only meaningful in IDA hosts. Started by PLUGIN_ENTRY for the IDA host.
No-op outside IDA (do not start the watcher there).

Behavior:
- Polls every ``interval_ms`` (default 500) via ``QTimer.singleShot``
  (recursive scheduling) so no persistent QTimer object is retained.
- Compares ``(Window, WindowText)`` color signature against the last
  seen one. IDA only flips these two roles on a theme switch, so this
  two-color key is sufficient to detect all user-visible changes.
- On change → calls ``ThemeManager.refresh_from_host()`` which
  recomputes the IDA_NATIVE tokens and emits ``themeChanged``.
- Catches all exceptions in the tick loop to avoid crashing the Qt
  event loop on transient palette access errors.
"""

from __future__ import annotations

import threading

from ...core.logging import log_error

# All Qt imports must go through the shared compatibility layer. Direct
# PySide6/PyQt5 imports would load Qt6 DLLs into a Qt5-based IDA host
# and crash the process (see ``lucnhan/ui/qt_compat.py``).
from ..qt_compat import QObject, QPalette, QTimer
from .manager import ThemeManager


def _palette_signature(pal: QPalette) -> tuple[str, str]:
    """Two-color signature ``(Window, WindowText)`` used for change detection.

    IDA only toggles Window + WindowText on a theme switch, so this key
    is sufficient and avoids spurious refreshes on role changes we
    don't actually use.
    """
    return (
        pal.color(QPalette.ColorRole.Window).name(),
        pal.color(QPalette.ColorRole.WindowText).name(),
    )


class IDAThemeWatcher(QObject):
    """Polls ``QApplication.palette()`` and notifies the manager on change.

    Lifecycle:
    - ``start()`` — begin polling (idempotent).
    - ``stop()`` — stop polling. Subsequent ticks will not reschedule.

    The watcher uses ``QTimer.singleShot`` recursively rather than a
    persistent ``QTimer`` so there is no ``QTimer`` object to clean up
    when the watcher is destroyed. The ``threading.Event`` ``_alive``
    is the single source of truth for "should I reschedule?".
    """

    def __init__(self, interval_ms: int = 500) -> None:
        super().__init__()
        self._interval_ms = interval_ms
        self._last_sig: tuple[str, str] | None = None
        self._alive = threading.Event()

    def start(self) -> None:
        """Begin polling. Idempotent — a second call while running is a no-op."""
        if self._alive.is_set():
            return
        self._alive.set()
        # Use QTimer.singleShot (recursive) so we don't hold a QTimer
        # object that would need explicit deletion. The first tick is
        # scheduled to fire after ``_interval_ms``; subsequent ticks
        # reschedule themselves only if ``_alive`` is still set.
        QTimer.singleShot(self._interval_ms, self._tick)

    def stop(self) -> None:
        """Stop polling. Subsequent ticks will not reschedule themselves."""
        self._alive.clear()

    def _tick(self) -> None:
        """Single poll cycle. Reschedules itself if still alive."""
        if not self._alive.is_set():
            return
        try:
            # ThemeManager._app_source is the test seam — production
            # returns QApplication.instance(); tests can override the
            # method (or assign an instance attribute) to inject a
            # fake source. The seam keeps PySide6 imports out of the
            # tick body, so stub environments degrade gracefully.
            source = ThemeManager.instance()._app_source()
            if source is None:
                return
            sig = _palette_signature(source.palette())
            if sig != self._last_sig:
                self._last_sig = sig
                ThemeManager.instance().refresh_from_host()
        except Exception as e:
            # Swallow all exceptions: the tick runs on the Qt event
            # loop, and an unhandled error here would tear down the
            # loop and freeze the host UI. ``log_error`` takes a single
            # ``msg`` arg, so embed the exception class + repr in the
            # message — enough for postmortem debugging without
            # threading extra context through the logging facade.
            log_error(f"ThemeWatcher tick failed: {type(e).__name__}: {e}")
        finally:
            if self._alive.is_set():
                QTimer.singleShot(self._interval_ms, self._tick)
