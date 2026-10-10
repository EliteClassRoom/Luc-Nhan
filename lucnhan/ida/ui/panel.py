"""IDA PluginForm wrapper around the shared Luc Nhan panel core.

This module provides IDA Pro theme integration, automatically detecting
the current color scheme (dark/light) and applying appropriate styling.
"""

import importlib
import importlib.util as _importlib_util
from typing import Any

from lucnhan.core.early_log import _early_log, _early_log_crash
from lucnhan.core.startup_timing import end, start
from lucnhan.ui.panel_core import LucNhanPanelCore
from lucnhan.ui.qt_compat import QApplication, QVBoxLayout, QWidget

from .actions import LucNhanUIHooks
from .session_controller import IdaSessionController
from .tools_form import LucNhanToolsForm

# Probe the IDA SDK presence cheaply with ``find_spec`` (does not run
# module code).  Mirrors ``actions._probe_ida`` so a single import path
# pattern covers every IDA-host module — Shiboken UAF mitigation on
# Python ≥ 3.11 (see ``AGENTS.md`` §1).
_IDA_MODULES_PANEL: tuple[str, ...] = ("idaapi", "ida_kernwin")


def _probe_ida_panel() -> bool:
    for _m in _IDA_MODULES_PANEL:
        try:
            spec = _importlib_util.find_spec(_m)
        except (ValueError, ModuleNotFoundError):
            continue
        if spec is None:
            return False
    return True


_HAS_IDA_PANEL = _probe_ida_panel()
_ida_panel_loaded = False


def _ensure_ida_panel() -> bool:
    """Import ``idaapi`` / ``ida_kernwin`` and bind them as module attrs.

    Idempotent: subsequent calls are free.  Mirrors ``actions._ensure_ida``.
    Returns ``_HAS_IDA_PANEL`` so callers can early-out when IDA is absent.
    """
    global _ida_panel_loaded
    if _ida_panel_loaded:
        return _HAS_IDA_PANEL
    _ida_panel_loaded = True
    if not _HAS_IDA_PANEL:
        return False
    import sys as _sys

    try:
        for _m in _IDA_MODULES_PANEL:
            setattr(_sys.modules[__name__], _m, importlib.import_module(_m))
        return True
    except ImportError:
        return False


# Bind the names eagerly when IDA is present so the rest of this file
# (class bodies, type annotations) can refer to ``idaapi`` etc. without
# going through ``__getattr__``.  When IDA is absent, the names remain
#   unbound and the ``if _HAS_IDA_PANEL:`` block below is skipped entirely.
if _ensure_ida_panel():
    idaapi = importlib.import_module("idaapi")  # type: ignore[assignment]
    ida_kernwin = importlib.import_module("ida_kernwin")


def _form_to_qt_widget(plugin_form_cls: Any, form: Any) -> Any:
    """Convert an IDA PluginForm handle to a Qt widget, robustly.

    IDA ≥ 9.0 ships PySide6, and on most versions ``FormToPySideWidget``
    is the correct call. IDA 9.4's ``ida_kernwin.TWidgetToPySideWidget``
    (the function backing ``FormToPySideWidget``) is buggy: it resolves
    ``ctx.QtGui.QWidget.FromCapsule(tw)``, but ``QWidget`` lives in
    ``PySide6.QtWidgets`` and the real class has no ``FromCapsule``
    method — so the call raises ``AttributeError``. On those versions we
    fall back to ``FormToPyQtWidget``, which takes a different internal
    path and still returns a PySide6 widget (IDA's PyQt5 is a thin shim
    over PySide6). Because ``qt_compat`` is PySide6-only, every Qt symbol
    Luc Nhan uses originates in PySide6, so a widget returned via either
    path type-matches our ``QVBoxLayout``/``QWidget``.

    ``plugin_form_cls`` is the ``idaapi.PluginForm`` subclass (typically
    ``type(self)``); passing it explicitly keeps this a module function
    while still letting tests patch the conversion methods on the
    subclass under test.
    """
    try:
        return plugin_form_cls.FormToPySideWidget(form)
    except (AttributeError, TypeError):
        return plugin_form_cls.FormToPyQtWidget(form)


def _early_log_crash_safe(exc: BaseException) -> None:
    """Crash-log sink tolerant of any failure.

    Wraps :func:`_early_log_crash` to provide a single import path even if
    ``_early_log_crash`` itself is unavailable (test environments that
    stub the module).
    """
    try:
        _early_log_crash(exc)
    except Exception:
        pass


