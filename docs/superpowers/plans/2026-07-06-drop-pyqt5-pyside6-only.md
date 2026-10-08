# Drop PyQt5 — PySide6-only Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the PyQt5 compatibility layer so Luc Nhan uses PySide6 (Qt6) exclusively, fixing the `QVBoxLayout(QWidget): argument 1 has unexpected type 'PySide6.QtWidgets.QWidget'` crash on IDA 9.1.

**Architecture:** Remove `_detect_binding()` and the PyQt5 fallback branch entirely. `qt_compat.py` becomes a thin PySide6 re-export module (kept as the single import surface for future binding swaps). Delete the now-dead `qt_flags()` / `qt_run()` helpers and inline their trivial PySide6 equivalents (`|`, `.exec()`). The IDA wrappers (`panel.py`, `tools_form.py`) call `FormToPySideWidget(form)` directly.

**Tech Stack:** Python 3.10+, PySide6 (Qt6), IDA Pro ≥ 9.0, pytest.

## Global Constraints

- **Minimum IDA version: 9.0** (PySide6-only). IDA 9.0 / 9.1 / 9.2 all ship PySide6 as the primary binding; their `PyQt5` module is a thin shim over PySide6, not a separate Qt5 binding.
- **Minimum Python: 3.10** (unchanged — matches CLAUDE.md "Python 3.10 is safest for IDA").
- **Breaking change.** Bump version `1.7.0` → `1.8.0` and document in CHANGELOG. Users on IDA 8.x or Qt5-only hosts must stay on `1.7.0`.
- **PySide6 is the only Qt binding.** No `if QT_BINDING == "PyQt5"` branches anywhere. No `_detect_binding`. No `try: FormToPyQtWidget except: FormToPySideWidget`.
- **`qt_compat.py` stays as a re-export layer** (Decision 2b): all call sites keep importing from `lucnhan.ui.qt_compat`, but the module imports PySide6 directly. This keeps a single seam if IDA 10 ever swaps bindings.
- **`qt_flags()` and `qt_run()` are deleted** (Decision 3). Replace with PySide6-native `|` and `.exec()`.
- **TDD.** Every code task writes/fails/passes a test first.
- **Frequent commits.** One commit per task, conventional-commit format, no Co-Authored-By trailer (attribution disabled globally).
- **Verify gate before each commit:** `python3 -m ruff format <file> && python3 -m ruff check <file> --fix && python3 -m pytest <test> -v`.

---

## File Structure

**Modify:**
- `lucnhan/ui/qt_compat.py` — Strip detection + PyQt5 branch; PySide6-only re-export. Delete `qt_flags`/`qt_run`.
- `lucnhan/ui/panel_core.py` — Remove `qt_flags`/`qt_run` imports + call sites (6 sites).
- `lucnhan/ui/message_widgets.py` — Remove `qt_flags` import + 2 call sites.
- `lucnhan/ui/tool_widgets.py` — Remove `qt_flags` import + 2 call sites.
- `lucnhan/ida/ui/panel.py` — Replace `FormToPyQtWidget`/`FormToPySideWidget` branch with direct `FormToPySideWidget`.
- `lucnhan/ida/ui/tools_form.py` — Same: direct `FormToPySideWidget`.
- `lucnhan/tests/conftest.py` — Remove `PyQt5` fallback import.
- `tests/test_qt_compat.py` — Rewrite: drop PyQt5 detection tests, add PySide6-only regression test.
- `tests/qt_stubs.py` — No code change, but verify stubs still align (PySide6-only).
- `CLAUDE.md` — Update "IDA 9.x API changes" note: state PySide6-only.
- `README.md` — Update Requirements: "IDA Pro 9.0+ (PySide6 / Qt6)".
- `AGENTS.md` — Update any Qt-binding guidance (if it references PyQt5).
- `CHANGELOG.md` — Add `1.8.0` entry.
- `lucnhan/constants.py` — Bump `PLUGIN_VERSION = "1.8.0"`.
- `pyproject.toml` — Bump `version = "1.8.0"`.
- `ida-plugin.json` — Bump `"version": "1.8.0"`.

