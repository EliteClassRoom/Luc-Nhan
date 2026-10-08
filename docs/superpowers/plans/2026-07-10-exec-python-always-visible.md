# ExecutePythonWidget Always-Visible Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render `execute_python` output in an always-visible scrollable block; remove the collapse/expand mechanism that was hiding script output.

**Architecture:** Replace the word-wrapped `_HeightCachedLabel` result with a read-only `QPlainTextEdit` (dynamic height, capped ~15 lines). Strip every collapse/expand toggle and state flag. Add a token-driven style builder for the result editor that supports an optional error text color (QSS first-rule-wins prevents appending).

**Tech Stack:** Python 3.10+, PySide6 (Qt6), pytest + unittest, ruff, mypy.

## Global Constraints

- Every module starts with `from __future__ import annotations`.
- Qt symbols imported from `lucnhan.ui.qt_compat`, never from `PySide6` directly.
- Host/theme helpers use lazy import inside functions to avoid cycles (pattern: `_tokens()` in `widgets_mutation.py`).
- Tool name constant: use `lucnhan.constants.EXECUTE_PYTHON_TOOL_NAME`, never hardcode the string.
- No mutation of existing objects — build new QSS strings.
- Run `python -m ruff format` and `python -m ruff check --fix` on changed files before committing.
- Tests stub Qt via `tests/qt_stubs.py`; no IDA Pro needed.

---

## File Structure

- **Modify** `lucnhan/ui/theme/widgets_mutation.py` — add `get_tool_result_editor_style(text_color=None)` + private `_tool_result_editor_style`.
- **Modify** `lucnhan/ui/styles.py` — re-export the new function (line ~144 area).
- **Modify** `lucnhan/ui/tool_widgets.py` — rewrite `ExecutePythonWidget`: drop toggle, swap result label for `QPlainTextEdit`, simplify `set_result` / `set_code` / `_apply_styles` / `set_docs_gate_status`.
- **Modify** `tests/tools/test_execute_python_widget.py` — flip collapse-assertions to always-visible, add scroll/cap/color tests.

---

### Task 1: Add result-editor style builder

**Files:**
- Modify: `lucnhan/ui/theme/widgets_mutation.py` (after line 99, the `_tool_approval_code_editor_style` block)
- Modify: `lucnhan/ui/styles.py:134-152` (re-export block)
- Test: `tests/ui/test_widgets_mutation_styles.py` (create)

**Interfaces:**
- Produces: `get_tool_result_editor_style(text_color: str | None = None) -> str` — QSS for a `QPlainTextEdit` result editor; when `text_color is None` uses `t.code_text`, otherwise the passed color.

- [ ] **Step 1: Write the failing test**

Create `tests/ui/test_widgets_mutation_styles.py`:

```python
"""Tests for tool-approval / result-editor style builders."""

from __future__ import annotations

import sys
import unittest

from tests.qt_stubs import ensure_pyside6_stubs

ensure_pyside6_stubs()

# Ensure the real module is loaded even if another test stubbed it.
sys.modules.pop("lucnhan.ui.theme.widgets_mutation", None)

from lucnhan.ui.theme.widgets_mutation import (  # noqa: E402
    get_tool_result_editor_style,
)


class TestResultEditorStyle(unittest.TestCase):
    def test_returns_qss_for_qplaintextedit(self):
        css = get_tool_result_editor_style()
        self.assertIn("QPlainTextEdit", css)
        self.assertIn("background:", css)
        self.assertIn("border:", css)

    def test_custom_text_color_appears_in_qss(self):
        css = get_tool_result_editor_style(text_color="#ff0000")
        self.assertIn("#ff0000", css)
        # The color must land in the QPlainTextEdit color rule, not just
        # appended (QSS keeps the first matching rule).
        self.assertIn("color: #ff0000", css.split("QScrollBar")[0])

    def test_default_has_no_literal_color_override_marker(self):
        # Default path: text_color is None → uses token code_text. The
        # QPlainTextEdit color rule is present and uses the token.
        css = get_tool_result_editor_style()
        self.assertIn("color:", css)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/ui/test_widgets_mutation_styles.py -v`
Expected: FAIL — `ImportError: cannot import name 'get_tool_result_editor_style'`.

- [ ] **Step 3: Write minimal implementation**

In `lucnhan/ui/theme/widgets_mutation.py`, add after the `_tool_approval_code_editor_style` function (after line 99):