def _log_teardown(context: str, exc: BaseException) -> None:
    """Best-effort log for swallowed teardown exceptions.

    Qt widget destruction and theme teardown run during IDA shutdown where
    raising would destabilize the host. Failures are surfaced via IDA's
    message log instead of being silently swallowed.
    """
    try:
        ida_kernwin.msg(f"[Lục nhãn] {context}: {type(exc).__name__}: {exc}\n")
    except Exception:
        pass  # IDA message log itself unavailable — nothing more to do.


def _get_ida_theme_colors() -> dict[str, tuple[int, int, int] | bool]:
    """Extract current IDA Pro theme colors.

    Returns a dictionary of color names to RGB tuples based on IDA's
    current color scheme. This allows Luc Nhan to blend in with IDA's UI.
    The key ``"is_dark"`` is always set to a bool indicating whether the
    detected background is dark — callers should use this to choose the
    helper palette for inline-styled widgets.
    """
    colors: dict[str, tuple[int, int, int] | bool] = {}
    is_dark: bool = False

    # Try to get colors from IDA's kernel window API.
    # Note: ida_kernwin.get_widget_color() returns IDA's internal widget colors,
    # NOT the custom Qt CSS theme colors the user has configured.
    # When it returns near-black values like (30,30,30) it's IDA's built-in fallback
    # for custom themes. Previously this case was forced to the Monokai Light
    # palette, which produced light widgets in a dark IDA installation. We now
    # keep the dark background and only fall back to a light palette when the
    # API is completely unavailable.
    try:
        bg_raw = _ida_color_to_rgb(ida_kernwin.get_widget_color(ida_kernwin.BCKCOLOR))
        bg_brightness = (bg_raw[0] * 299 + bg_raw[1] * 587 + bg_raw[2] * 114) / 1000
        if bg_brightness < 20:
            # API returned IDA's internal dark fallback. Keep dark colors
            # and only use Monokai Light for the text color contrast hint.
            colors["background"] = (30, 30, 30)
            colors["text"] = (220, 220, 220)
            is_dark = True
        else:
            colors["background"] = bg_raw
            try:
                colors["text"] = _ida_color_to_rgb(ida_kernwin.get_widget_color(ida_kernwin.FGCOLOR))
            except Exception:
                colors["text"] = (44, 44, 44)
            is_dark = bg_brightness < 128
    except Exception:
        # API completely unavailable — default to a neutral dark palette
        # so the panel does not look broken in custom IDA setups. The
        # caller still gets the correct ``is_dark`` flag and can override.
        colors["background"] = (30, 30, 30)
        colors["text"] = (220, 220, 220)
        is_dark = True

    # Calculate derived colors based on background brightness
    bg = colors["background"]
    if not isinstance(bg, tuple):
        bg = (30, 30, 30)
    bg_brightness = (bg[0] * 299 + bg[1] * 587 + bg[2] * 114) / 1000
    is_dark = bg_brightness < 128

    if is_dark:
        # Dark theme derived colors
        colors["surface"] = _lighten_color(bg, 15)
        colors["surface_variant"] = _lighten_color(bg, 25)
        colors["border"] = _lighten_color(bg, 35)
        colors["text_secondary"] = _blend_colors(colors["text"], bg, 0.6)
        colors["accent"] = (0, 122, 204)  # IDA's blue accent
        colors["accent_hover"] = (26, 138, 212)
        colors["selection"] = (38, 79, 120)
        colors["success"] = (78, 201, 176)
        colors["error"] = (199, 46, 46)
        colors["tool_header"] = (86, 156, 214)
        colors["tool_content"] = (156, 220, 254)
        # Code block: slightly darker than surface for contrast
        colors["code_block_bg"] = _lighten_color(bg, 5)
        colors["code_block_border"] = _lighten_color(bg, 20)
        colors["code_text"] = colors["text"]
    else:
        # Light theme derived colors
        colors["surface"] = _darken_color(bg, 10)
        colors["surface_variant"] = _darken_color(bg, 20)
        colors["border"] = _darken_color(bg, 30)
        colors["text_secondary"] = _blend_colors(colors["text"], bg, 0.6)
        colors["accent"] = (0, 102, 204)  # Darker blue for light theme
        colors["accent_hover"] = (0, 122, 224)
        colors["selection"] = (180, 210, 240)
        colors["success"] = (0, 128, 100)
        colors["error"] = (180, 50, 50)
        colors["tool_header"] = (0, 80, 160)
        colors["tool_content"] = (0, 100, 180)
        # Code block: warm gray, distinct from message background
        colors["code_block_bg"] = _darken_color(bg, 8)  # slightly darker warm surface
        colors["code_block_border"] = _darken_color(bg, 20)
        colors["code_text"] = colors["text"]

    colors["is_dark"] = is_dark
    return colors