**Create:**
- `tests/ida_ui/test_panel_onside_widget.py` — Regression test for the IDA 9.1 crash (asserts `OnCreate` calls `FormToPySideWidget`, never `FormToPyQtWidget`).

---

### Task 1: Regression test for IDA 9.1 panel crash

This test encodes the bug before we touch production code. It will FAIL on current `panel.py` (which still branches on `QT_BINDING`), then PASS after Task 5 simplifies `OnCreate`.

**Files:**
- Create: `tests/ida_ui/test_panel_onside_widget.py`

**Interfaces:**
- Produces: `TestPanelOnCreate::test_uses_form_to_pyside_widget_only` — asserts `LucNhanPanel.OnCreate` invokes `FormToPySideWidget` and never `FormToPyQtWidget`. Later tasks must keep this green.

- [ ] **Step 1: Write the failing test**

```python
"""Regression test for IDA 9.1 QVBoxLayout/PySide6 crash.

Crash symptom (before fix):
    TypeError: arguments did not match any overloaded call:
      QVBoxLayout(QWidget): argument 1 has unexpected type 'PySide6.QtWidgets.QWidget'

Root cause: panel.OnCreate branched on QT_BINDING and could select
FormToPyQtWidget; on IDA 9.x that method returns a PySide6 widget
(since IDA's PyQt5 is a shim), mismatching a PyQt5 QVBoxLayout.

This test pins the contract: OnCreate always uses FormToPySideWidget.
"""

from __future__ import annotations

import importlib
import unittest
from unittest import mock


class TestPanelOnCreatePySideOnly(unittest.TestCase):
    def test_uses_form_to_pyside_widget_only(self) -> None:
        panel_mod = importlib.import_module("lucnhan.ida.ui.panel")
        panel = panel_mod.LucNhanPanel.__new__(panel_mod.LucNhanPanel)

        with mock.patch.object(panel, "FormToPySideWidget", create=True) as pyside, \
             mock.patch.object(panel, "FormToPyQtWidget", create=True) as pyqt:
            # The idaapi.PluginForm base provides these as instance methods;
            # create=True lets us patch them even if the real base is stubbed.
            with mock.patch.object(panel_mod.LucNhanPanel, "FormToPySideWidget", pyside), \
                 mock.patch.object(panel_mod.LucNhanPanel, "FormToPyQtWidget", pyqt):
                # OnCreate constructs QWidget/QVBoxLayout/LucNhanPanelCore —
                # stub them via qt_compat so no real Qt is needed.
                from lucnhan.ui import qt_compat
                with mock.patch.object(qt_compat, "QWidget", return_value=mock.MagicMock()), \
                     mock.patch.object(qt_compat, "QVBoxLayout", return_value=mock.MagicMock()):
                    # LucNhanPanelCore.__init__ is heavy; short-circuit it.
                    with mock.patch("lucnhan.ui.panel_core.LucNhanPanelCore") as core_cls:
                        core_cls.return_value = mock.MagicMock()
                        try:
                            panel.OnCreate(mock.sentinel.form)
                        except Exception:
                            # OnCreate may do theme work that fails without IDA;
                            # we only care about which Form method was selected.
                            pass

        pyside.assert_called()
        pyqt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/ida_ui/test_panel_onside_widget.py -v`
Expected: FAIL — current `OnCreate` calls `FormToPyQtWidget` first (inside the `QT_BINDING == "PyQt5"` branch the test exercises when detection returns PyQt5) OR the branch structure makes the assertion ambiguous. If the test PASSES on `master` (because detection returns PySide6 in the test env), that is acceptable — the test still guards future regressions; proceed to Task 5 which removes the branch entirely.

- [ ] **Step 3: Commit (test only, no production change yet)**