```python
def _tool_result_editor_style(text_color: str | None = None) -> str:
    """QSS for the read-only result editor used by ExecutePythonWidget.

    Mirrors :func:`_tool_approval_code_editor_style` but lets the caller
    override the foreground colour — used to paint error output red.
    QSS keeps the first matching rule, so the override must replace the
    ``color`` value in the ``QPlainTextEdit`` rule rather than append a
    new rule after it.
    """
    t = _tokens()
    fg = text_color if text_color is not None else t.code_text
    return (
        f"QPlainTextEdit {{ "
        f"  color: {fg}; background: {t.code_bg}; "
        f"  font-size: inherit; border: 1px solid {t.mid}; border-radius: 4px; "
        f"  padding: 4px; "
        f"}}"
        f"QScrollBar:vertical {{ width: 8px; background: {t.code_bg}; }}"
        f"QScrollBar::handle:vertical {{ background: {t.mid}; border-radius: 4px; }}"
        f"QScrollBar:horizontal {{ height: 8px; background: {t.code_bg}; }}"
        f"QScrollBar::handle:horizontal {{ background: {t.mid}; border-radius: 4px; }}"
    )
```

Then add the public getter near the other public getters (after `get_tool_approval_code_editor_style` at line 201):

```python
def get_tool_result_editor_style(text_color: str | None = None) -> str:
    return _tool_result_editor_style(text_color)
```

- [ ] **Step 4: Re-export from styles.py**

In `lucnhan/ui/styles.py`, find the import block from `.theme.widgets_mutation` (around line 134-144) and add `get_tool_result_editor_style` to the imported names, keeping alphabetical-ish order matching the surrounding block.

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/ui/test_widgets_mutation_styles.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Lint and commit**

```bash
python -m ruff format lucnhan/ui/theme/widgets_mutation.py lucnhan/ui/styles.py
python -m ruff check lucnhan/ui/theme/widgets_mutation.py lucnhan/ui/styles.py tests/ui/test_widgets_mutation_styles.py --fix
git add lucnhan/ui/theme/widgets_mutation.py lucnhan/ui/styles.py tests/ui/test_widgets_mutation_styles.py
git commit -m "feat(ui): add get_tool_result_editor_style for execute_python output block"
```

---

### Task 2: Rewrite ExecutePythonWidget to always-visible + scrollable output

**Files:**
- Modify: `lucnhan/ui/tool_widgets.py:1367-1822` (the entire `ExecutePythonWidget` class)
- Test: `tests/tools/test_execute_python_widget.py`

**Interfaces:**
- Consumes: `get_tool_result_editor_style` (from Task 1, via `from ..styles import ...`).
- Produces: `ExecutePythonWidget` with simplified lifecycle — `set_code`, `set_arguments`, `set_result`, `set_docs_gate_status`, `show_approval_buttons`, `mark_done`, `hide_preview` (no-op, kept for ChatView grouping compat), `append_args_delta` (no-op, kept for ChatView streaming compat). Removed: `toggle_all`, `_set_expanded`, and all `_expanded`/`_result_*_visible`/`_status_detail_*` flags.

- [ ] **Step 1: Update module-level constant**

In `lucnhan/ui/tool_widgets.py`, find the constants block near line 41-43:

```python
_MAX_ARGS_DISPLAY = 2000
_MAX_RESULT_DISPLAY = 3000
_TOOL_PREVIEW_LINES = 3
```

Add after `_MAX_RESULT_DISPLAY`:

```python
#: Maximum visible lines in the ExecutePythonWidget result editor before it
#: scrolls. Mirrors the code editor's 15-line cap.
_RESULT_MAX_LINES = 15
```

- [ ] **Step 2: Update the styles import**

In `lucnhan/ui/tool_widgets.py`, in the `from .styles import (...)` block (lines 28-37), add `get_tool_result_editor_style` to the imported names.

- [ ] **Step 3: Rewrite the class**

Replace the entire `ExecutePythonWidget` class body (lines 1367-1822) with:

```python
class ExecutePythonWidget(QFrame):
    """Unified lifecycle widget for the ``execute_python`` tool.

    Renders code, an optional docs-review status line, approval buttons,
    and the execution result — all visible by default. State is inferred
    from the events received: the widget shows buttons only when
    ``show_approval_buttons()`` is called (driven by TOOL_APPROVAL_REQUEST)
    and shows the result after ``set_result()``. There is no
    collapse/expand toggle: every section is visible whenever its state
    is active, and the result lives in a scrollable read-only editor so
    a long script output never dominates the card.
    """

    approved = Signal(str, str)  # (tool_call_id, "allow"/"allow_all"/"deny")

    def __init__(self, tool_call_id: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("message_tool")
        self._tool_call_id = tool_call_id
        self._code = ""
        self._buttons_visible = False
        self._status_visible = False
        self._status_text = ""
        self._is_error = False
        self._blocked = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.setSpacing(2)

        layout.addLayout(self._build_header())
        layout.addWidget(self._build_code_section())
        self._status_line = self._build_status_line()
        layout.addWidget(self._status_line)
        layout.addLayout(self._build_approval_buttons())
        layout.addWidget(self._build_result_block())

        bind_theme(self, self._apply_styles)

    # ------------------------------------------------------------------
    # Builders
    # ------------------------------------------------------------------

    def _apply_styles(self, _tokens: object = None) -> None:
        """Re-apply card QSS and refresh child label colours on theme change."""
        self.setStyleSheet(_tool_card_css())
        tool_colors = get_tool_colors()
        color = _tool_color(constants.EXECUTE_PYTHON_TOOL_NAME)
        if getattr(self, "_bullet", None) is not None:
            self._bullet.setStyleSheet(f"color: {color}; font-size: inherit;")
        if getattr(self, "_name_label", None) is not None:
            self._name_label.setStyleSheet(f"color: {color}; font-weight: bold; font-size: inherit;")
        status_icon = getattr(self, "_status_icon", None)
        if status_icon is not None and status_icon.text():
            key = "status_error" if getattr(self, "_is_error", False) else "status_success"
            status_icon.setStyleSheet(f"color: {tool_colors[key]}; font-size: inherit;")
        result_edit = getattr(self, "_result_edit", None)
        if result_edit is not None:
            result_color = tool_colors["status_error"] if getattr(self, "_is_error", False) else None
            result_edit.setStyleSheet(get_tool_result_editor_style(result_color))
        # Re-paint the docs-review status label + detail against the live palette.
        if getattr(self, "_status_label", None) is not None and getattr(self, "_status_visible", False):
            self._status_label.setStyleSheet(f"color: {tool_colors['preview']}; font-size: inherit;")
        if getattr(self, "_status_detail", None) is not None and self._status_detail.isVisible():
            self._status_detail.setStyleSheet(f"color: {tool_colors['status_error']}; font-size: inherit;")

    def shutdown(self) -> None:
        """Detach the theme subscription so teardown does not warn."""
        disconnect_theme(self)

    def _build_header(self) -> QHBoxLayout:
        tool_colors = get_tool_colors()
        color = _tool_color(constants.EXECUTE_PYTHON_TOOL_NAME)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(0)

        # Indent aligned with the toggle button width in other tool cards
        # so the bullet sits at the same column.
        indent = QLabel("")
        indent.setFixedWidth(14)
        header.addWidget(indent)

        self._bullet = QLabel("●")
        self._bullet.setStyleSheet(f"color: {color}; font-size: inherit;")
        self._bullet.setFixedWidth(14)
        header.addWidget(self._bullet)

        self._name_label = QLabel(_strip_mcp_prefix(constants.EXECUTE_PYTHON_TOOL_NAME))
        self._name_label.setStyleSheet(f"color: {color}; font-weight: bold; font-size: inherit;")
        header.addWidget(self._name_label)

        header.addStretch()

        self._status_icon = QLabel("")
        self._status_icon.setStyleSheet(f"color: {tool_colors['status_spinner']}; font-size: inherit;")
        header.addWidget(self._status_icon)

        return header

    def _build_code_section(self) -> QWidget:
        section = QWidget()
        layout = QVBoxLayout(section)
        layout.setContentsMargins(28, 2, 0, 2)
        layout.setSpacing(2)

        self._code_info_label = QLabel("")
        self._code_info_label.setStyleSheet("color: #808080; font-size: inherit;")
        self._code_info_label.setVisible(False)
        layout.addWidget(self._code_info_label)

        self._code_edit = QPlainTextEdit()
        self._code_edit.setReadOnly(True)
        self._code_edit.setStyleSheet(get_tool_approval_code_editor_style())
        self._code_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._code_edit.setVisible(False)
        layout.addWidget(self._code_edit)
        self._code_highlighter = _PythonHighlighter(self._code_edit.document())

        # Visible only when set_code() provides code (handled there).
        section.setVisible(False)
        return section

    def _build_status_line(self) -> QWidget:
        """Build the docs-review status row (header + always-on detail)."""
        tool_colors = get_tool_colors()
        wrapper = QWidget()
        wrapper_layout = QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.setSpacing(0)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(4)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet(f"color: {tool_colors['preview']}; font-size: inherit;")
        self._status_label.setVisible(False)
        header_row.addWidget(self._status_label, 1)
        wrapper_layout.addLayout(header_row)

        self._status_detail = QLabel("")
        self._status_detail.setWordWrap(True)
        self._status_detail.setTextInteractionFlags(
            Qt.TextInteractionFlag(
                Qt.TextInteractionFlag.TextSelectableByMouse.value
                | Qt.TextInteractionFlag.TextSelectableByKeyboard.value
            )
        )
        self._status_detail.setStyleSheet(f"color: {tool_colors['status_error']}; font-size: inherit;")
        self._status_detail.setVisible(False)
        wrapper_layout.addWidget(self._status_detail)

        wrapper.setVisible(False)
        return wrapper

    def _build_approval_buttons(self) -> QHBoxLayout:
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)

        self._allow_btn = QToolButton()
        self._allow_btn.setText("  Allow  ")
        self._allow_btn.setStyleSheet(get_tool_approval_allow_btn_style())
        self._allow_btn.clicked.connect(self._on_allow)
        btn_layout.addWidget(self._allow_btn)

        self._always_btn = QToolButton()
        self._always_btn.setText("  Always Allow  ")
        self._always_btn.setStyleSheet(get_tool_approval_always_btn_style())
        self._always_btn.clicked.connect(self._on_always_allow)
        btn_layout.addWidget(self._always_btn)

        self._deny_btn = QToolButton()
        self._deny_btn.setText("  Deny  ")
        self._deny_btn.setStyleSheet(get_tool_approval_deny_btn_style())
        self._deny_btn.clicked.connect(self._on_deny)
        btn_layout.addWidget(self._deny_btn)

        btn_layout.addStretch()

        # Wrap in a container so we can toggle visibility as a unit.
        self._buttons_container = QWidget()
        self._buttons_container.setLayout(btn_layout)
        self._buttons_container.setVisible(False)
        # Return a layout-like wrapper: embed the container in a layout.
        wrapper = QHBoxLayout()
        wrapper.setContentsMargins(0, 0, 0, 0)
        wrapper.addWidget(self._buttons_container)
        return wrapper

    def _build_result_block(self) -> QWidget:
        tool_colors = get_tool_colors()
        self._result_block = QFrame()
        self._result_block.setStyleSheet(_tool_card_css())
        layout = QVBoxLayout(self._result_block)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.setSpacing(2)

        self._result_header_label = QLabel("Result:")
        self._result_header_label.setStyleSheet(
            f"color: {tool_colors['result_header']}; font-weight: bold; font-size: inherit;"
        )
        self._result_header_label.setVisible(False)
        layout.addWidget(self._result_header_label)

        self._result_edit = QPlainTextEdit()
        self._result_edit.setReadOnly(True)
        self._result_edit.setStyleSheet(get_tool_result_editor_style())
        self._result_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self._result_edit)

        self._result_block.setVisible(False)
        return self._result_block

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_code(self, code: str) -> None:
        self._code = code
        self._code_edit.setPlainText(code)
        lines = code.strip().splitlines() if code.strip() else []
        if lines:
            self._code_info_label.setText(f"Python code — {len(lines)} line{'s' if len(lines) != 1 else ''}")
            self._code_info_label.setVisible(True)
            visible = min(len(lines), 15)
            line_height = self._code_edit.fontMetrics().lineSpacing()
            self._code_edit.setFixedHeight(line_height * visible + 16)
            self._code_edit.setVisible(True)
            self._code_section().setVisible(True)
        else:
            self._code_info_label.setVisible(False)
            self._code_edit.setVisible(False)
            self._code_section().setVisible(False)

    def append_args_delta(self, delta: str) -> None:
        """Accumulate streaming args (TOOL_CALL_ARGS_DELTA).

        ExecutePythonWidget renders code only after ``set_arguments()`` parses
        the complete JSON on TOOL_CALL_DONE, so deltas are a no-op here — but
        ChatView calls this unconditionally for every tool widget.
        """
        # No-op: code is extracted and rendered in set_arguments() on TOOL_CALL_DONE.

    def set_arguments(self, args_text: str) -> None:
        """Parse JSON args and extract the code (compat with ToolCallWidget API)."""
        try:
            args = json.loads(args_text) if args_text.strip() else {}
            code = args.get("code", args.get("script", "")) or args_text
        except (json.JSONDecodeError, TypeError, AttributeError):
            code = args_text
        self.set_code(code)

    def set_docs_gate_status(
        self,
        state: str,
        reasons: tuple[str, ...] = (),
        summary: str = "",
    ) -> None:
        self._status_visible = True
        tool_colors = get_tool_colors()
        if state == "running":
            self._status_text = "\U0001f50d Reviewing script..."
            if reasons:
                self._status_text += f" (complex: {', '.join(reasons[:3])})"
            self._status_label.setStyleSheet(f"color: {tool_colors['preview']}; font-size: inherit;")
            self._status_icon.setText("⟳")
            self._status_detail.setVisible(False)
        elif state == "approved":
            self._status_text = "✓ Docs review passed"
            self._status_label.setStyleSheet(
                f"color: {tool_colors['status_success']}; font-size: inherit; opacity: 0.7;"
            )
            self._status_icon.setText("✓")
            self._status_detail.setVisible(False)
        elif state == "blocked":
            self._blocked = True
            self._status_text = "✗ Docs review blocked"
            self._status_label.setStyleSheet(
                f"color: {tool_colors['status_error']}; font-weight: bold; font-size: inherit;"
            )
            self._status_icon.setText("✗")
            self._status_detail.setText(summary or "The reviewer flagged the script.")
            self._status_detail.setStyleSheet(f"color: {tool_colors['status_error']}; font-size: inherit;")
            self._status_detail.setVisible(True)
            self._buttons_visible = False
            self._buttons_container.setVisible(False)
        elif state == "failed":
            self._status_text = f"⚠ Docs review error — review manually. ({summary})"
            self._status_label.setStyleSheet(f"color: {tool_colors['status_spinner']}; font-size: inherit;")
            self._status_icon.setText("⚠")
            self._status_detail.setVisible(False)
            # FAILED keeps buttons visible so the user can still approve.
        else:
            self._status_text = ""
            self._status_visible = False
            self._status_detail.setVisible(False)

        self._status_label.setText(self._status_text)
        self._status_line.setVisible(self._status_visible)

    def show_approval_buttons(self) -> None:
        if not self._status_visible or not self._status_text.startswith("✗"):
            # Keep buttons hidden if currently hard-blocked by docs gate.
            self._buttons_visible = True
            self._buttons_container.setVisible(True)

    def mark_done(self) -> None:
        """Mark the call complete (used by history restore). Safe to call
        multiple times."""
        if self._status_icon.text() not in ("✓", "✗"):
            tool_colors = get_tool_colors()
            self._status_icon.setText("✓")
            self._status_icon.setStyleSheet(f"color: {tool_colors['status_success']}; font-size: inherit;")

    def hide_preview(self) -> None:
        """No-op retained for ChatView tool-grouping compatibility.

        ToolCallWidget / ToolBatchWidget collapse their content when nested
        inside a ToolGroupWidget; ExecutePythonWidget has no collapse state,
        so grouping leaves it fully visible.
        """
        return

    def set_result(self, result: str, is_error: bool = False) -> None:
        tool_colors = get_tool_colors()
        self._is_error = is_error
        # When the docs gate blocked the script, the loop emits a TOOL_RESULT
        # carrying the reviewer summary as an error. That summary already
        # lives in the status detail line — rendering a separate result block
        # would duplicate it. Skip.
        if self._blocked:
            self._buttons_visible = False
            self._buttons_container.setVisible(False)
            return
        display = result[:_MAX_RESULT_DISPLAY] + "\n... (truncated)" if len(result) > _MAX_RESULT_DISPLAY else result
        self._result_edit.setPlainText(display)
        lines = display.splitlines() if display.strip() else []
        visible = max(min(len(lines), _RESULT_MAX_LINES), 1)
        line_height = self._result_edit.fontMetrics().lineSpacing()
        self._result_edit.setFixedHeight(line_height * visible + 16)
        self._result_header_label.setVisible(True)
        self._result_block.setVisible(True)
        # Hide approval buttons after result arrives.
        self._buttons_visible = False
        self._buttons_container.setVisible(False)

        if is_error:
            self._result_edit.setStyleSheet(get_tool_result_editor_style(tool_colors["status_error"]))
            self._status_icon.setText("✗")
            self._status_icon.setStyleSheet(f"color: {tool_colors['status_error']}; font-size: inherit;")
            self._bullet.setStyleSheet(f"color: {tool_colors['status_error']}; font-size: inherit;")
        else:
            self._result_edit.setStyleSheet(get_tool_result_editor_style())
            self._status_icon.setText("✓")
            self._status_icon.setStyleSheet(f"color: {tool_colors['status_success']}; font-size: inherit;")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _code_section(self) -> QWidget:
        # The code section is the 2nd widget in the main layout.
        return self.layout().itemAt(1).widget()

    def _disable_buttons(self) -> None:
        self._allow_btn.setEnabled(False)
        self._always_btn.setEnabled(False)
        self._deny_btn.setEnabled(False)

    def _on_allow(self) -> None:
        self._disable_buttons()
        self._allow_btn.setText("  Allowed  ")
        self._allow_btn.setStyleSheet(get_tool_approval_disabled_btn_style())
        self.approved.emit(self._tool_call_id, "allow")

    def _on_always_allow(self) -> None:
        self._disable_buttons()
        self._always_btn.setText("  Always Allowed  ")
        self._always_btn.setStyleSheet(get_tool_approval_disabled_btn_style())
        self.approved.emit(self._tool_call_id, "allow_all")

    def _on_deny(self) -> None:
        self._disable_buttons()
        self._deny_btn.setText("  Denied  ")
        self._deny_btn.setStyleSheet(get_tool_approval_disabled_btn_style())
        self.approved.emit(self._tool_call_id, "deny")
```

