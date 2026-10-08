"""Tests for lucnhan.ui.qt_compat — PySide6-only Qt surface."""

from __future__ import annotations

import unittest

from tests.qt_stubs import ensure_pyside6_stubs

ensure_pyside6_stubs()
import lucnhan.ui.qt_compat as qt_compat


class TestQtCompat(unittest.TestCase):
    def test_qt_core_symbols_exported(self):
        for name in ("Signal", "Qt", "QObject", "QTimer", "QEvent", "QThread"):
            self.assertTrue(hasattr(qt_compat, name), f"missing {name}")

    def test_qt_widget_symbols_exported(self):
        for name in (
            "QApplication",
            "QWidget",
            "QVBoxLayout",
            "QHBoxLayout",
            "QLabel",
            "QPushButton",
            "QPlainTextEdit",
            "QScrollArea",
            "QDialog",
            "QComboBox",
            "QLineEdit",
            "QCheckBox",
            "QMenu",
            "QMessageBox",
        ):
            self.assertTrue(hasattr(qt_compat, name), f"missing {name}")

    def test_qt_gui_symbols_exported(self):
        for name in ("QColor", "QFont", "QPalette", "QPainter", "QSyntaxHighlighter"):
            self.assertTrue(hasattr(qt_compat, name), f"missing {name}")

    def test_no_pyqt5_detection_symbols_remain(self):
        """PyQt5 detection is gone — these names must not be exported."""
        for name in ("QT_BINDING", "is_pyside6", "qt_flags", "qt_run", "_detect_binding"):
            self.assertFalse(
                hasattr(qt_compat, name),
                f"{name} should be removed (PySide6-only)",
            )

    def test_qt_compat_source_imports_only_pyside6(self):
        """Source-level check: qt_compat imports from PySide6, never PyQt5.

        Why source instead of ``inspect.getmodule()``:
        ``tests/qt_stubs.py`` builds Qt classes with ``type("QWidget", (), attrs)``
        inside the ``tests.qt_stubs`` module, so each stub class gets
        ``__module__ = "tests.qt_stubs"`` stamped at construction time. The stubs
        are then injected into ``sys.modules["PySide6.QtWidgets"].__dict__``, but
        that injection does NOT mutate the class's ``__module__`` attribute, so
        ``inspect.getmodule(obj)`` follows ``__module__`` and returns
        ``tests.qt_stubs`` — never ``PySide6.*``. The intent ("qt_compat must
        only pull symbols from PySide6") is therefore expressed more reliably
        by inspecting the source file directly. This check works identically
        under stubs and real PySide6.
        """
        import ast
        import inspect

        source_path = inspect.getsourcefile(qt_compat)
        self.assertIsNotNone(source_path, "could not locate qt_compat source")
        with open(source_path, encoding="utf-8") as fh:
            source = fh.read()

        # Assert on parsed imports, not raw substrings: a comment mentioning
        # the legacy PyQt5 shim is fine, an actual import is not.
        tree = ast.parse(source)
        imported_modules: set[str] = set()
        imported_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_modules.add(node.module)
                imported_names.update(alias.name for alias in node.names)

        self.assertTrue(
            any(mod == "PySide6" or mod.startswith("PySide6.") for mod in imported_modules),
            "qt_compat must import from PySide6",
        )
        self.assertFalse(
            any("PyQt5" in mod for mod in imported_modules | imported_names),
            "qt_compat must not import from PyQt5 (PySide6-only)",
        )


if __name__ == "__main__":
    unittest.main()
