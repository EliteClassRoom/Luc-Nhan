"""IDA UI hooks and context menu integration.

Data-driven table of 9 context-menu actions under Luc Nhan/.
"""

from __future__ import annotations

import importlib
import importlib.util as _importlib_util
from collections.abc import Callable
from typing import Any

from ...core.logging import log_debug
from ...ui.action_handlers import (
    handle_annotate,
    handle_clean,
    handle_deobfuscate,
    handle_explain,
    handle_rename,
    handle_send_to,
    handle_suggest_types,
    handle_vuln_audit,
    handle_xref_analysis,
)

# Probe the IDA SDK presence cheaply with ``find_spec`` (does not run
# module code) and lazy-import the real modules on first use.  This
# defers the cost of importing ``idaapi`` etc. from the panel-load
# path to the moment the user actually invokes a context-menu action.
# Some test mock modules (e.g. ``MagicMock``) are missing ``__spec__``
# and would cause ``find_spec`` to raise ``ValueError``; treat that as
# "module is present" and let the actual import attempt below decide.

_IDA_MODULES: tuple[str, ...] = ("ida_funcs", "ida_kernwin", "ida_name", "idaapi", "idc")


def _probe_ida() -> bool:
    for _m in _IDA_MODULES:
        try:
            spec = _importlib_util.find_spec(_m)
        except (ValueError, ModuleNotFoundError):
            continue
        if spec is None:
            return False
    return True


_HAS_IDA = _probe_ida()
_ida_loaded = False


def _ensure_ida() -> bool:
    """Import the IDA SDK modules and bind them as module attributes.

    Idempotent: subsequent calls are free. Returns ``_HAS_IDA``.
    """
    global _ida_loaded
    if _ida_loaded:
        return _HAS_IDA
    _ida_loaded = True
    if not _HAS_IDA:
        return False
    try:
        import sys as _sys

        for _m in _IDA_MODULES:
            setattr(_sys.modules[__name__], _m, importlib.import_module(_m))
        return True
    except ImportError:
        return False


# Bind the names eagerly when IDA is present so the rest of this file
# (class bodies, type annotations) can refer to ``idaapi`` etc. without
# going through ``__getattr__``.  When IDA is absent, the names remain
# unbound and the ``if _HAS_IDA:`` block below is skipped entirely.
if _ensure_ida():
    ida_funcs = importlib.import_module("ida_funcs")  # type: ignore[assignment]
    ida_kernwin = importlib.import_module("ida_kernwin")
    ida_name = importlib.import_module("ida_name")
    idaapi = importlib.import_module("idaapi")
    idc = importlib.import_module("idc")


def _get_context() -> dict[str, Any]:
    """Extract context from the current IDA view.

    Returns dict with keys: ea, func_ea, func_name, selected_text.
    """
    ea = idc.get_screen_ea()
    ctx: dict[str, Any] = {
        "ea": ea,
        "func_ea": None,
        "func_name": None,
        "selected_text": "",
    }

    func = ida_funcs.get_func(ea)
    if func:
        ctx["func_ea"] = func.start_ea
        ctx["func_name"] = ida_name.get_name(func.start_ea)

    # Try to grab viewer selection
    viewer = ida_kernwin.get_current_viewer()
    if viewer:
        sel_ok, start, end = ida_kernwin.read_range_selection(viewer)
        if sel_ok:
            ctx["selected_text"] = f"0x{start:x}-0x{end:x}"

    return ctx