- [ ] **Step 4: Run the full widget test suite to see what breaks**

Run: `python -m pytest tests/tools/test_execute_python_widget.py -v`
Expected: Several FAILs — tests asserting collapsed state / `toggle_all` / `_result_content_visible` now break. These are fixed in Task 3. Confirm only the expected tests fail (no import/syntax errors).

- [ ] **Step 5: Lint the changed file**

```bash
python -m ruff format lucnhan/ui/tool_widgets.py
python -m ruff check lucnhan/ui/tool_widgets.py --fix
```

- [ ] **Step 6: Commit (tests still failing — intentional, fixed next task)**

```bash
git add lucnhan/ui/tool_widgets.py
git commit -m "refactor(ui): rewrite ExecutePythonWidget to always-visible scrollable output

Drop the collapse/expand toggle and all _result_*_visible state flags.
Result now renders in a read-only QPlainTextEdit with a dynamic height
capped at 15 lines. Widget tests will be updated in the next commit."
```

---

### Task 3: Flip widget tests to always-visible behaviour

**Files:**
- Modify: `tests/tools/test_execute_python_widget.py` (rewrite test classes)

**Interfaces:**
- Consumes: `ExecutePythonWidget` API from Task 2.

- [ ] **Step 1: Replace the test file content**

Replace the entire contents of `tests/tools/test_execute_python_widget.py` with:

```python
"""Tests for ExecutePythonWidget (always-visible, no collapse/expand)."""

from __future__ import annotations

import json
import sys
import unittest

from tests.qt_stubs import ensure_pyside6_stubs

ensure_pyside6_stubs()

# Ensure the real module is loaded even if another test stubbed it.
sys.modules.pop("lucnhan.ui.tool_widgets", None)

from lucnhan.ui.tool_widgets import ExecutePythonWidget  # noqa: E402


class TestExecutePythonWidgetInit(unittest.TestCase):
    def test_init_idle_no_buttons(self):
        w = ExecutePythonWidget("tc1")
        # No code set yet.
        self.assertEqual(w._code, "")
        # Buttons should not be shown until show_approval_buttons().
        self.assertFalse(w._buttons_visible)


class TestSetArguments(unittest.TestCase):
    def test_set_arguments_extracts_code_from_json(self):
        w = ExecutePythonWidget("tc1")
        w.set_arguments(json.dumps({"code": "print(1)\nprint(2)\n"}))
        self.assertEqual(w._code, "print(1)\nprint(2)\n")

    def test_set_arguments_extracts_script_field(self):
        w = ExecutePythonWidget("tc1")
        w.set_arguments(json.dumps({"script": "x = 1"}))
        self.assertEqual(w._code, "x = 1")

    def test_set_arguments_fallback_raw_on_bad_json(self):
        w = ExecutePythonWidget("tc1")
        w.set_arguments("not valid json")
        self.assertEqual(w._code, "not valid json")


class TestDocsGateStatus(unittest.TestCase):
    def test_running_sets_status_text(self):
        w = ExecutePythonWidget("tc1")
        w.set_docs_gate_status("running", reasons=("2 IDA modules",))
        self.assertIn("Reviewing", w._status_text)
        self.assertIn("2 IDA modules", w._status_text)
        self.assertTrue(w._status_visible)

    def test_approved_sets_status_text(self):
        w = ExecutePythonWidget("tc1")
        w.set_docs_gate_status("approved")
        self.assertIn("Docs review passed", w._status_text)
        self.assertTrue(w._status_visible)

    def test_blocked_hides_buttons(self):
        w = ExecutePythonWidget("tc1")
        w.show_approval_buttons()
        self.assertTrue(w._buttons_visible)
        w.set_docs_gate_status("blocked", summary="bad API")
        self.assertFalse(w._buttons_visible)
        self.assertIn("Docs review blocked", w._status_text)

    def test_blocked_status_detail_visible_by_default(self):
        """A blocked review shows the full reviewer summary immediately —
        there is no collapse toggle to click open."""
        w = ExecutePythonWidget("tc1")
        w.set_docs_gate_status("blocked", summary="ida_bytes.patch_qword is not a real API" * 5)
        self.assertTrue(w._status_visible)
        self.assertTrue(w._status_detail.isVisible())

    def test_blocked_result_does_not_dup(self):
        """When the docs gate blocks, the loop emits TOOL_RESULT with the
        reviewer summary as an error. The widget already shows that summary
        in the status detail, so set_result must NOT render a result block."""
        w = ExecutePythonWidget("tc1")
        w.set_docs_gate_status("blocked", summary="rewrite guidance")
        w.set_result("rewrite guidance", is_error=True)
        self.assertFalse(w._result_block.isVisible())

    def test_failed_shows_buttons(self):
        """FAILED (reviewer crash) still lets the user approve."""
        w = ExecutePythonWidget("tc1")
        w.show_approval_buttons()
        w.set_docs_gate_status("failed", summary="boom")
        self.assertTrue(w._buttons_visible)
        self.assertIn("review manually", w._status_text.lower())

    def test_no_status_hidden_by_default(self):
        w = ExecutePythonWidget("tc1")
        self.assertFalse(w._status_visible)


class TestApprovalButtons(unittest.TestCase):
    def test_show_approval_buttons_makes_visible(self):
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)")
        w.show_approval_buttons()
        self.assertTrue(w._buttons_visible)

    def test_allow_emits_signal(self):
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)")
        w.show_approval_buttons()
        captured = []
        w.approved.connect(lambda tid, decision: captured.append((tid, decision)))
        w._on_allow()
        self.assertEqual(captured, [("tc1", "allow")])

    def test_always_allow_emits_allow_all(self):
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)")
        w.show_approval_buttons()
        captured = []
        w.approved.connect(lambda tid, decision: captured.append((tid, decision)))
        w._on_always_allow()
        self.assertEqual(captured, [("tc1", "allow_all")])

    def test_deny_emits_deny(self):
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)")
        w.show_approval_buttons()
        captured = []
        w.approved.connect(lambda tid, decision: captured.append((tid, decision)))
        w._on_deny()
        self.assertEqual(captured, [("tc1", "deny")])


class TestSetResult(unittest.TestCase):
    def test_set_result_success_shows_result_block(self):
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)")
        w.set_result("42", is_error=False)
        self.assertTrue(w._result_block.isVisible())
        self.assertFalse(w._is_error)

    def test_set_result_shows_output_in_editor(self):
        """Output must be visible immediately — no toggle required."""
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)")
        w.set_result("the answer is 42", is_error=False)
        self.assertEqual(w._result_edit.toPlainText(), "the answer is 42")
        self.assertTrue(w._result_block.isVisible())

    def test_set_result_error_marks_error_and_colors(self):
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)")
        w.set_result("NameError: x", is_error=True)
        self.assertTrue(w._result_block.isVisible())
        self.assertTrue(w._is_error)
        self.assertEqual(w._status_icon.text(), "✗")

    def test_result_short_output_compact(self):
        """A short output renders at its natural line count (no cap, no
        scroll)."""
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)")
        w.set_result("line one\nline two", is_error=False)
        lines = 2
        line_height = w._result_edit.fontMetrics().lineSpacing()
        self.assertEqual(w._result_edit.height(), line_height * lines + 16)

    def test_result_long_output_capped_and_scrollable(self):
        """A long output caps the editor height at _RESULT_MAX_LINES; the
        full text is still present in the document (scrollable)."""
        from lucnhan.ui.tool_widgets import _RESULT_MAX_LINES

        long_output = "\n".join(f"line {i}" for i in range(50))
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)")
        w.set_result(long_output, is_error=False)
        line_height = w._result_edit.fontMetrics().lineSpacing()
        self.assertEqual(w._result_edit.height(), line_height * _RESULT_MAX_LINES + 16)
        # Full content preserved for scrolling.
        self.assertIn("line 49", w._result_edit.toPlainText())


class TestMarkDone(unittest.TestCase):
    def test_mark_done_is_safe_to_call(self):
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)")
        # mark_done must not raise whether or not result is set.
        w.mark_done()
        w.set_result("ok", is_error=False)
        w.mark_done()


class TestHidePreview(unittest.TestCase):
    def test_hide_preview_is_noop(self):
        """hide_preview is retained for ChatView grouping compat but is a
        no-op — the widget has no collapse state."""
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)\nprint(2)\n")
        # Should not raise and should not hide code.
        w.hide_preview()
        self.assertTrue(w._code_section().isVisible())


class TestCodeDisplayedOnce(unittest.TestCase):
    def test_no_redundant_description_label(self):
        """The widget must not carry a redundant 'Run Python code: ...'
        description — code is shown once in the code editor."""
        w = ExecutePythonWidget("tc1")
        w.set_arguments(json.dumps({"code": "import idautils\nprint(1)\n"}))
        # There should be no _description_label attribute holding a
        # duplicate of the first code line.
        self.assertFalse(getattr(w, "_description_label", None))


class TestAlwaysVisible(unittest.TestCase):
    def test_no_toggle_button_in_header(self):
        """The collapse toggle (QToolButton) is gone from the header."""
        w = ExecutePythonWidget("tc1")
        self.assertFalse(getattr(w, "_toggle_btn", None))

    def test_set_code_always_visible(self):
        """After set_code, the code section is visible without toggling."""
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)")
        self.assertTrue(w._code_section().isVisible())
        self.assertTrue(w._code_edit.isVisible())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `python -m pytest tests/tools/test_execute_python_widget.py -v`
Expected: PASS (all tests).

- [ ] **Step 3: Lint and commit**

```bash
python -m ruff format tests/tools/test_execute_python_widget.py
python -m ruff check tests/tools/test_execute_python_widget.py --fix
git add tests/tools/test_execute_python_widget.py
git commit -m "test(ui): flip execute_python widget tests to always-visible behaviour"
```

---

### Task 4: Verify ChatView callers and run full CI

**Files:**
- Verify only: `lucnhan/ui/chat_view.py`

**Interfaces:**
- Consumes: `ExecutePythonWidget.hide_preview` (no-op), `append_args_delta` (no-op), `set_result`, `set_code`/`set_arguments` (via `set_arguments`).

- [ ] **Step 1: Verify ChatView callers still work**

ChatView calls on ExecutePythonWidget at these lines (from earlier grep):
- `chat_view.py:738` — `run_widget.hide_preview()` → now no-op. Safe.
- `chat_view.py:747` — `widget.hide_preview()` → no-op. Safe.
- `chat_view.py:954` — `existing_tw.append_args_delta(event.tool_args)` → no-op. Safe.
- `chat_view.py:963` — `existing_tw.set_result(event.tool_result, event.tool_is_error)` → works.
- `chat_view.py:978` — `isinstance(existing, ExecutePythonWidget)` then accesses — verify the block still only references attributes that exist (it surfaces the widget from a group). Read lines 968-1030 to confirm no `toggle_all()` / `_set_expanded` / `_result_content_visible` references remain.

Run: `grep -n "toggle_all\|_set_expanded\|_result_content_visible\|_result_header_visible\|_status_detail_visible\|_code_expanded\|_result_block_visible" lucnhan/ui/chat_view.py`
Expected: no output (no references to removed attributes/methods).

- [ ] **Step 2: Fix any stragglers if grep found references**

If grep returns hits, those callers referenced removed API. For each hit, decide: if it was driving the old toggle, delete the call; if it read a removed flag, replace with the new always-visible behaviour (e.g. drop the branch). Do NOT reintroduce collapse state.

- [ ] **Step 3: Run the chat_view + panel test suites**

Run:
```bash
python -m pytest tests/tools/test_chat_view.py tests/tools/test_panel_core.py tests/ui/test_chat_view_restore.py -v
```
Expected: PASS. These exercise tool-widget creation, result restore, and grouping.

- [ ] **Step 4: Run local CI**

Run: `./ci-local.sh --fix`
Expected: PASS (format + lint + mypy + pytest + desloppify ≥ 88.5).

- [ ] **Step 5: Commit any ChatView fixes (if Step 2 changed anything)**

```bash
git add lucnhan/ui/chat_view.py
git commit -m "fix(ui): drop removed execute_python toggle refs in ChatView"
```
(If no changes were needed, skip this commit.)

- [ ] **Step 6: Final verification**

Run: `python -m pytest tests/ -k "execute_python or chat_view or panel_core" -v`
Expected: all PASS. Confirm the `execute_python` output is no longer gated behind a toggle by mentally tracing `TOOL_RESULT` → `set_result` → `_result_block.setVisible(True)`.

---

## Self-Review

**1. Spec coverage:**
- "Bỏ nút ▶" → Task 2 Step 3, `_build_header` drops `_toggle_btn`; Task 3 `test_no_toggle_button_in_header`. ✓
- "Code luôn visible khi có code" → Task 2 `set_code` always sets visible; Task 3 `test_set_code_always_visible`. ✓
- "Result scrollable QPlainTextEdit, cap 15 dòng" → Task 2 `_build_result_block` + `set_result` height math; Task 3 `test_result_long_output_capped_and_scrollable`. ✓
- "Error chỉ đổi màu" → Task 2 `set_result` error branch uses `get_tool_result_editor_style(error_color)`; Task 3 `test_set_result_error_marks_error_and_colors`. ✓
- "Blocked summary luôn visible" → Task 2 `set_docs_gate_status("blocked")` sets `_status_detail.setVisible(True)`; Task 3 `test_blocked_status_detail_visible_by_default`. ✓
- "Style builder cho error override" → Task 1 `get_tool_result_editor_style(text_color)`. ✓
- "hide_preview / append_args_delta compat" → Task 2 retains both as no-op; Task 3 `test_hide_preview_is_noop`; Task 4 Step 1 verifies ChatView callers. ✓

**2. Placeholder scan:** No TBD/TODO in steps. All code shown inline. The one "decide per-hit" instruction (Task 4 Step 2) is guarded by an explicit grep with "expected: no output" — the fallback is only for the unexpected case. ✓

**3. Type consistency:** `get_tool_result_editor_style(text_color: str | None = None)` — Task 1 defines, Task 2 calls with `tool_colors["status_error"]` (str) or no arg (None). `_result_edit` is the consistent name across `_build_result_block`, `set_result`, `_apply_styles`, and tests. `_RESULT_MAX_LINES` constant referenced in both `tool_widgets.py` and the test import. ✓

No gaps found. Plan is ready.