```bash
git add tests/ida_ui/test_panel_onside_widget.py
git commit -m "test(ida-ui): pin OnCreate to FormToPySideWidget only

Regression guard for the IDA 9.1 QVBoxLayout/PySide6 crash. Before the
PyQt5 drop, OnCreate could select FormToPyQtWidget; on IDA 9.x that
returns a PySide6 widget (PyQt5 is a shim), mismatching a PyQt5
QVBoxLayout."
```

---

### Task 2: Strip PyQt5 detection from `qt_compat.py`

Reduce `qt_compat.py` to a PySide6 re-export module. Delete `_detect_binding`, `QT_BINDING`, `is_pyside6`, `qt_flags`, `qt_run`, and the entire `else` (PyQt5) branch.

**Files:**
- Modify: `lucnhan/ui/qt_compat.py` (full rewrite)

**Interfaces:**
- Removes: `QT_BINDING`, `is_pyside6`, `qt_flags`, `qt_run` — all call sites migrated in Tasks 3–4 before or during this task. **Order matters:** Tasks 3 and 4 (removing call sites) must land first OR in the same commit. This plan runs Task 2 *after* Tasks 3–4 to keep each commit green.
- Produces: `qt_compat` exporting only PySide6 names. `QT_BINDING` is gone — any remaining reference is a hard `ImportError` caught by tests.

- [ ] **Step 1: Reorder — do call-site cleanup first (Tasks 3, 4, 5, 6, 7, 8) THEN this task.**

This task is sequenced last (before docs/version) precisely so we never have a broken intermediate commit. See Tasks 3–8 first; return here once they are done.

- [ ] **Step 2: Rewrite `qt_compat.py`**

Replace the entire file with:

```python
"""Qt binding surface for Luc Nhan.

Luc Nhan targets IDA Pro ≥ 9.0, which ships PySide6 (Qt6) as its sole Qt
binding. (IDA 9.x exposes a ``PyQt5`` module, but it is a thin shim that
delegates to PySide6 — not a separate Qt5 binding.)

This module is the single import seam for Qt symbols across the package.
All call sites import from ``lucnhan.ui.qt_compat`` rather than from
``PySide6`` directly, so a future host binding swap (e.g. PySide7) only
requires editing this one file.

Previously this module also supported PyQt5 via runtime detection. That
detection was the root cause of the IDA 9.1 crash where another plugin
pre-imported PyQt5, tricking detection into selecting PyQt5 while the
host ran PySide6 — producing
``QVBoxLayout(QWidget): argument 1 has unexpected type
'PySide6.QtWidgets.QWidget'``. PyQt5 support has been removed entirely.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QIntValidator,
    QPainter,
    QPalette,
    QSyntaxHighlighter,
    QTextCharFormat,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
```

- [ ] **Step 3: Verify nothing imports the removed names**

Run:
```bash
grep -rn "qt_flags\|qt_run\|QT_BINDING\|is_pyside6" lucnhan/ tests/
```
Expected: no matches. (If any match, the corresponding task missed a call site — fix before committing.)

- [ ] **Step 4: Run the qt_compat test suite**

Run: `python3 -m pytest tests/test_qt_compat.py -v`
Expected: PASS (the rewritten test from Task 8).

- [ ] **Step 5: Commit**

```bash
git add lucnhan/ui/qt_compat.py
git commit -m "refactor(qt): drop PyQt5 detection — PySide6 only

Remove _detect_binding, QT_BINDING, is_pyside6, qt_flags, qt_run, and
the entire PyQt5 fallback branch. qt_compat is now a thin PySide6
re-export layer kept as the single import seam.

This is the root-cause fix for the IDA 9.1 QVBoxLayout/PySide6 crash:
another plugin pre-importing PyQt5 fooled detection into picking PyQt5
while the host ran PySide6. With detection gone, every Qt symbol comes
from PySide6 unconditionally."
```

---

### Task 3: Remove `qt_flags` / `qt_run` from `panel_core.py`