def _ida_color_to_rgb(color_val: int) -> tuple[int, int, int]:
    """Convert IDA color value to RGB tuple.

    IDA stores colors as 0xBBGGRR (blue, green, red).
    """
    if color_val == 0xFFFFFFFF:  # Default/invalid color
        return (30, 30, 30)

    r = color_val & 0xFF
    g = (color_val >> 8) & 0xFF
    b = (color_val >> 16) & 0xFF
    return (r, g, b)


def _lighten_color(rgb: tuple[int, int, int], amount: int) -> tuple[int, int, int]:
    """Lighten an RGB color by a percentage amount."""
    r, g, b = rgb
    factor = 1 + (amount / 100)
    return (min(255, int(r * factor)), min(255, int(g * factor)), min(255, int(b * factor)))


def _darken_color(rgb: tuple[int, int, int], amount: int) -> tuple[int, int, int]:
    """Darken an RGB color by a percentage amount."""
    r, g, b = rgb
    factor = 1 - (amount / 100)
    return (max(0, int(r * factor)), max(0, int(g * factor)), max(0, int(b * factor)))


def _blend_colors(rgb1: tuple[int, int, int], rgb2: tuple[int, int, int], alpha: float) -> tuple[int, int, int]:
    """Blend two colors with the given alpha (0-1)."""
    return (
        int(rgb1[0] * alpha + rgb2[0] * (1 - alpha)),
        int(rgb1[1] * alpha + rgb2[1] * (1 - alpha)),
        int(rgb1[2] * alpha + rgb2[2] * (1 - alpha)),
    )


def _installed_font_family(family: str) -> str:
    """The installed family matching ``family``, or ``""``.

    Only a name the host actually has installed is ever returned, so a
    configured value cannot smuggle QSS into the wrapper's stylesheet
    (``Consolas'; font-size: 100pt`` matches nothing).  A case-insensitive
    hit returns the host's own spelling, which is what Qt expects.
    """
    name = (family or "").strip()
    if not name:
        return ""
    try:
        from lucnhan.ui.qt_compat import QFontDatabase

        installed = list(QFontDatabase.families())
    except Exception:  # pragma: no cover — no Qt / no font database
        return ""
    for candidate in installed:
        if candidate == name:
            return candidate
    lowered = name.casefold()
    for candidate in installed:
        if candidate.casefold() == lowered:
            return candidate
    return ""


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    """Convert RGB tuple to hex color string."""
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


