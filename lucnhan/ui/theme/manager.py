"""ThemeManager singleton and color math helpers.

The manager owns the current ThemeMode and exposes:
- mode: the user-selected mode (read/write)
- tokens(): the resolved ThemeTokens for the current mode
- themeChanged: signal emitted on mode change (carries the new ThemeTokens)
- set_mode(mode): change mode (triggers debounced QSS rebuild)

Helpers (private/public):
- hex_luminance(hex): sRGB-linearized luminance (IEC 61966-2-1)
- blend_hex(h1, h2, t): linear interpolation between two #rrggbb colors
- blend_tokens(a, b, t): linear interpolation between two token sets
- format_template(s, mapping): str.format with {placeholders}
- is_dark_tokens(tokens): True when tokens.window is dark (lum < 0.5)
"""

from __future__ import annotations

from typing import Any

# Lazy/optional Qt imports via the shared compatibility layer. Direct
# PySide6/PyQt5 imports are forbidden in this module — importing Qt6 in a
# Qt5-based IDA host can crash the process. See ``lucnhan/ui/qt_compat.py``.
try:
    from ..qt_compat import QApplication, QObject, QTimer, Signal

    _HAS_QT = True
except (ImportError, ModuleNotFoundError):  # pragma: no cover — headless fallback
    QObject = object  # type: ignore[assignment,misc]
    QTimer = None  # type: ignore[assignment]
    Signal = None  # type: ignore[assignment]
    QApplication = None  # type: ignore[assignment]
    _HAS_QT = False

from .palette_dark import DARK_TOKENS
from .palette_light import LIGHT_TOKENS
from .tokens import (  # noqa: F401 — re-exported
    ThemeMode,
    ThemeTokens,
    hex_luminance,
    is_dark_tokens,
)

# Logging is best-effort: when other tests in the suite replace
# lucnhan.core.logging with a stub module that lacks get_logger, we
# fall back to a no-op logger so manager.py can still be imported.
try:
    from ...core.logging import get_logger
except ImportError:  # pragma: no cover
    import logging as _logging

    def get_logger() -> _logging.Logger:  # type: ignore[no-redef]
        return _logging.getLogger("LucNhan")


# Module-level logger used for best-effort fallback diagnostics. Theme code
# runs during early UI construction and Qt teardown where raising would
# destabilize the host — failures are logged at debug instead of swallowed.
logger = get_logger()


# === Helpers (public) ===
#
# ``hex_luminance`` lives in ``.tokens`` (re-exported above) so the
# leaf module is the single source of truth. ``blend_hex`` is defined
# here because ``palette_ida`` imports it during its own module load
# and would create a cycle if it were moved into ``.tokens``.


