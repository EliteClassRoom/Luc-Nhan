"""Regression test for IDA 9.1 QVBoxLayout/PySide6 crash.

Crash symptom (before fix):
    TypeError: arguments did not match any overloaded call:
      QVBoxLayout(QWidget): argument 1 has unexpected type 'PySide6.QtWidgets.QWidget'

Root cause: panel.OnCreate branched on QT_BINDING and could select
FormToPyQtWidget; on IDA 9.x that method returns a PySide6 widget
(since IDA's PyQt5 is a shim), mismatching a PyQt5 QVBoxLayout.

After Task 5 of the ``refactor/drop-pyqt5`` plan, the
``if QT_BINDING == "PyQt5"`` branch is gone: ``OnCreate`` calls
``FormToPySideWidget(form)`` unconditionally. Both tests in this file
pin that contract — even when ``_detect_binding`` wrongly returns
``"PyQt5"`` (e.g., another plugin pre-imported PyQt5), ``OnCreate``
must not reach for ``FormToPyQtWidget``.
"""

from __future__ import annotations

import importlib
import os
import sys
import types
import unittest
from unittest import mock
from unittest.mock import MagicMock

# Make ``tests`` importable and install the IDA + Qt stubs BEFORE any
# ``rikugan.ida.ui.*`` import. ``rikugan.ida.ui.panel`` pulls in
# ``idaapi`` (via ``tools_form``) at module load time; without these
# stubs the import raises ``ModuleNotFoundError`` outside IDA Pro.
# This mirrors the established pattern in ``tests/tools/test_ida_panel.py``.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from tests.qt_stubs import _qt_class, ensure_pyside6_stubs

ensure_pyside6_stubs()

# ``rikugan.ida.ui.panel`` also imports these siblings at load time; stub
# them so the module import succeeds without their heavy transitive deps.
_panel_core_mod = types.ModuleType("rikugan.ui.panel_core")
_panel_core_mod.RikuganPanelCore = _qt_class("RikuganPanelCore")
sys.modules.setdefault("rikugan.ui.panel_core", _panel_core_mod)

_session_mod = types.ModuleType("rikugan.ida.ui.session_controller")
_session_mod.IdaSessionController = MagicMock()
sys.modules.setdefault("rikugan.ida.ui.session_controller", _session_mod)

_actions_mod = types.ModuleType("rikugan.ida.ui.actions")
_actions_mod.RikuganUIHooks = MagicMock()
sys.modules.setdefault("rikugan.ida.ui.actions", _actions_mod)


def _run_oncreate_under_stubs(qt_binding: str) -> tuple[mock.MagicMock, mock.MagicMock]:
    """Run ``RikuganPanel.OnCreate`` under the standard stub setup.

    The ``qt_binding`` argument is preserved for documentation only —
    after the PyQt5 drop, ``OnCreate`` ignores the binding entirely and
    always uses ``FormToPySideWidget``. Each test can still drive the
    "PyQt5 detected" environment by passing ``qt_binding="PyQt5"`` to
    prove the contract holds even under hostile binding-detection.
    Returns ``(pyside_mock, pyqt_mock)`` so callers can assert which
    conversion method OnCreate selected.

    The function exits cleanly even if OnCreate raises partway through
    — the stub setup swallows theme/font work that depends on a real
    IDA runtime; we only care which ``FormTo*Widget`` was invoked.
    """
    del qt_binding  # post-Task-5: panel.py no longer reads QT_BINDING
    panel_mod = importlib.import_module("rikugan.ida.ui.panel")
    panel = panel_mod.RikuganPanel.__new__(panel_mod.RikuganPanel)

    with (
        mock.patch.object(panel, "FormToPySideWidget", create=True) as pyside,
        mock.patch.object(panel, "FormToPyQtWidget", create=True) as pyqt,
    ):
        # The idaapi.PluginForm base provides these as instance methods;
        # create=True lets us patch them even if the real base is stubbed.
        with (
            mock.patch.object(panel_mod.RikuganPanel, "FormToPySideWidget", pyside),
            mock.patch.object(panel_mod.RikuganPanel, "FormToPyQtWidget", pyqt),
        ):
            # OnCreate constructs QWidget/QVBoxLayout/RikuganPanelCore —
            # stub them via qt_compat so no real Qt is needed.
            from rikugan.ui import qt_compat

            with (
                mock.patch.object(qt_compat, "QWidget", return_value=mock.MagicMock()),
                mock.patch.object(qt_compat, "QVBoxLayout", return_value=mock.MagicMock()),
            ):
                # RikuganPanelCore.__init__ is heavy; short-circuit it.
                with mock.patch("rikugan.ui.panel_core.RikuganPanelCore") as core_cls:
                    core_cls.return_value = mock.MagicMock()
                    try:
                        panel.OnCreate(mock.sentinel.form)
                    except Exception:
                        # OnCreate may do theme work that fails without IDA;
                        # we only care about which Form method was selected.
                        pass

    return pyside, pyqt