**Files:**
- Modify: `lucnhan/ui/panel_core.py:27-48` (imports), `:154`, `:306`, `:315`, `:790`, `:798`, `:1186`, `:1223`

**Interfaces:**
- Consumes: PySide6-native `|` (replaces `qt_flags`) and `.exec()` (replaces `qt_run`).

- [ ] **Step 1: Write the failing test**

Create `tests/ui/test_panel_core_no_qt_helpers.py`:

```python
"""panel_core must not use the deleted qt_flags/qt_run helpers."""

from __future__ import annotations

import pathlib
import unittest


_PANEL_CORE = pathlib.Path("lucnhan/ui/panel_core.py")


class TestPanelCoreNoQtHelpers(unittest.TestCase):
    def test_no_qt_flags_or_qt_run_references(self) -> None:
        source = _PANEL_CORE.read_text(encoding="utf-8")
        self.assertNotIn("qt_flags", source, "qt_flags must be inlined as `|`")
        self.assertNotIn("qt_run", source, "qt_run must be inlined as `.exec()`")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/ui/test_panel_core_no_qt_helpers.py -v`
Expected: FAIL — `qt_flags`/`qt_run` still referenced.

- [ ] **Step 3: Edit the import block**

In `lucnhan/ui/panel_core.py`, change lines 27–48 — remove `qt_flags` and `qt_run` from the import list. The import block ends at `QWidget,` (drop the trailing two names).

- [ ] **Step 4: Replace call sites**

For each `qt_run(x)` call: replace with `x.exec()`.
- Line 154: `action = menu.exec(self.mapToGlobal(pos))`
- Line 315: `if dlg.exec() != QDialog.DialogCode.Accepted:`
- Line 798: `if not dlg.exec():`
- Line 1186: `result = dlg.exec()`
- Line 1223: `dlg.exec()`

For each `qt_flags(A, B)` call: replace with `A | B`.
- Line 306–309:
  ```python
  buttons = QDialogButtonBox(
      QDialogButtonBox.StandardButton.Ok
      | QDialogButtonBox.StandardButton.Cancel,
  )
  ```
- Line 790–793:
  ```python
  buttons = QDialogButtonBox(
      QDialogButtonBox.StandardButton.Ok
      | QDialogButtonBox.StandardButton.Cancel
  )
  ```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/ui/test_panel_core_no_qt_helpers.py -v`
Expected: PASS.

- [ ] **Step 6: Run broader UI tests to catch regressions**

Run: `python3 -m pytest tests/ui/ -v -k "panel_core or settings or export"`
Expected: PASS (no behavioral change — `|` and `.exec()` are PySide6-native equivalents).

- [ ] **Step 7: Commit**

```bash
git add lucnhan/ui/panel_core.py tests/ui/test_panel_core_no_qt_helpers.py
git commit -m "refactor(ui): inline qt_flags/qt_run in panel_core

Replace qt_flags(A, B) with A | B and qt_run(x) with x.exec(). These
helpers existed only to paper over PyQt5 vs PySide6 enum/exec
differences; with PySide6-only they were dead indirection."
```

---

### Task 4: Remove `qt_flags` from `message_widgets.py` and `tool_widgets.py`

**Files:**
- Modify: `lucnhan/ui/message_widgets.py:13-27` (imports), `:343`, `:517`
- Modify: `lucnhan/ui/tool_widgets.py:12-27` (imports), `:567`, `:585`

**Interfaces:**
- Consumes: PySide6-native `|`.

- [ ] **Step 1: Write the failing test**

Create `tests/ui/test_no_qt_flags_helpers.py`:

```python
"""message_widgets and tool_widgets must not use qt_flags."""

from __future__ import annotations

import pathlib
import unittest


_FILES = [
    pathlib.Path("lucnhan/ui/message_widgets.py"),
    pathlib.Path("lucnhan/ui/tool_widgets.py"),
]


