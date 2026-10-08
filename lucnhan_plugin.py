"""Luc Nhan - Intelligent Reverse-engineering Integrated System.

IDA Pro plugin entry point.
All lucnhan.* imports are deferred to avoid crashes during plugin enumeration.

Startup path:
  IDA load → LucNhanPlugin.init() [imports constants only]
  → user activates → LucNhanPlugmod._toggle_panel()
    → minimal import of lucnhan.ida.ui.panel
    → LucNhanPanel() → show()
  Old behaviour (disabled): recursive full-package preload.
  Restore old path: set LUCNHAN_PRELOAD_ALL=1.
"""

import builtins
import importlib
import os
import threading

# Import the stdlib-only early-startup log first so that any failure in
# ``import idaapi`` (or anything below) is captured in
# ``~/.idapro/lucnhan/early_startup.log``. If the import itself fails the
# surrounding try/except falls back to a raw ``open(...)`` write so we
# never lose the diagnostic sink.
try:
    from lucnhan.core import early_log as _el
except Exception as _el_exc:  # pragma: no cover — defensive
    try:
        import sys as _sys

        _fallback_dir = os.path.join(os.path.expanduser("~"), ".idapro", "lucnhan")
        os.makedirs(_fallback_dir, exist_ok=True)
        with open(os.path.join(_fallback_dir, "early_startup.log"), "a", encoding="utf-8") as _fallback_fh:
            _fallback_fh.write(
                f"[CRITICAL] lucnhan.core.early_log import failed: {type(_el_exc).__name__}: {_el_exc}\n"
            )
    except Exception:
        pass
    _sys.stderr.write(f"[Lục nhãn] early_log import failed: {_el_exc}\n")
    _el = None  # type: ignore[assignment]

import idaapi

# ---------------------------------------------------------------------------
# Shiboken __import__ hook re-entrancy guard
# ---------------------------------------------------------------------------
# PySide6/Shiboken6 patches builtins.__import__ with a hook.  When this
# hook is invoked during Qt signal dispatch (e.g. submit_requested.emit()),
# and the connected slot's code triggers an import, the hook re-enters
# itself.  After 3-4 levels of nesting the hook accesses freed memory
# (UAF → SIGSEGV in ___lldb_unnamed_symbol945, address looks like ASCII
# string fragment — type-name pointer corruption).
#
# Fix: wrap the hook so that first-level calls go through Shiboken
# normally (preserving PySide6 module wrapping), but nested calls
# (re-entrant) are redirected to CPython's standard import, avoiding
# the corruption.  Installed once and never removed.

_import_guard = threading.local()
_shiboken_import = builtins.__import__


def _guarded_import(*args, **kwargs):
    if getattr(_import_guard, "active", False):
        # Re-entrant call — bypass Shiboken's hook
        return importlib.__import__(*args, **kwargs)
    _import_guard.active = True
    try:
        return _shiboken_import(*args, **kwargs)
    finally:
        _import_guard.active = False


_guarded_import._lucnhan_guarded = True  # marker to avoid double-wrapping


def _ensure_import_guard() -> None:
    """Install the Shiboken import guard if it is not already active.

    PySide6/Shiboken6 may replace ``builtins.__import__`` with its own
    hook *after* plugin load. Without re-installing our guard, the
    re-entrancy protection would be silently lost. Call this before any
    risky import (e.g. before importing the panel module) and again
    whenever the surrounding code observes a Shiboken restart.

    The bypass target (``_shiboken_import``) is pinned ONCE at module
    load to the unmodified CPython ``__import__``. We never reassign it
    inside this function — if we did, a Shiboken restart would overwrite
    the bypass target with the very hook we are trying to bypass, and
    the guard would silently become a no-op.
    """
    if _el is not None:
        _el._early_log("guard:pre_install")
    current = builtins.__import__
    if getattr(current, "_lucnhan_guarded", False):
        if _el is not None:
            _el._early_log("guard:already_active")
        return
    builtins.__import__ = _guarded_import
    if _el is not None:
        _el._early_log("guard:post_install", level="DEBUG")


_ensure_import_guard()
if _el is not None:
    _el._early_log("module_import:complete")