class LucNhanPanel(idaapi.PluginForm):
    """IDA dockable form embedding the shared panel core widget.

    This panel automatically adapts to IDA Pro's current color theme,
    ensuring visual consistency with the rest of the IDA interface.
    """

    def __init__(self):
        _early_log("ida_panel:init:entry")
        super().__init__()
        # Theme watcher is started by _apply_ida_theme() if the active
        # mode is AUTO or IDA_NATIVE (only those modes read QPalette).
        # DARK/LIGHT modes ship bundled tokens and never poll the host,
        # so spinning the watcher would be pure overhead.
        self._theme_watcher: Any = None
        self._form_widget: QWidget | None = None
        self._root: QWidget | None = None
        self._core: LucNhanPanelCore | None = None
        _early_log("ida_panel:init:done")

    def OnCreate(self, form: Any) -> None:
        _early_log("ida_panel:on_create:entry")
        t_oncreate = start("ida_form.on_create_total")

        try:
            t_widget = start("ida_form.to_qt_widget")
            self._form_widget = _form_to_qt_widget(type(self), form)
            end("ida_form.to_qt_widget", t_widget)
            _early_log("ida_panel:on_create:to_qt_widget_done")
        except Exception as _exc:
            _early_log_crash_safe(_exc)
            raise

        self._root = QWidget()
        form_layout = QVBoxLayout(self._form_widget)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.addWidget(self._root)

        root_layout = QVBoxLayout(self._root)
        root_layout.setContentsMargins(0, 0, 0, 0)

        # Create the core panel
        try:
            t_core = start("ida_form.core_construct")
            self._core = LucNhanPanelCore(
                controller_factory=IdaSessionController,
                ui_hooks_factory=lambda panel_getter: LucNhanUIHooks(panel_getter=panel_getter),
                tools_form_factory=lambda tools_widget: LucNhanToolsForm(tools_widget),
                parent=self._root,
            )
            end("ida_form.core_construct", t_core)
            _early_log("ida_panel:on_create:core_constructed")
        except Exception as _exc:
            _early_log_crash_safe(_exc)
            raise

        root_layout.addWidget(self._core)

        # Apply IDA theme-aware stylesheet
        try:
            t_theme = start("ida_form.apply_theme")
            self._apply_ida_theme()
            end("ida_form.apply_theme", t_theme)
        except Exception as _exc:
            _early_log_crash_safe(_exc)
            raise

        # Apply custom font if configured
        try:
            self._apply_font_override()
        except Exception as _exc:
            _early_log_crash_safe(_exc)
            raise

        end("ida_form.on_create_total", t_oncreate)
        _early_log("ida_panel:on_create:done")

    def _apply_ida_theme(self) -> None:
        """Align the panel with the user's theme setting.

        ``dark`` / ``light`` hand the panel to the bundled Luc Nhan
        palettes.  ``ida`` / ``auto`` keep the host palette: the manager
        stays in its host-inherit mode and derives tokens from the
        *effective* widget palette (the panel registers itself as the
        palette source), so the plugin tracks IDA instead of the
        light ``QApplication`` default.

        The wrapper paints no colors of its own: the transcript surface
        and all other chrome belong to the single token pipeline in
        ``panel_core``, and a wrapper stylesheet would cascade onto — and
        overwrite — those widgets.  The user's font is applied to this
        form's own root widget (see :meth:`_apply_font_override`).
        """
        from lucnhan.ui.markdown import clear_code_block_theme, set_code_block_theme

        config_theme = getattr(self._core, "_config", None)
        if config_theme is not None:
            config_theme = config_theme.theme

        if config_theme in ("dark", "light"):
            self._core.set_theme(config_theme)
            clear_code_block_theme()
            return

        # ``config_theme`` is "ida" or "auto" (or an unknown value, which
        # the manager normalizes).  Derive the live IDA palette either
        # way so we can pick the right helper palette for inline-styled
        # widgets (action buttons, mode bar, history nav, etc.).
        c = _get_ida_theme_colors()
        is_dark = bool(c.get("is_dark", False))
        # ``set_code_block_theme`` is IDA's own API (no QSS sub-control
        # for the syntax-highlighted code blocks), so it stays — the values
        # come from the same IDA palette the helper widgets use.
        set_code_block_theme(
            bg=_rgb_to_hex(c["code_block_bg"]),
            border=_rgb_to_hex(c["code_block_border"]),
            text=_rgb_to_hex(c["code_text"]),
        )

        # The effective theme only feeds the *legacy* ``styles`` helpers
        # used by ``is_dark_theme()`` / ``is_host_theme()``-aware getters;
        # the manager's tokens derive their own value from the host.
        self._core.set_theme(
            "auto" if config_theme == "auto" else "ida",
            effective_theme="dark" if is_dark else "light",
        )

        # Spin up the IDAThemeWatcher for live palette tracking. Only
        # AUTO and IDA_NATIVE read QPalette tokens, so the watcher is
        # a no-op overhead in DARK/LIGHT modes — see the gate in
        # tests/tools/test_theme_watcher.py::TestPluginWatcherGate.
        self._maybe_start_theme_watcher(config_theme)

    def _maybe_start_theme_watcher(self, config_theme: str) -> None:
        """Start IDAThemeWatcher when the active theme mode reads QPalette.

        AUTO and IDA_NATIVE derive tokens from the independent host
        palette probe (see ``ThemeManager._host_probe_widget``), so the
        watcher is required for them to track user-driven IDA theme
        switches. DARK and LIGHT return bundled constants and never touch
        the palette, so starting the watcher there is pure overhead (and
        risks spurious refresh_from_host calls).
        """
        if self._theme_watcher is not None:
            return  # already started
        if config_theme not in ("auto", "ida"):
            return  # bundled-constant mode — no need to poll
        try:
            from lucnhan.ui.theme.manager import ThemeManager
            from lucnhan.ui.theme.tokens import ThemeMode
            from lucnhan.ui.theme.watcher import IDAThemeWatcher

            manager = ThemeManager.instance()
            # The manager picks the effective mode; only start if the
            # resolved mode is one that needs palette polling. This
            # handles the case where config says "auto" but the manager
            # has been set to DARK by a user override.
            current_mode = manager.mode
            if current_mode not in (ThemeMode.AUTO, ThemeMode.IDA_NATIVE):
                return
            self._theme_watcher = IDAThemeWatcher(interval_ms=500)
            self._theme_watcher.start()
        except Exception as e:
            # Watcher is best-effort — never block panel creation on it.
            try:
                from lucnhan.core.logging import log_debug

                log_debug(f"ThemeWatcher init failed: {type(e).__name__}: {e}")
            except Exception:
                pass
            self._theme_watcher = None

    def _apply_font_override(self) -> None:
        """Apply the user's font settings.

        Two effects, both needed.  The configured point size goes to the
        ThemeManager, which the typography role builders read and which
        repaints through ``themeChanged``.  The family goes onto this
        wrapper's ``_root`` and onto the panel core via ``QFont``, plus a
        single family rule on ``_root``'s own stylesheet so descendants that
        carry their own sheet still take it (a widget stylesheet does not
        inherit the parent's ``setFont`` family — measured).

        ``_core``'s stylesheet is never touched: the core owns every color
        rule, and writing to it is what wiped them before.  The family name
        is validated against the installed families first, so a configured
        value cannot smuggle QSS into the wrapper's sheet.

        Clearing the family restores the inherited host font rather than
        freezing the previous override.  Reading the *live* config
        matters: Settings writes the new value into it.
        """
        config = getattr(self._core, "_config", None)
        if config is None:
            try:
                from lucnhan.core.logging import log_debug

                log_debug("Font: config is None, skipping override")
            except Exception:
                pass
            return
        try:
            from lucnhan.ui.theme.manager import ThemeManager

            ThemeManager.instance().set_font_size_override(getattr(config, "font_size_override", 0) or 0)
        except Exception:
            pass
        if self._root is None:
            return
        try:
            from lucnhan.ui.qt_compat import QFont

            family = getattr(config, "font_family", "") or ""
            # A widget whose own stylesheet sets a size or a color does not
            # take its family from the parent's ``setFont`` (measured: the
            # status label stayed on the host family while root and core
            # took the override).  An ancestor stylesheet rule *does* merge
            # with a descendant's own sheet, so the family belongs there.
            # The name is validated against the installed families first:
            # an unknown or injected string is simply not applied.
            canonical = _installed_font_family(family)
            self._root.setStyleSheet(f'*{{ font-family: "{canonical}"; }}' if canonical else "")
            font = QFont()
            if canonical:
                font.setFamily(canonical)
            for widget in (self._root, self._core):
                if widget is not None:
                    widget.setFont(font)
        except Exception as exc:  # pragma: no cover — defensive
            _early_log_crash_safe(exc)
            return
        try:
            from lucnhan.core.logging import log_debug

            log_debug(f"Font: applied to the form root: {getattr(config, 'font_family', '') or '<inherit>'}")
        except Exception:
            pass

    def OnClose(self, form):
        self.shutdown()
        if self._root is not None:
            self._root.setParent(None)
            self._root.deleteLater()
            self._root = None

    def show(self):
        return self.Show(
            "Luc Nhan",
            options=(idaapi.PluginForm.WOPN_TAB | idaapi.PluginForm.WOPN_PERSIST),
        )

    def close(self):
        self.Close(0)

    def shutdown(self) -> None:
        # Stop the theme watcher first so it can't enqueue more refreshes
        # while the panel widgets are being torn down. Use getattr with
        # a default because tests build mock panels that skip __init__
        # (so the attribute may not exist on the instance).
        _watcher = getattr(self, "_theme_watcher", None)
        if _watcher is not None:
            try:
                _watcher.stop()
            except Exception as exc:
                _log_teardown("theme watcher stop", exc)
            self._theme_watcher = None
        # The panel's own theme subscription lives on ``_core`` (it owns
        # every color stylesheet), and ``shutdown`` there disconnects it —
        # so this wrapper has no slot left to unsubscribe.
        if self._core is not None:
            self._core.shutdown()
            self._core.setParent(None)
            self._core.deleteLater()
            self._core = None

    def prefill_input(self, text: str, auto_submit: bool = False) -> None:
        if self._core is not None:
            self._core.prefill_input(text, auto_submit=auto_submit)

    def on_database_changed(self, new_path: str) -> None:
        if self._core is not None:
            self._core.on_database_changed(new_path)

    def __getattr__(self, name: str):
        # Forward UI action accessors like _input_area / _on_submit.
        core = object.__getattribute__(self, "_core")
        if core is not None and hasattr(core, name):
            return getattr(core, name)
        raise AttributeError(name)