class TestNoQtFlagsHelpers(unittest.TestCase):
    def test_no_qt_flags_references(self) -> None:
        for f in _FILES:
            with self.subTest(file=str(f)):
                source = f.read_text(encoding="utf-8")
                self.assertNotIn(
                    "qt_flags",
                    source,
                    f"{f}: qt_flags must be inlined as `|`",
                )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/ui/test_no_qt_flags_helpers.py -v`
Expected: FAIL.

- [ ] **Step 3: Edit `message_widgets.py`**

Remove `qt_flags` from the import block (line ~25). Replace the two call sites (`:343`, `:517`):

```python
self._content.setTextInteractionFlags(
    Qt.TextInteractionFlag.TextSelectableByMouse
    | Qt.TextInteractionFlag.TextSelectableByKeyboard
)
```

- [ ] **Step 4: Edit `tool_widgets.py`**

Remove `qt_flags` from the import block (line ~27). Replace the two call sites (`:567`, `:585`) with the same `|` pattern.

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/ui/test_no_qt_flags_helpers.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add lucnhan/ui/message_widgets.py lucnhan/ui/tool_widgets.py tests/ui/test_no_qt_flags_helpers.py
git commit -m "refactor(ui): inline qt_flags in message/tool widgets

Replace qt_flags(A, B) with A | B. PySide6 enums support | natively;
the helper was PyQt5-compat dead code."
```

---

### Task 5: Simplify `panel.py` `OnCreate` to PySide6-only

**Files:**
- Modify: `lucnhan/ida/ui/panel.py:14` (import — drop `QT_BINDING`), `:191-197` (branch)

**Interfaces:**
- Produces: `LucNhanPanel.OnCreate` calls `FormToPySideWidget(form)` unconditionally. Pinned by Task 1's test.

- [ ] **Step 1: Edit the import**

Line 14:
```python
from lucnhan.ui.qt_compat import QApplication, QVBoxLayout, QWidget
```
(Drop `QT_BINDING`.)

- [ ] **Step 2: Replace the branch**

Lines 191–197 currently:
```python
if QT_BINDING == "PyQt5":
    self._form_widget = self.FormToPyQtWidget(form)
else:
    try:
        self._form_widget = self.FormToPySideWidget(form)
    except Exception:
        self._form_widget = self.FormToPyQtWidget(form)
```

Replace with:
```python
# IDA ≥ 9.0 ships PySide6; FormToPySideWidget returns the host widget.
# (IDA 9.x also exposes FormToPyQtWidget, but it returns the same
# PySide6 widget via a shim — mixing it with PyQt5-imported layouts was
# the root cause of the QVBoxLayout/PySide6 type-mismatch crash.)
self._form_widget = self.FormToPySideWidget(form)
```

- [ ] **Step 3: Update Task 1's characterization test (assertion flip)**

Task 1 added two tests in `tests/ida_ui/test_panel_onside_widget.py`. The second, `test_path_a_dispatches_to_pyqt_under_buggy_branch`, is a **characterization test** that asserted `FormToPyQtWidget` IS called under `QT_BINDING=="PyQt5"` — documenting the pre-Task-5 buggy behavior. Now that the `if QT_BINDING == "PyQt5"` branch is gone, `FormToPyQtWidget` is never called regardless of `QT_BINDING`. **Flip that test's assertion**: rename it to `test_path_a_never_dispatches_to_pyqt_regardless_of_binding` and assert `FormToPyQtWidget` is NOT called (and `FormToPySideWidget` IS called) even when `QT_BINDING` is monkey-patched to `"PyQt5"`. Update its docstring to reflect the new contract: "After the PyQt5 drop, OnCreate must never call FormToPyQtWidget, no matter what QT_BINDING resolves to." Keep the `_run_oncreate_under_stubs("PyQt5")` helper call as-is.

- [ ] **Step 4: Run Task 1's regression test**

Run: `python3 -m pytest tests/ida_ui/test_panel_onside_widget.py -v`
Expected: PASS — both tests green (the flipped one now asserts the post-Task-5 correct behavior).

- [ ] **Step 4: Commit**