def _hex_to_linear_rgb(hex_color: str) -> tuple[float, float, float]:
    """Convert #rrggbb to linear-light (R, G, B) in 0-1 range."""
    h = hex_color.strip().lower()
    if not (len(h) == 7 and h.startswith("#")):
        return (0.0, 0.0, 0.0)
    r = int(h[1:3], 16) / 255.0
    g = int(h[3:5], 16) / 255.0
    b = int(h[5:7], 16) / 255.0

    def _srgb_to_linear(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return (_srgb_to_linear(r), _srgb_to_linear(g), _srgb_to_linear(b))


def _linear_rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    """Convert linear-light (R, G, B) in 0-1 range to #rrggbb."""

    def _linear_to_srgb(c: float) -> int:
        c = max(0.0, min(1.0, c))
        s = c * 12.92 if c <= 0.00304 else 1.055 * (c ** (1 / 2.4)) - 0.055
        return round(s * 255)

    r, g, b = rgb
    return f"#{_linear_to_srgb(r):02x}{_linear_to_srgb(g):02x}{_linear_to_srgb(b):02x}"


def blend_hex(h1: str, h2: str, t: float) -> str:
    """Linearly interpolate two #rrggbb colors in sRGB-linear space.

    t=0.0 returns h1, t=1.0 returns h2, t=0.5 is the midpoint. Used by
    both blend_tokens (per-field blending) and palette_ida (semantic
    hue blending). Module-level so palette_ida can import the primitive
    directly without rebuilding full ThemeTokens.
    """
    c1 = _hex_to_linear_rgb(h1)
    c2 = _hex_to_linear_rgb(h2)
    out = tuple(c1[i] * (1 - t) + c2[i] * t for i in range(3))
    return _linear_rgb_to_hex(out)


def asdict_fields() -> list[str]:
    """Return the 20 ThemeTokens field names in declaration order."""
    return [
        "window",
        "window_text",
        "base",
        "alt_base",
        "text",
        "button",
        "button_text",
        "highlight",
        "highlight_text",
        "mid",
        "light",
        "dark",
        "success",
        "warning",
        "error",
        "code_text",
        "code_bg",
        "accent",
        "selection",
        "muted_text",
    ]


# === ThemeManager (singleton) ===

_DEBOUNCE_MS = 50


# A trivial Signal stand-in for headless / no-Qt environments. Calls to
# ``emit`` become no-ops and ``connect`` records listeners in a list.
class _DummySignal:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._listeners: list[Any] = []

    def connect(self, slot: Any) -> None:
        self._listeners.append(slot)

    def disconnect(self, slot: Any = None) -> None:
        if slot is None:
            self._listeners.clear()
        else:
            try:
                self._listeners.remove(slot)
            except ValueError:
                pass

    def emit(self, *args: Any, **kwargs: Any) -> None:
        for listener in list(self._listeners):
            try:
                listener(*args, **kwargs)
            except Exception as exc:
                logger.debug("theme signal listener raised", exc_info=exc)


def _make_signal() -> Any:
    """Build a fresh ``_DummySignal`` for the no-Qt fallback.

    This helper is no longer used to build real ``Signal`` instances
    (those are declared at class level on :class:`ThemeManager`
    when PySide6 is available).  Kept for tests and any legacy
    callers that still construct a stand-in signal.
    """
    return _DummySignal()


class ThemeManager(QObject):  # type: ignore[misc, valid-type]
    """Singleton holding the active ThemeMode and resolved ThemeTokens.

    Lifecycle:
    - Use ThemeManager.instance() to get the singleton
    - set_mode(mode) updates the mode and emits themeChanged (with tokens)
    - tokens() returns the resolved ThemeTokens for the current mode
    - reset() (class method) clears the singleton (testability)

    Signal wiring
    -------------
    In real PySide6 mode, ``themeChanged`` is a class-level
    ``Signal(object)``.  PySide6's ``Signal`` is a descriptor that
    binds on instance attribute access — a class-level declaration
    is required for ``instance().themeChanged.connect(...)`` to
    work.  Assigning a fresh ``Signal`` to ``self.themeChanged`` in
    ``__init__`` (as the previous code did) was invalid: an
    instance-bound ``Signal`` does not expose ``.connect``, which
    caused ``UserMessageWidget`` and ``_ThinkingBlock`` to crash
    with ``AttributeError`` the moment they subscribed to
    ``themeChanged``.

    In the headless/no-Qt fallback, there is no real ``Signal`` to
    use, so ``__init__`` assigns a fresh ``_DummySignal`` to
    ``self.themeChanged``.  ``reset()`` then drops the old instance
    along with its listeners, so a post-reset singleton starts
    with a clean listener list — exactly the behaviour the
    class-level-fallback contract would have provided in PySide6
    via the per-instance QObject child signal namespace.
    """

    if _HAS_QT and Signal is not None:
        # Real PySide6: declare a class-level Signal so
        # ``instance().themeChanged.connect(...)`` works.
        themeChanged = Signal(object)  # type: ignore[arg-type,call-arg,misc]
    else:
        # Headless fallback: a class-level attribute is still
        # useful for ``hasattr`` checks.  It is a dummy object
        # whose ``connect`` is a no-op, replaced per-instance in
        # ``__init__`` (see below) so that :func:`reset` produces
        # a clean listener list.
        themeChanged: Any = None  # populated by _DummySignal() in __init__

    _instance: ThemeManager | None = None

    def __init__(self) -> None:
        if _HAS_QT:
            super().__init__()
            # Real PySide6 path: keep the class-level Signal intact;
            # do NOT shadow it with ``self.themeChanged = ...``,
            # that broke ``.connect`` in the previous revision.
        else:
            # No-Qt fallback: install a per-instance dummy signal so
            # ``reset()`` -> ``cls()`` produces a fresh listener
            # list (a class-level dummy would carry listeners
            # across resets).
            self.themeChanged = _DummySignal()
        self._mode: ThemeMode = ThemeMode.AUTO
        self._tokens_cache: ThemeTokens | None = None
        self._pending_apply: Any = None
        # Must be initialised BEFORE _compute_tokens(): in AUTO+IDA mode the
        # first compute reaches _log_auto_derive_once, which reads this flag.
        # Setting it after the compute call (the previous order) raised
        # AttributeError inside __init__ — silently caught by _compute_tokens'
        # broad except, which masked a successful derive with a DARK fallback.
        self._log_auto_derive_once_flag: bool = False
        # Compute initial tokens immediately so the first themeChanged
        # listener can render correctly.
        self._tokens_cache = self._compute_tokens()

    @classmethod
    def instance(cls) -> ThemeManager:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Clear the singleton. Test-only helper."""
        cls._instance = None

    @property
    def mode(self) -> ThemeMode:
        return self._mode

    def set_mode(self, mode: ThemeMode) -> None:
        """Set the theme mode. No-op if same value. Debounces rapid switches."""
        if mode == self._mode:
            return
        self._mode = mode
        self._tokens_cache = None
        if not _HAS_QT or QTimer is None:
            # No Qt: apply synchronously.
            self._apply_now()
            return
        if self._pending_apply is not None:
            self._pending_apply.stop()
        self._pending_apply = QTimer(self)
        self._pending_apply.setSingleShot(True)
        self._pending_apply.timeout.connect(self._apply_now)
        self._pending_apply.start(_DEBOUNCE_MS)

    def tokens(self) -> ThemeTokens:
        """Return ThemeTokens for the current mode (cached)."""
        if self._tokens_cache is None:
            self._tokens_cache = self._compute_tokens()
        return self._tokens_cache

    def refresh_from_host(self) -> None:
        """Re-derive tokens from the current QApplication palette."""
        if self._mode in (ThemeMode.DARK, ThemeMode.LIGHT):
            return
        self._tokens_cache = None
        if self._pending_apply is not None:
            self._pending_apply.stop()
            self._pending_apply = None
        self._apply_now()

    def _log_auto_derive_once(self, tokens: ThemeTokens) -> None:
        if self._log_auto_derive_once_flag:
            return
        self._log_auto_derive_once_flag = True
        try:
            from ...core.logging import log_info

            log_info(
                f"ThemeManager: AUTO mode deriving from host palette — "
                f"window={tokens.window}, text={tokens.text}, "
                f"base={tokens.base}."
            )
        except Exception as exc:
            logger.debug("AUTO-derive diagnostic log failed", exc_info=exc)

    def _app_source(self) -> Any:
        """Return the object to read QPalette from."""
        if QApplication is None:
            return None
        return QApplication.instance()

    def _sync_legacy_theme_helpers(self, tokens: ThemeTokens) -> None:
        """Mirror the live mode into the legacy ``styles`` helpers.

        ``styles._current_theme`` / ``styles._effective_theme`` are
        read by inline-styled widgets (e.g. ``get_tool_colors``,
        ``get_agent_status_colors``) and by ``is_host_theme()`` /
        ``is_dark_theme()``.  Until this hook ran inside
        ``_apply_now``, every theme change that originated from
        ``ThemeManager.set_mode`` (rather than
        ``LucNhanPanelCore.set_theme``) left the legacy module-level
        variables stale, so helper-palette colours continued to
        report the *previous* mode.

        The legacy mapping preserves the user-visible semantics:

        - ``ThemeMode.IDA_NATIVE`` -> legacy ``"ida"`` (so
          ``is_host_theme()`` returns True).
        - ``ThemeMode.AUTO`` -> legacy ``"ida"`` too (legacy helpers
          do not distinguish auto from ida; the effective palette is
          decided by the live QApplication).
        - ``ThemeMode.DARK`` / ``ThemeMode.LIGHT`` -> legacy
          ``"dark"`` / ``"light"`` so ``is_dark_theme()`` and the
          branch-keyed colour dicts (``TOOL_COLORS``,
          ``BULK_STATUS_COLORS``, ``AGENT_STATUS_COLORS``) flip with
          the user's choice.

        The helper must run *before* ``themeChanged.emit`` so any
        listener that consults ``styles.is_dark_theme()`` during the
        emit observes the new value.
        """
        try:
            from ..styles import set_current_theme
        except Exception as exc:
            logger.debug("set_current_theme import failed; legacy helpers stay stale", exc_info=exc)
            return

        try:
            if self._mode in (ThemeMode.IDA_NATIVE, ThemeMode.AUTO):
                legacy_value = "ida"
            else:
                legacy_value = self._mode.value
            effective = "dark" if is_dark_tokens(tokens) else "light"
            # Outside IDA, ``IDA_NATIVE`` falls back to DARK_TOKENS
            # (and ``AUTO`` outside IDA also falls back to DARK).
            # In that case the user picked the host-theme *mode*
            # but the resolved palette is dark, so legacy helpers
            # that gate on ``is_host_theme()`` would still report
            # host.  Force the effective branch to "dark" so the
            # two legacy helpers agree (host-mode + dark palette =
            # both True) and inline widgets read the dark dict.
            try:
                from ...core.host import is_ida
            except Exception:

                def is_ida() -> bool:  # type: ignore[no-redef]
                    return False

            if not is_ida() and self._mode in (ThemeMode.IDA_NATIVE, ThemeMode.AUTO):
                effective = "dark"
            set_current_theme(legacy_value, effective_theme=effective)
        except Exception as exc:
            logger.debug("legacy theme helper sync failed", exc_info=exc)

    def _apply_now(self) -> None:
        """Compute current tokens and emit ``themeChanged``.

        Order of operations matters here: the legacy
        ``styles._current_theme`` / ``_effective_theme`` mirror must
        be updated **before** the ``themeChanged`` emit so that any
        listener that consults ``styles.is_dark_theme()`` (e.g.
        ``get_tool_colors``, ``get_bulk_status_colors``,
        ``get_agent_status_colors``) sees the new branch during the
        call.  See :meth:`_sync_legacy_theme_helpers` for the
        mapping table.
        """
        tokens = self.tokens()
        self._sync_legacy_theme_helpers(tokens)
        try:
            # In real PySide6, ``themeChanged`` is a class-level
            # ``Signal`` descriptor bound to this instance.  In the
            # no-Qt fallback, ``__init__`` installed a
            # ``_DummySignal`` on ``self``.  Both expose ``.emit``,
            # but the ``Signal`` descriptor only routes through the
            # QObject machinery, so the call is safe regardless of
            # which mode is active.
            self.themeChanged.emit(tokens)
        except Exception as exc:
            logger.debug("themeChanged.emit raised", exc_info=exc)
        self._pending_apply = None

    def _compute_tokens(self) -> ThemeTokens:
        """Compute tokens for the current mode.

        AUTO: IDA → derive_ida_tokens; standalone → DARK_TOKENS.
        DARK: DARK_TOKENS.
        LIGHT: LIGHT_TOKENS.
        IDA_NATIVE: derive_ida_tokens (non-IDA → DARK_TOKENS + warning log).
        """
        try:
            from ...core.host import is_ida
        except Exception:

            def is_ida() -> bool:  # type: ignore[no-redef]
                return False

        try:
            from ...core.logging import log_warning
        except Exception:

            def log_warning(msg: str) -> None:  # type: ignore[no-redef]
                pass

        if self._mode == ThemeMode.DARK:
            return DARK_TOKENS
        if self._mode == ThemeMode.LIGHT:
            return LIGHT_TOKENS
        if self._mode == ThemeMode.AUTO:
            if is_ida():
                try:
                    from .palette_ida import derive_ida_tokens

                    app = self._app_source()
                    if app is not None:
                        tokens = derive_ida_tokens(app)
                        self._log_auto_derive_once(tokens)
                        return tokens
                except Exception as exc:
                    logger.debug("IDA AUTO derive failed; using DARK", exc_info=exc)
            return DARK_TOKENS
        if self._mode == ThemeMode.IDA_NATIVE:
            if not is_ida():
                try:
                    log_warning("IDA Native theme requested on non-IDA host; falling back to Dark")
                except Exception as exc:
                    logger.debug("log_warning stub raised", exc_info=exc)
                return DARK_TOKENS
            try:
                from .palette_ida import derive_ida_tokens

                app = self._app_source()
                if app is not None:
                    return derive_ida_tokens(app)
            except Exception as exc:
                logger.debug("IDA Native derive failed; using DARK", exc_info=exc)
            return DARK_TOKENS
        return DARK_TOKENS