if _HAS_IDA:
    # ------------------------------------------------------------------
    # Action handler factory
    # ------------------------------------------------------------------

    class _LucNhanAction(idaapi.action_handler_t):
        """Generic context-menu action backed by a handler callback."""

        def __init__(
            self,
            panel_getter: Callable[[], Any],
            handler: Callable[[dict[str, Any]], str],
            auto_submit: bool = False,
        ):
            super().__init__()
            self._get_panel = panel_getter
            self._handler = handler
            self._auto_submit = auto_submit

        def activate(self, ctx) -> int:
            panel = self._get_panel()
            if panel is None:
                return 0
            context = _get_context()
            text = self._handler(context)
            if text:
                panel._input_area.setPlainText(text)
                if self._auto_submit:
                    panel._on_submit(text)
                else:
                    panel._input_area.setFocus()
            return 1

        def update(self, ctx) -> int:
            return idaapi.AST_ENABLE_ALWAYS

    class _OpenToolsAction(idaapi.action_handler_t):
        """Open the Luc Nhan Tools panel (lands on the Knowledge tab)."""

        def __init__(self, panel_getter: Callable[[], Any]):
            super().__init__()
            self._get_panel = panel_getter

        def activate(self, ctx) -> int:
            panel = self._get_panel()
            if panel is None:
                return 0
            panel.show_tools_panel()
            return 1

        def update(self, ctx) -> int:
            return idaapi.AST_ENABLE_ALWAYS

    # ------------------------------------------------------------------
    # Handler functions — shared handlers from ui.action_handlers,
    # with IDA-specific wrappers for microcode terminology.
    # ------------------------------------------------------------------

    _handle_send_to = handle_send_to
    _handle_explain = handle_explain
    _handle_rename = handle_rename
    _handle_vuln_audit = handle_vuln_audit
    _handle_suggest_types = handle_suggest_types
    _handle_annotate = handle_annotate
    _handle_xref_analysis = handle_xref_analysis

    def _handle_deobfuscate(ctx: dict[str, Any]) -> str:
        return handle_deobfuscate(ctx, optimizer_term="microcode")

    def _handle_clean_mcode(ctx: dict[str, Any]) -> str:
        return handle_clean(ctx, ir_term="microcode")

    # ------------------------------------------------------------------
    # Action definitions table
    # ------------------------------------------------------------------
    # (action_id, label, handler_fn, auto_submit, hotkey, tooltip, allowed_views)

    _ACTION_DEFS: list[tuple[str, str, Callable, bool, str, str, set[str]]] = [
        (
            "lucnhan:send_to",
            "Send to Luc Nhan",
            _handle_send_to,
            False,
            "Ctrl+Shift+A",
            "Send selection or address to Luc Nhan input",
            {"disasm", "pseudo"},
        ),
        (
            "lucnhan:explain",
            "Explain this",
            _handle_explain,
            True,
            "",
            "Explain the current function with Luc Nhan",
            {"disasm", "pseudo"},
        ),
        (
            "lucnhan:rename",
            "Rename with Luc Nhan",
            _handle_rename,
            True,
            "",
            "Analyze and rename the current function",
            {"disasm", "pseudo"},
        ),
        (
            "lucnhan:deobfuscate",
            "Deobfuscate with Luc Nhan",
            _handle_deobfuscate,
            True,
            "",
            "Deobfuscate the current function",
            {"disasm", "pseudo"},
        ),
        (
            "lucnhan:vuln_audit",
            "Find vulnerabilities",
            _handle_vuln_audit,
            True,
            "",
            "Audit the current function for security bugs",
            {"disasm", "pseudo"},
        ),
        (
            "lucnhan:suggest_types",
            "Suggest types",
            _handle_suggest_types,
            True,
            "",
            "Infer and apply types for the current function",
            {"disasm", "pseudo"},
        ),
        (
            "lucnhan:annotate",
            "Annotate function",
            _handle_annotate,
            True,
            "",
            "Add comments to the current function",
            {"pseudo"},
        ),
        (
            "lucnhan:clean_mcode",
            "Clean microcode",
            _handle_clean_mcode,
            True,
            "",
            "Clean the microcode for the current function",
            {"pseudo"},
        ),
        (
            "lucnhan:xref_analysis",
            "Xref analysis",
            _handle_xref_analysis,
            True,
            "",
            "Deep cross-reference analysis on the current function",
            {"disasm", "pseudo"},
        ),
    ]

    _WIDGET_TYPE_MAP = {
        idaapi.BWN_DISASM: "disasm",
        idaapi.BWN_PSEUDOCODE: "pseudo",
    }

    # ------------------------------------------------------------------
    # UI hooks
    # ------------------------------------------------------------------

    class LucNhanUIHooks(idaapi.UI_Hooks):
        """UI hooks for adding Luc Nhan to context menus."""

        def __init__(self, panel_getter: Callable[[], Any]):
            super().__init__()
            self._get_panel = panel_getter
            self._registered = False

        def hook(self) -> bool:
            self._register_actions()
            return super().hook()

        def ready_to_run(self) -> None:
            self._register_actions()

        def _register_actions(self) -> None:
            if self._registered:
                return

            for (
                action_id,
                label,
                handler_fn,
                auto_submit,
                hotkey,
                tooltip,
                _views,
            ) in _ACTION_DEFS:
                desc = idaapi.action_desc_t(
                    action_id,
                    label,
                    _LucNhanAction(self._get_panel, handler_fn, auto_submit),
                    hotkey,
                    tooltip,
                )
                idaapi.register_action(desc)

            # Register "Open Tools" action (menu + context menu)
            idaapi.register_action(
                idaapi.action_desc_t(
                    "lucnhan:open_tools",
                    "Open Tools",
                    _OpenToolsAction(self._get_panel),
                    "",
                    "Open the Luc Nhan Tools panel",
                )
            )
            idaapi.attach_action_to_menu(
                "Edit/Plugins/Luc Nhan/",
                "lucnhan:open_tools",
                idaapi.SETMENU_APP,
            )

            self._registered = True

        def finish_populating_widget_popup(self, widget, popup) -> None:
            widget_type = idaapi.get_widget_type(widget)
            view_key = _WIDGET_TYPE_MAP.get(widget_type)
            if view_key is None:
                return

            for action_id, _label, _handler, _auto, _hk, _tt, views in _ACTION_DEFS:
                if view_key in views:
                    idaapi.attach_action_to_popup(widget, popup, action_id, "Luc Nhan/")

            # Always attach "Open Tools" in disasm/pseudo views.
            # The Bulk Renamer action has been removed because the
            # Renamer tab is hidden from the visible Tools surface.
            if view_key in ("disasm", "pseudo"):
                idaapi.attach_action_to_popup(widget, popup, "lucnhan:open_tools", "Luc Nhan/")

        def database_inited(self, is_new_database: bool, idc_script: str) -> None:
            """Called when a database is opened or created."""
            panel = self._get_panel()
            if panel is None:
                return
            try:
                new_path = idaapi.get_path(idaapi.PATH_TYPE_IDB)
                if not new_path:
                    new_path = idaapi.get_input_file_path() or ""
                if new_path:
                    panel.on_database_changed(new_path)
                    log_debug(f"Database changed notification: {new_path}")
            except Exception as e:
                log_debug(f"database_inited hook error: {e}")

        def term(self) -> None:
            if self._registered:
                for action_id, *_ in _ACTION_DEFS:
                    idaapi.unregister_action(action_id)
                idaapi.unregister_action("lucnhan:open_tools")
                self._registered = False

else:

    class LucNhanUIHooks:
        """Stub when IDA is not available."""

        def __init__(self, *args, **kwargs):
            self._panel_getter = kwargs.get("panel_getter")

        def hook(self):
            return False

        def unhook(self):
            return False