```bash
git add lucnhan/ida/ui/panel.py
git commit -m "refactor(ida-ui): OnCreate uses FormToPySideWidget only

Drop the QT_BINDING branch. On IDA 9.x FormToPyQtWidget returns a
PySide6 widget anyway (PyQt5 is a shim), so the branch was both dead
code and the crash trigger when detection picked PyQt5."
```

---

### Task 6: Simplify `tools_form.py` `OnCreate` to PySide6-only

**Files:**
- Modify: `lucnhan/ida/ui/tools_form.py:39-42`

**Interfaces:**
- Produces: `LucNhanToolsForm.OnCreate` calls `FormToPySideWidget(form)` unconditionally.

- [ ] **Step 1: Replace the try/except branch**

Lines 39–42 currently:
```python
try:
    self._form_widget = self.FormToPyQtWidget(form)
except Exception:
    self._form_widget = self.FormToPySideWidget(form)
```

Replace with:
```python
# IDA ≥ 9.0 ships PySide6 — see panel.py for the binding rationale.
self._form_widget = self.FormToPySideWidget(form)
```

- [ ] **Step 2: Run any existing tools_form tests (or the full UI suite)**

Run: `python3 -m pytest tests/ -v -k "tools_form or tools_panel"`
Expected: PASS (or no tests collected — then run the broader `tests/ui/`).

- [ ] **Step 3: Commit**

```bash
git add lucnhan/ida/ui/tools_form.py
git commit -m "refactor(ida-ui): tools_form OnCreate uses FormToPySideWidget only"
```

---

### Task 7: Remove PyQt5 fallback from `tests/conftest.py`

**Files:**
- Modify: `lucnhan/tests/conftest.py:10-17`

- [ ] **Step 1: Simplify the import**

Lines 10–17 currently:
```python
try:
    from lucnhan.ui.qt_compat import QApplication
except ModuleNotFoundError:
    # Fallback: assume PySide6 is available in the test environment
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        from PyQt5.QtWidgets import QApplication
```

Replace with:
```python
from lucnhan.ui.qt_compat import QApplication
```

(Luc Nhan is now PySide6-only; the fallback chain served the deleted detection logic.)

- [ ] **Step 2: Run a test that uses the qapp fixture**

Run: `python3 -m pytest tests/tools/test_settings_dialog.py -v`
Expected: PASS (this test exercises settings dialog widgets via qapp).

- [ ] **Step 3: Commit**

```bash
git add lucnhan/tests/conftest.py
git commit -m "refactor(tests): drop PyQt5 fallback in conftest qapp import"
```

---

### Task 8: Rewrite `tests/test_qt_compat.py` for PySide6-only

**Files:**
- Modify: `tests/test_qt_compat.py` (full rewrite)

- [ ] **Step 1: Rewrite the file**

Replace the entire file with:

```python
"""Tests for lucnhan.ui.qt_compat — PySide6-only Qt surface."""

from __future__ import annotations

import unittest

from tests.qt_stubs import ensure_pyside6_stubs

ensure_pyside6_stubs()
import lucnhan.ui.qt_compat as qt_compat  # noqa: E402


class TestQtCompat(unittest.TestCase):
    def test_qt_core_symbols_exported(self):
        for name in ("Signal", "Qt", "QObject", "QTimer", "QEvent", "QThread"):
            self.assertTrue(hasattr(qt_compat, name), f"missing {name}")

    def test_qt_widget_symbols_exported(self):
        for name in (
            "QApplication", "QWidget", "QVBoxLayout", "QHBoxLayout",
            "QLabel", "QPushButton", "QPlainTextEdit", "QScrollArea",
            "QDialog", "QComboBox", "QLineEdit", "QCheckBox",
            "QMenu", "QMessageBox",
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

    def test_all_symbols_come_from_pyside6(self):
        """Every Qt symbol qt_compat exports must originate in PySide6."""
        import inspect

        for name in ("QWidget", "QVBoxLayout", "QTimer", "Signal", "Qt"):
            obj = getattr(qt_compat, name)
            module = inspect.getmodule(obj)
            self.assertIsNotNone(module, f"{name} has no resolvable module")
            self.assertTrue(
                module.__name__.startswith("PySide6"),
                f"{name} should come from PySide6, got {module.__name__}",
            )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test (will FAIL until Task 2 lands)**

Run: `python3 -m pytest tests/test_qt_compat.py -v`
Expected: FAIL on `test_no_pyqt5_detection_symbols_remain` (because `qt_compat` still exports `QT_BINDING`/`qt_flags`/etc). This is correct — Task 2 closes the loop.

- [ ] **Step 3: Commit (test rewrite — will go green after Task 2)**

```bash
git add tests/test_qt_compat.py
git commit -m "test(qt): rewrite qt_compat tests for PySide6-only surface