class LucNhanPlugmod(idaapi.plugmod_t):
    """Per-database plugin module."""

    def __init__(self):
        super().__init__()
        self._panel = None

    def run(self, arg: int) -> bool:
        if _el is not None:
            _el._early_log("plugmod_run:entry")
        _ensure_import_guard()
        self._toggle_panel()
        return True

    def term(self) -> None:
        _log("LucNhanPlugmod.term() called")
        panel = self._panel
        self._panel = None
        if panel is not None:
            try:
                panel.close()
            except Exception as e:
                idaapi.msg(f"[Lục nhãn] Panel close error: {e}\n")
        # Flush deferred widget deletions while Python is still alive.
        # Without this, orphaned Qt-wrapped QFrames survive until
        # QApplication::~QApplication() where their C++ destructors call
        # disconnectNotify -> PyErr_Occurred on a dead interpreter -> crash.
        try:
            from lucnhan.ui.qt_compat import QApplication

            QApplication.processEvents()
        except Exception as exc:
            import sys

            sys.stderr.write(f"[Lục nhãn] QApplication.processEvents failed: {exc}\n")

    def _toggle_panel(self) -> None:
        # Reinstall the Shiboken import guard before any panel import —
        # PySide6/Shiboken6 may have replaced builtins.__import__ since
        # plugin load, and a bare ``from lucnhan.… import …`` without
        # protection would re-enter the broken hook.
        if _el is not None:
            _el._early_log("toggle_panel:entry")
        _ensure_import_guard()
        # Lazily import startup_timing within toggle_panel so that the
        # plugin module itself does NOT import lucnhan.* at load time.
        from lucnhan.core.startup_timing import (
            complete as _complete_timing,
        )
        from lucnhan.core.startup_timing import (
            end as _end_phase,
        )
        from lucnhan.core.startup_timing import (
            flush as _flush_timing,
        )
        from lucnhan.core.startup_timing import (
            start as _start_phase,
        )

        t_toggle = _start_phase("toggle.total")
        try:
            _log("_toggle_panel: entry")
            if self._panel is not None:
                _log("_toggle_panel: panel exists, calling show()")
                self._panel.show()
                return

            # Minimal import path: load only the lucnhan.ida.ui.panel module.
            # The old behaviour (recursive full-package preload via
            # pkgutil.iter_modules) is available via LUCNHAN_PRELOAD_ALL=1.
            _PRELOAD_ALL = os.environ.get("LUCNHAN_PRELOAD_ALL", "") in ("1", "yes", "true")

            if _PRELOAD_ALL:
                _log("_toggle_panel: LUCNHAN_PRELOAD_ALL=1 — performing full recursive import")

                t_bulk = _start_phase("toggle.bulk_import_all")
                import pkgutil

                import lucnhan

                _imported = 0
                _skipped = 0

                def _load_submodules(pkg):
                    nonlocal _imported, _skipped
                    for _finder, modname, ispkg in pkgutil.iter_modules(pkg.__path__, prefix=pkg.__name__ + "."):
                        try:
                            mod = importlib.import_module(modname)
                            _imported += 1
                            if ispkg:
                                _load_submodules(mod)
                        except Exception as e:
                            _skipped += 1
                            import sys

                            sys.stderr.write(f"[Lục nhãn] Skipping {modname}: {e}\n")

                saved_import = builtins.__import__
                builtins.__import__ = importlib.__import__
                try:
                    _load_submodules(lucnhan)
                finally:
                    builtins.__import__ = saved_import
                _end_phase("toggle.bulk_import_all", t_bulk, meta={"imported": _imported, "skipped": _skipped})
                _log("_toggle_panel: all lucnhan modules loaded")
            else:
                _log("_toggle_panel: using minimal import (set LUCNHAN_PRELOAD_ALL=1 for legacy full preload)")

            _log("_toggle_panel: importing lucnhan.ida.ui.panel")
            t_import_panel = _start_phase("toggle.import_panel_module")
            LucNhanPanel = importlib.import_module("lucnhan.ida.ui.panel").LucNhanPanel
            if _el is not None:
                _el._early_log("toggle_panel:panel_module_imported")
            _end_phase("toggle.import_panel_module", t_import_panel)

            _log("_toggle_panel: creating LucNhanPanel()")
            t_construct = _start_phase("toggle.panel_construct")
            self._panel = LucNhanPanel()
            if _el is not None:
                _el._early_log("toggle_panel:panel_constructed")
            _end_phase("toggle.panel_construct", t_construct)

            _log("_toggle_panel: calling show()")
            t_show = _start_phase("toggle.panel_show")
            self._panel.show()
            if _el is not None:
                _el._early_log("toggle_panel:panel_shown")
            _end_phase("toggle.panel_show", t_show)

            if _el is not None:
                _el._early_log("toggle_panel:done")
            _log("_toggle_panel: done")
            _end_phase("toggle.total", t_toggle)

            # Mark startup session complete, then flush timing records
            # to the debug log now that the logging subsystem is available.
            try:
                _complete_timing()
                _flush_timing()
            except Exception as exc:
                import sys

                sys.stderr.write(f"[Lục nhãn] timing flush failed: {exc}\n")
        except Exception as e:
            import sys
            import traceback

            # Flush the early-startup ring buffer + a formatted traceback
            # to ``early_startup_crash.log`` BEFORE the existing IDA/log
            # fallback runs. The existing fallback chain is preserved.
            if _el is not None:
                _el._early_log_crash(e)

            tb_str = traceback.format_exc()
            idaapi.msg(f"[Lục nhãn] Failed to open panel: {e}\n{tb_str}\n")
            try:
                importlib.import_module("lucnhan.core.logging").log_error(f"Failed to open panel: {e}\n{tb_str}")
            except Exception:
                try:
                    log_path = os.path.join(os.path.expanduser("~"), ".idapro", "lucnhan", "lucnhan_debug.log")
                    with open(log_path, "a") as f:
                        f.write(f"[Lục nhãn CRASH] {e}\n{tb_str}\n")
                        f.flush()
                        os.fsync(f.fileno())
                except Exception:
                    print(f"[Lục nhãn CRASH] {e}\n{tb_str}", file=sys.stderr)


