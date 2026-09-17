"""IDA PluginForm wrapper for the Rikugan Tools panel (dockable view)."""

from __future__ import annotations
import importlib
import importlib.util as _importlib_util
from typing import Any

from rikugan.ui.qt_compat import QTimer, QVBoxLayout, QWidget
from rikugan.ui.tools_panel import ToolsPanel

# Probe the IDA SDK presence cheaply with ``find_spec`` (does not run
# module code).  Mirrors ``actions._probe_ida`` so every IDA-host
# module shares the same import pattern — Shiboken UAF mitigation on
# Python ≥ 3.11 (see ``AGENTS.md`` §1).
_IDA_MODULES_TOOLS_FORM: tuple[str, ...] = ("idaapi",)


def _probe_ida_tools_form() -> bool:
    for _m in _IDA_MODULES_TOOLS_FORM:
        try:
            spec = _importlib_util.find_spec(_m)
        except (ValueError, ModuleNotFoundError):
            continue
        if spec is None:
            return False
    return True


_HAS_IDA_TOOLS_FORM = _probe_ida_tools_form()
_ida_tools_form_loaded = False


def _ensure_ida_tools_form() -> bool:
    """Import ``idaapi`` and bind it as a module attribute.

    Idempotent: subsequent calls are free.  Mirrors ``actions._ensure_ida``.
    Returns ``_HAS_IDA_TOOLS_FORM`` so callers can early-out when IDA is
    absent (e.g. during unit tests that stub the IDA SDK).
    """
    global _ida_tools_form_loaded
    if _ida_tools_form_loaded:
        return _HAS_IDA_TOOLS_FORM
    _ida_tools_form_loaded = True
    if not _HAS_IDA_TOOLS_FORM:
        return False
    import sys as _sys

    try:
        for _m in _IDA_MODULES_TOOLS_FORM:
            setattr(_sys.modules[__name__], _m, importlib.import_module(_m))
        return True
    except ImportError:
        return False


# Bind the name eagerly when IDA is present so the rest of this file
# (class bodies, type annotations) can refer to ``idaapi`` without going
# through ``__getattr__``.  When IDA is absent, the name remains unbound
# and the ``if _HAS_IDA_TOOLS_FORM:`` block below is skipped entirely.
if _ensure_ida_tools_form():
    idaapi = importlib.import_module("idaapi")  # type: ignore[assignment]


def _form_to_qt_widget(plugin_form_cls: Any, form: Any) -> Any:
    """Convert an IDA PluginForm handle to a Qt widget, robustly.

    Mirrors ``rikugan.ida.ui.panel._form_to_qt_widget``: try
    ``FormToPySideWidget`` first, fall back to ``FormToPyQtWidget`` on
    IDA 9.4 (whose ``TWidgetToPySideWidget`` is buggy). See panel.py
    for the full rationale.
    """
    try:
        return plugin_form_cls.FormToPySideWidget(form)
    except (AttributeError, TypeError):
        return plugin_form_cls.FormToPyQtWidget(form)


def _widget_alive(w: QWidget) -> bool:
    """Return True if the underlying C++ QWidget has not been deleted."""
    try:
        from shiboken6 import isValid

        return isValid(w)
    except Exception:
        try:
            # Fallback: any attribute access on a dead wrapper raises
            w.objectName()
            return True
        except RuntimeError:
            return False


class RikuganToolsForm(idaapi.PluginForm):
    """IDA dockable form embedding the shared ToolsPanel widget."""

    def __init__(self, tools_widget: ToolsPanel):
        super().__init__()
        self._tools_widget = tools_widget
        self._form_widget: QWidget | None = None
        self._shown = False

    def OnCreate(self, form: Any) -> None:
        # IDA ≥ 9.0 ships PySide6 — see panel.py for the binding rationale.
        self._form_widget = _form_to_qt_widget(type(self), form)

        layout = QVBoxLayout(self._form_widget)
        layout.setContentsMargins(0, 0, 0, 0)

        if _widget_alive(self._tools_widget):
            self._tools_widget.hide_header()
            self._tools_widget.setVisible(True)
            layout.addWidget(self._tools_widget)
        else:
            # Widget was destroyed by a prior close — create a placeholder.
            # panel_core will re-set the real widget on next init.
            from rikugan.ui.qt_compat import QLabel

            placeholder = QLabel("Tools panel is reloading...")
            placeholder.setStyleSheet("color: #808080; padding: 20px;")
            layout.addWidget(placeholder)

        # IDA's docking system doesn't always trigger a paint after the
        # initial Show().  Schedule a deferred update so the form contents
        # become visible without requiring the user to switch away and back.
        QTimer.singleShot(50, self._force_update)

    def _force_update(self) -> None:
        """Force the form and tools widget to repaint after IDA docking."""
        if self._form_widget is not None:
            self._form_widget.update()
        if _widget_alive(self._tools_widget):
            self._tools_widget.update()
            self._tools_widget.updateGeometry()

    def OnClose(self, form: Any) -> None:
        # Reparent the tools widget out of the doomed form so it is not
        # destroyed together with the form's QWidget tree.
        if _widget_alive(self._tools_widget):
            self._tools_widget.setParent(None)
            self._tools_widget.setVisible(False)
        self._shown = False

    def show(self) -> None:
        if not self._shown:
            self.Show(
                "Luc Nhan Tools",
                options=(idaapi.PluginForm.WOPN_TAB | idaapi.PluginForm.WOPN_PERSIST),
            )
            self._shown = True
        else:
            widget = idaapi.find_widget("Luc Nhan Tools")
            if widget:
                idaapi.activate_widget(widget, True)

    def hide(self) -> None:
        widget = idaapi.find_widget("Luc Nhan Tools")
        if widget:
            idaapi.close_widget(widget, 0)
        self._shown = False

    @property
    def is_visible(self) -> bool:
        widget = idaapi.find_widget("Luc Nhan Tools")
        return widget is not None and self._shown

    def set_tab(self, index: int) -> None:
        if _widget_alive(self._tools_widget) and hasattr(self._tools_widget, "_tabs"):
            self._tools_widget._tabs.setCurrentIndex(index)