class TestPanelOnCreatePySideOnly(unittest.TestCase):
    def test_uses_form_to_pyside_widget_only(self) -> None:
        """Default-path guard: OnCreate picks FormToPySideWidget under PySide6.

        After Task 5, OnCreate unconditionally calls ``FormToPySideWidget(form)``
        (the ``QT_BINDING`` branch is gone). This test pins that contract on
        the default binding.
        """
        pyside, pyqt = _run_oncreate_under_stubs(qt_binding="PySide6")

        pyside.assert_called()
        pyqt.assert_not_called()

    def test_path_a_never_dispatches_to_pyqt_regardless_of_binding(self) -> None:
        """QT_BINDING == "PyQt5" path must never pick FormToPyQtWidget after the drop.

        After the PyQt5 drop, OnCreate must never call FormToPyQtWidget,
        no matter what QT_BINDING resolves to. Previously this test pinned
        the buggy pre-Task-5 behavior (path (a) of the
        ``if QT_BINDING == "PyQt5"`` branch that called
        ``FormToPyQtWidget(form)``). Task 5 removed that branch entirely
        and unconditionally calls ``FormToPySideWidget(form)``; calling
        the old test name with flipped assertions pins the new contract.
        """
        pyside, pyqt = _run_oncreate_under_stubs(qt_binding="PyQt5")

        pyside.assert_called()
        pyqt.assert_not_called()


class TestFormToQtWidgetFallback(unittest.TestCase):
    """``_form_to_qt_widget`` falls back to FormToPyQtWidget on IDA 9.4.

    IDA 9.4's ``TWidgetToPySideWidget`` (backing ``FormToPySideWidget``)
    is buggy: it resolves ``ctx.QtGui.QWidget.FromCapsule(tw)`` which
    raises ``AttributeError``. ``_form_to_qt_widget`` must catch that
    and fall back to ``FormToPyQtWidget`` (which still returns a PySide6
    widget via IDA's shim). Because qt_compat is PySide6-only, the
    widget returned via either path type-matches our PySide6 layouts.
    """

    def test_falls_back_when_pyside_raises_attribute_error(self) -> None:
        panel_mod = importlib.import_module("rikugan.ida.ui.panel")
        plugin_form_cls = panel_mod.idaapi.PluginForm
        with (
            mock.patch.object(
                plugin_form_cls,
                "FormToPySideWidget",
                staticmethod(lambda form: (_ for _ in ()).throw(AttributeError("simulated IDA 9.4 bug"))),
                create=True,
            ),
            mock.patch.object(
                plugin_form_cls,
                "FormToPyQtWidget",
                staticmethod(lambda form: "pyqt_widget"),
                create=True,
            ),
        ):
            result = panel_mod._form_to_qt_widget(plugin_form_cls, mock.sentinel.form)
        self.assertEqual(result, "pyqt_widget")

    def test_uses_pyside_when_it_succeeds(self) -> None:
        panel_mod = importlib.import_module("rikugan.ida.ui.panel")
        plugin_form_cls = panel_mod.idaapi.PluginForm
        with (
            mock.patch.object(
                plugin_form_cls,
                "FormToPySideWidget",
                staticmethod(lambda form: "pyside_widget"),
                create=True,
            ),
            mock.patch.object(
                plugin_form_cls,
                "FormToPyQtWidget",
                staticmethod(lambda form: "pyqt_widget_should_not_be_used"),
                create=True,
            ),
        ):
            result = panel_mod._form_to_qt_widget(plugin_form_cls, mock.sentinel.form)
        self.assertEqual(result, "pyside_widget")

    def test_falls_back_on_type_error_too(self) -> None:
        """TypeError (e.g. wrong overload) must also trigger fallback."""
        panel_mod = importlib.import_module("rikugan.ida.ui.panel")
        plugin_form_cls = panel_mod.idaapi.PluginForm
        with (
            mock.patch.object(
                plugin_form_cls,
                "FormToPySideWidget",
                staticmethod(lambda form: (_ for _ in ()).throw(TypeError("simulated type error"))),
                create=True,
            ),
            mock.patch.object(
                plugin_form_cls,
                "FormToPyQtWidget",
                staticmethod(lambda form: "pyqt_widget"),
                create=True,
            ),
        ):
            result = panel_mod._form_to_qt_widget(plugin_form_cls, mock.sentinel.form)
        self.assertEqual(result, "pyqt_widget")


if __name__ == "__main__":
    unittest.main()