Drop PyQt5 detection tests. Add assertions that QT_BINDING/is_pyside6/
qt_flags/qt_run are gone and that every exported symbol originates in
PySide6."
```

---

### Task 9: Update documentation

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`
- Modify: `AGENTS.md` (only if it references PyQt5 — verify first)

- [ ] **Step 1: Verify AGENTS.md references**

Run: `grep -n "PyQt5\|PyQt\b\|QT_BINDING\|qt_flags\|qt_run\|FormToPyQtWidget\|_detect_binding" AGENTS.md`
If no matches → skip AGENTS.md. If matches → edit those lines to remove PyQt5 guidance and state PySide6-only.

- [ ] **Step 2: Update README.md Requirements section**

Around line 102–109, change the Requirements block to explicitly state IDA ≥ 9.0 and PySide6. Example:

```markdown
## Requirements

- IDA Pro 9.0+ (ships PySide6 / Qt6; PyQt5 is not used)
- Python 3.10+
```

Keep the existing Shiboken UAF note about Python 3.14.

- [ ] **Step 3: Update CLAUDE.md**

In the "IDA 9.x API changes" section (or near the Shiboken UAF note), add a line:

```markdown
- **Qt binding: PySide6 only.** Luc Nhan targets IDA ≥ 9.0, which ships PySide6 (Qt6). The `PyQt5` module in IDA 9.x is a shim over PySide6 and is not used. `lucnhan/ui/qt_compat.py` is the single Qt import seam — import Qt symbols from there, not from `PySide6` directly.
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md README.md AGENTS.md
git commit -m "docs: document PySide6-only / IDA ≥ 9.0 requirement

State that PyQt5 is dropped and IDA 9.0+ is the minimum. qt_compat.py
is the single Qt import seam."
```

---

### Task 10: Bump version to 1.8.0 and CHANGELOG

**Files:**
- Modify: `lucnhan/constants.py:10`
- Modify: `pyproject.toml` (version field)
- Modify: `ida-plugin.json` (version field)
- Modify: `CHANGELOG.md` (add 1.8.0 entry at top)

- [ ] **Step 1: Bump the three version sources**

`lucnhan/constants.py:10`:
```python
PLUGIN_VERSION = "1.8.0"
```

`pyproject.toml` — find `version = "1.7.0"` and change to `version = "1.8.0"`.

`ida-plugin.json` — find `"version": "1.7.0"` and change to `"version": "1.8.0"`.

- [ ] **Step 2: Add CHANGELOG entry**

At the top of `CHANGELOG.md` (above `## [1.7.0]`), insert:

```markdown
## [1.8.0] — 2026-07-06

### Breaking
- **Dropped PyQt5 support.** Luc Nhan now uses PySide6 (Qt6) exclusively. Minimum IDA Pro version is **9.0** (all 9.x releases ship PySide6 as their primary binding; IDA 9.x's `PyQt5` module is a thin shim over PySide6 and is no longer used). Users on IDA 8.x or Qt5-only hosts must stay on `1.7.0`.

### Fixed
- IDA 9.1 crash: `QVBoxLayout(QWidget): argument 1 has unexpected type 'PySide6.QtWidgets.QWidget'`. Root cause was `_detect_binding()` in `lucnhan/ui/qt_compat.py` selecting PyQt5 when another plugin had pre-imported it into `sys.modules`, while the host actually ran PySide6. The entire detection layer is removed; Qt symbols now come from PySide6 unconditionally.

### Removed
- `lucnhan/ui/qt_compat.py`: `_detect_binding()`, `QT_BINDING`, `is_pyside6()`, `qt_flags()`, `qt_run()`, and the PyQt5 import branch.
- `lucnhan/ida/ui/panel.py` and `lucnhan/ida/ui/tools_form.py`: the `FormToPyQtWidget` / `FormToPySideWidget` try-except branch — `OnCreate` now calls `FormToPySideWidget(form)` directly.
- `lucnhan/tests/conftest.py`: PyQt5 fallback in the `qapp` fixture import.

### Changed
- `lucnhan/ui/qt_compat.py` is now a thin PySide6 re-export layer (kept as the single Qt import seam). Call sites that used `qt_flags(A, B)` now use `A | B`; `qt_run(x)` now uses `x.exec()`.
```

- [ ] **Step 3: Verify version sync (three sources agree)**

Run:
```bash
grep 'PLUGIN_VERSION' lucnhan/constants.py
grep '^version' pyproject.toml
grep '"version"' ida-plugin.json
```
Expected: all three show `1.8.0`.

- [ ] **Step 4: Commit**

```bash
git add lucnhan/constants.py pyproject.toml ida-plugin.json CHANGELOG.md
git commit -m "chore(release): bump version to 1.8.0

PyQt5 drop + IDA 9.1 crash fix. Breaking: minimum IDA 9.0 (PySide6)."
```

---

### Task 11: Final verification — full CI mirror

**Files:** None (verification only).

- [ ] **Step 1: Run the local CI mirror**

Run: `./ci-local.sh`
Expected: format + lint + mypy + pytest + desloppify all pass. Desloppify score must not drop more than 0.5 below baseline (89.0).

- [ ] **Step 2: Grep for any leftover PyQt5 references**

Run:
```bash
grep -rn "PyQt5\|QT_BINDING\|qt_flags\|qt_run\|_detect_binding\|is_pyside6\|FormToPyQtWidget" lucnhan/ tests/
```
Expected: no matches in `lucnhan/` or `tests/`. (Doc references in CHANGELOG are fine — they describe the removal.)

- [ ] **Step 3: Run the full test suite once more**

Run: `python3 -m pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 4: Final commit (only if ci-local.sh or grep surfaced fixes)**

If Step 1–2 surfaced anything, fix and commit:
```bash
git commit -am "fix(qt): clean up residual PyQt5 references"
```
Otherwise, no commit — Task 10's version bump is the final commit.

---

## Self-Review

**Spec coverage:**
- "Drop PyQt5 completely, PySide6-only" → Tasks 2, 3, 4, 5, 6, 7, 8.
- "Support IDA 9.0+" → Task 9 (docs), Task 10 (CHANGELOG breaking note).
- "No longer maintain old IDA versions" → Task 9 README Requirements, Task 10 CHANGELOG.
- Regression test for the IDA 9.1 crash → Task 1.

**Placeholder scan:** No "TBD", "TODO", "add error handling", "similar to Task N" placeholders. Every code step shows the actual code.

**Type consistency:** `FormToPySideWidget` (singular, used in Tasks 1, 5, 6) — matches `idaapi.PluginForm.FormToPySideWidget`. `qt_flags`/`qt_run` (deleted in Task 2, removed from call sites in Tasks 3, 4) — names consistent. Version `1.8.0` consistent across Tasks 10's four files.

**Ordering note:** Tasks 3–8 (call-site + test cleanup) land *before* Task 2 (the `qt_compat.py` rewrite) so no intermediate commit breaks the import graph. Task 1's regression test is written first (TDD) but only goes meaningfully green after Task 5.