class LucNhanPlugin(idaapi.plugin_t):
    flags = idaapi.PLUGIN_MULTI | idaapi.PLUGIN_FIX
    comment = "Intelligent Reverse-engineering Integrated System"
    help = ""
    wanted_name = "Luc Nhan"
    wanted_hotkey = "Ctrl+Shift+I"

    def init(self) -> idaapi.plugmod_t:
        if _el is not None:
            _el._early_log("plugin_init:entry")
        _ver = importlib.import_module("lucnhan.constants").PLUGIN_VERSION
        idaapi.msg(f"[Lục nhãn] Plugin loaded (v{_ver})\n")
        return LucNhanPlugmod()


def _log(msg: str) -> None:
    """Best-effort log to the debug file via ``log_trace``.

    Routine bootstrap trace messages are routed through the standard
    logging subsystem (debug file + JSONL); they do **not** echo into
    IDA's Output window.  Set ``LUCNHAN_BOOTSTRAP_VERBOSE=1`` to force
    every bootstrap call to also write to the Output window — useful
    when debugging plugin-load failures.

    Caches the ``log_trace`` callable after the first successful import so
    that repeated ``importlib`` calls are avoided during the tight bootstrap
    path.  A single transient import failure does **not** permanently
    suppress further attempts — the import is retried on every call until it
    succeeds.  Only a successful import is cached.
    """
    verbose = os.environ.get("LUCNHAN_BOOTSTRAP_VERBOSE", "") in ("1", "yes", "true")
    if verbose:
        try:
            idaapi.msg(f"[Lục nhãn] {msg}\n")
        except Exception:
            pass
    cached = getattr(_log, "_cached_log_trace", None)
    if cached is not None:
        try:
            cached(msg)
        except Exception as e:
            import sys

            sys.stderr.write(f"[Lục nhãn] cached log_trace failed during bootstrap: {e}\n")
        return
    try:
        log_func = importlib.import_module("lucnhan.core.logging").log_trace
        _log._cached_log_trace = log_func
        log_func(msg)
    except Exception as e:
        import sys

        sys.stderr.write(f"[Lục nhãn] log_trace unavailable during bootstrap: {e}\n")


def PLUGIN_ENTRY():
    if _el is not None:
        _el._early_log("plugin_entry")
    return LucNhanPlugin()
