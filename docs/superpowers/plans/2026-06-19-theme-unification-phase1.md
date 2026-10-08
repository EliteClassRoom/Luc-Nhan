# Theme Unification Phase 1 — Bug-Fix + Dead Code Removal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the real "theme doesn't apply after switch" bug (panel.py `minimal_style` not re-applied on theme change) and remove ~1180 lines of dead `LIGHT_THEME`/`DARK_THEME` code.

**Architecture:** Phase 1 of the 4-phase strangler in `docs/superpowers/specs/2026-06-19-theme-system-unification-design.md`. The bug fix wires `ida/ui/panel.py` to subscribe to `ThemeManager.themeChanged` and rebuild its host-scoped `minimal_style` QSS on every theme switch. Dead-code removal deletes `LIGHT_THEME`, `DARK_THEME`, and the no-op `build_theme_stylesheet()` from `styles.py` and migrates the two test files that reference them.

**Tech Stack:** Python 3.11+, PySide6 (Qt6), pytest. Repo commands: `./ci-local.sh`, `python -m pytest`, `python -m ruff`, `python -m mypy`.

## Global Constraints

- Python `from __future__ import annotations` at top of every edited module.
- Host API imports (ida_*) use `importlib.import_module()` in `try/except ImportError` — never module-level. (Project rule, CLAUDE.md §1.)
- Type hints on all new/changed signatures. mypy must stay clean on `lucnhan/core` + `lucnhan/providers`.
- `./ci-local.sh` must pass (format + lint + mypy + pytest + desloppify).
- Branch: `feat/theme-phase1` off `master`. Commit per task with conventional-commit format.
- **IDA visual verification is the primary quality gate** for the bug fix — Qt theme cannot be unit-tested. The implementer (or user) must open IDA, switch theme mid-session, and confirm no stale widgets.

## File Structure

**Modify:**
- `lucnhan/ida/ui/panel.py` — add `themeChanged` subscription + extract `_reapply_minimal_style()` from the inline `_apply_styles` body so it can be called both at init and on theme change.
- `lucnhan/ui/styles.py` — delete `LIGHT_THEME` (lines 72-661), `DARK_THEME` (lines 664-1252), `build_theme_stylesheet()` (lines 1450-1457). Keep everything else (the host-inherit bridge, re-exports, `build_*_stylesheet` builders).
- `lucnhan/ui/panel_core.py` — remove the 2 no-op `build_theme_stylesheet(self)` call sites (lines 531, 730) and the `build_theme_stylesheet` import (line 46).
- `tests/tools/test_panel_core.py` — remove `DARK_THEME` + `build_theme_stylesheet` from the stub whitelist (lines 107-108).
- `tests/tools/test_settings_dialog.py` — remove `DARK_THEME` + `build_theme_stylesheet` from the stub whitelist (lines 69-70) and the 2 override lines (123-124).

**Create:**
- `tests/tools/test_ida_panel_theme_reapply.py` — new test file asserting panel.py re-applies `minimal_style` on `themeChanged`.

---

### Task 1: Extract `_reapply_minimal_style()` in panel.py (refactor, no behavior change)

**Files:**
- Modify: `lucnhan/ida/ui/panel.py` (the `_apply_styles` / minimal_style block, lines ~260-417)
- Test: existing `tests/tools/test_ida_panel.py` (smoke)

**Interfaces:**
- Consumes: `ThemeManager.instance().tokens()` (already imported lazily elsewhere in panel.py)
- Produces: `LucNhanPanel._reapply_minimal_style(self) -> None` — rebuilds and sets `self._core.setStyleSheet(minimal_style)`. Callable at init and from the `themeChanged` slot.

**Why this first:** Pure refactor that makes the existing inline QSS-build reusable. No behavior change means existing tests stay green and we have a safe base before wiring the signal.

- [ ] **Step 1: Read the current minimal_style block**

Read `lucnhan/ida/ui/panel.py` from the line where the colors are computed (the `_rgb_to_hex` / token reads, ~line 280) through line 417 (`self._core.setStyleSheet(minimal_style)`). Note the exact start line of the color setup so the extract is precise.

- [ ] **Step 2: Extract the method**

Wrap the existing code that computes colors + builds `minimal_style` + calls `self._core.setStyleSheet(minimal_style)` into a new method:

```python
def _reapply_minimal_style(self) -> None:
    """Rebuild and re-apply the host-scoped minimal QSS.

    Called once at construction and again on every theme change so the
    message/input/button objects pick up the new palette. The QSS is
    object-name-scoped (QFrame#thinking_block etc.) so it never bleeds
    into the host (IDA) UI.
    """
    # --- existing color computation (surface, accent, text_color, ...) ---
    # KEEP the exact lines from the current body verbatim, dedented into the method.
    ...
    minimal_style = f"""..."""  # KEEP the exact f-string
    self._core.setStyleSheet(minimal_style)
```

The call site (init path) becomes a single line: `self._reapply_minimal_style()`.

- [ ] **Step 3: Verify refactor is behavior-preserving**

Run: `python -m pytest tests/tools/test_ida_panel.py -v`
Expected: PASS (same as before refactor). Also `python -c "import lucnhan.ida.ui.panel"` to confirm no import error.

- [ ] **Step 4: Commit**

```bash
git add lucnhan/ida/ui/panel.py
git commit -m "refactor(ida): extract _reapply_minimal_style in panel wrapper"
```

---

### Task 2: Subscribe panel.py to themeChanged (the bug fix)

**Files:**
- Modify: `lucnhan/ida/ui/panel.py` (add subscription in the init/watcher path + disconnect in shutdown)
- Test: `tests/tools/test_ida_panel_theme_reapply.py` (new)

**Interfaces:**
- Consumes: `LucNhanPanel._reapply_minimal_style()` (from Task 1), `ThemeManager.instance().themeChanged`
- Produces: panel.py now repaints `minimal_style` whenever `themeChanged` fires.

- [ ] **Step 1: Write the failing test**

Create `tests/tools/test_ida_panel_theme_reapply.py`:

```python
"""Tests that the IDA panel wrapper re-applies minimal_style on theme change.

Regression: the host-scoped minimal_style QSS was built once at construction
and never re-applied when the user switched theme mid-session, so message/
input/button objects kept the old palette. The panel now subscribes to
ThemeManager.themeChanged and rebuilds the QSS on every emit.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from tests.qt_stubs import ensure_pyside6_stubs

ensure_pyside6_stubs()


class TestPanelReappliesMinimalStyleOnThemeChange(unittest.TestCase):
    def test_subscribe_connects_reapply_slot(self):
        from lucnhan.ui.theme.manager import ThemeManager

        manager = ThemeManager.instance()
        manager.themeChanged = MagicMock()

        with patch("lucnhan.ida.ui.panel.ThemeManager", return_value=manager):
            # The panel wrapper subscribes during init; assert the slot is
            # connected to the manager's themeChanged signal.
            panel = _build_minimal_panel()
        manager.themeChanged.connect.assert_any_call(panel._reapply_minimal_style)

    def test_theme_changed_emit_calls_reapply(self):
        panel = _build_minimal_panel()
        panel._reapply_minimal_style = MagicMock()
        # Simulate the manager emitting themeChanged.
        from lucnhan.ui.theme.manager import ThemeManager

        ThemeManager.instance().themeChanged.emit(object())
        panel._reapply_minimal_style.assert_called()


def _build_minimal_panel():
    """Construct an IDA panel wrapper with mocked IDA + Qt deps."""
    from tests.mocks.ida_mock import install_ida_mocks

    install_ida_mocks()
    from lucnhan.ida.ui.panel import LucNhanPanel

    return LucNhanPanel()


if __name__ == "__main__":
    unittest.main()
```

Note: the test uses MagicMock for the signal. If `tests/qt_stubs` already provides a real `_Signal`, adjust to assert on `.connect` calls instead. The implementer should inspect `tests/qt_stubs.py` `_Signal` behavior and adapt the assertion to whichever is present.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/tools/test_ida_panel_theme_reapply.py -v`
Expected: FAIL — `themeChanged.connect` not called (subscription not added yet) or `_reapply_minimal_style` not called on emit.

- [ ] **Step 3: Add the subscription**

In `lucnhan/ida/ui/panel.py`, add a helper that subscribes and call it from the init path (near `_maybe_start_theme_watcher`):

```python
def _subscribe_theme_changes(self) -> None:
    """Re-apply minimal_style whenever the active theme changes.

    Unlike panel_core (host-agnostic), this wrapper owns the host-scoped
    QSS for message/input/button objects. It must rebuild that QSS on
    every theme switch or those objects keep the old palette.
    """
    try:
        from lucnhan.ui.theme.manager import ThemeManager

        ThemeManager.instance().themeChanged.connect(self._reapply_minimal_style)
    except Exception as e:
        import ida_kernwin

        ida_kernwin.msg(f"[Luc Nhan] themeChanged subscribe failed: {type(e).__name__}: {e}")
```

Call `self._subscribe_theme_changes()` right after `self._reapply_minimal_style()` in the init path.

Add a disconnect in the panel's shutdown/cleanup path (search for the existing `_theme_watcher` stop or `closeEvent`; mirror its lifecycle). If no shutdown hook exists, add disconnect in the same method that stops the watcher:

```python
try:
    from lucnhan.ui.theme.manager import ThemeManager

    ThemeManager.instance().themeChanged.disconnect(self._reapply_minimal_style)
except Exception:
    pass  # best-effort; Qt removes connections on widget destruction anyway
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/tools/test_ida_panel_theme_reapply.py -v`
Expected: PASS.

- [ ] **Step 5: Run full panel test regression**

Run: `python -m pytest tests/tools/test_ida_panel.py tests/tools/test_panel_core.py tests/tools/test_theme_watcher.py -v`
Expected: PASS, no regressions.

- [ ] **Step 6: IDA visual verification (user / implementer)**

This is the **primary quality gate** for the bug fix. Open Luc Nhan in IDA Pro, start a chat (so message widgets exist), then switch theme via Settings (dark → light → ida → dark). Confirm:
- `thinking_block`, `message_queued`, `message_question`, `message_thinking` frames repaint.
- `input_area`, `send_button`, `cancel_button`, `history_nav` repaint.
- No widget keeps the old palette.

If visual verification is not possible in this session, STOP and request it before committing — do not merge a theme fix that was never visually confirmed.

- [ ] **Step 7: Commit**

```bash
git add lucnhan/ida/ui/panel.py tests/tools/test_ida_panel_theme_reapply.py
git commit -m "fix(ida): re-apply minimal_style on theme change

panel.py built the host-scoped minimal_style QSS once at construction and
never re-applied it on theme switch, leaving message/input/button objects
with a stale palette. Subscribe to ThemeManager.themeChanged and rebuild."
```

---

### Task 3: Migrate test references to deleted symbols

**Files:**
- Modify: `tests/tools/test_panel_core.py` (lines 107-108)
- Modify: `tests/tools/test_settings_dialog.py` (lines 69-70, 123-124)

**Interfaces:**
- Consumes: none
- Produces: tests no longer reference `DARK_THEME` or `build_theme_stylesheet` (so Task 4's deletion won't break collection)

**Why before deletion:** If we delete the symbols first, these tests fail to collect. Migrate first so deletion is clean.

- [ ] **Step 1: Read the exact stub-list context in both test files**

Read `tests/tools/test_panel_core.py:95-135` (the `_StubModule` whitelist loop) and `tests/tools/test_settings_dialog.py:55-130` (the stub whitelist + the override block at 120-125). Confirm the exact lines.

- [ ] **Step 2: Remove the 2 entries from test_panel_core.py**

Delete these two lines from the `_attr` list in the stub-install loop:

```python
        "DARK_THEME",
        "build_theme_stylesheet",
```

These entries existed only so the stub provided those names; once `styles.py` no longer has them, the stub shouldn't either.

- [ ] **Step 3: Remove entries + overrides from test_settings_dialog.py**

Delete the 2 whitelist lines (same as above, ~lines 69-70) and the override block (~lines 123-124):

```python
    _styles_mod.build_theme_stylesheet = lambda: ""
    _styles_mod.DARK_THEME = ""
```

- [ ] **Step 4: Verify tests still pass**

Run: `python -m pytest tests/tools/test_panel_core.py tests/tools/test_settings_dialog.py -v`
Expected: PASS (the symbols are still defined in styles.py at this point, so removing them from the stub is harmless — the stub just no longer declares them).

- [ ] **Step 5: Commit**

```bash
git add tests/tools/test_panel_core.py tests/tools/test_settings_dialog.py
git commit -m "test: drop stale DARK_THEME/build_theme_stylesheet stub refs"
```

---

### Task 4: Delete dead code (LIGHT_THEME, DARK_THEME, build_theme_stylesheet)

**Files:**
- Modify: `lucnhan/ui/styles.py` (delete lines 72-1252 for the two constants, 1450-1457 for build_theme_stylesheet)
- Modify: `lucnhan/ui/panel_core.py` (remove import line 46 + 2 call sites lines 531, 730)

**Interfaces:**
- Consumes: Task 3 (tests no longer reference the symbols)
- Produces: ~1180 lines removed; `styles.py` no longer carries dead QSS.

- [ ] **Step 1: Grep to confirm no remaining production references**

Run these and confirm ONLY the definition sites remain:
```bash
# Should show only styles.py:72, styles.py:664 (the definitions) and panel_core.py call sites
grep -rn "LIGHT_THEME\|DARK_THEME" lucnhan/ tests/
grep -rn "build_theme_stylesheet" lucnhan/ tests/
```
Expected: only `styles.py` definitions + `panel_core.py` call sites. If any other reference appears, stop and handle it first.

- [ ] **Step 2: Delete LIGHT_THEME and DARK_THEME from styles.py**

Delete the `LIGHT_THEME` constant (the `"""..."""` string at lines 72-661, including its leading comment header at line 70-71) and the `DARK_THEME` constant (lines 664-1252, including its comment header at line 663). Leave the module-global state helpers above (`_current_theme`, `set_current_theme`, `is_dark_theme`, `get_current_theme`, `is_host_theme`) and the re-export block + builders below intact.

- [ ] **Step 3: Delete build_theme_stylesheet from styles.py**

Delete the function definition (lines ~1450-1457):

```python
def build_theme_stylesheet(widget: object) -> str:
    """Build a minimal theme stylesheet for ``widget``.
    ...
    """
    return ""
```

- [ ] **Step 4: Remove the import + call sites from panel_core.py**

In `lucnhan/ui/panel_core.py`:
- Remove `build_theme_stylesheet` from the `from .styles import (...)` block (line 46 area).
- Remove the 2 call sites: `self.setStyleSheet(build_theme_stylesheet(self))` at line 531 (in `_build_ui`) and line 730 (in `_on_theme_changed`).

For line 730: since `build_theme_stylesheet` returned `""`, the call was a no-op. Just delete the line. For line 531: also just delete — setting an empty stylesheet is a no-op.

- [ ] **Step 5: Verify imports + full suite**

Run:
```bash
python -c "import lucnhan.ui.styles; import lucnhan.ui.panel_core; print('imports OK')"
python -m pytest tests/ -q
```
Expected: `imports OK` + full suite PASS (1597+ passed, same baseline minus any xfail churn).

- [ ] **Step 6: Verify lint + type**

Run:
```bash
python -m ruff check lucnhan/ui/styles.py lucnhan/ui/panel_core.py
python -m ruff format --check lucnhan/ui/styles.py lucnhan/ui/panel_core.py
python -m mypy lucnhan/core lucnhan/providers
```
Expected: ruff `All checks passed!`, format clean, mypy clean.

- [ ] **Step 7: Commit**

```bash
git add lucnhan/ui/styles.py lucnhan/ui/panel_core.py
git commit -m "refactor(ui): remove dead LIGHT_THEME/DARK_THEME + build_theme_stylesheet

~1180 lines of QSS constants that no function ever returned (build_theme_stylesheet
was a no-op returning ''). They predated the ThemeManager/token system and survived
only as confusing legacy. Removes the no-op call sites in panel_core too."
```

---

### Task 5: Final Phase 1 verification + merge prep

**Files:** none (verification only)

- [ ] **Step 1: Full ci-local.sh**

Run: `./ci-local.sh`
Expected: PASS (format + lint + mypy + pytest + desloppify). If desloppify score drops > 0.5, investigate (dead-code removal should not hurt objective score).

- [ ] **Step 2: Confirm dead code is gone**

Run:
```bash
grep -c "LIGHT_THEME\|DARK_THEME\|build_theme_stylesheet" lucnhan/ui/styles.py
```
Expected: `0`.

- [ ] **Step 3: Confirm line reduction**

Run: `wc -l lucnhan/ui/styles.py`
Expected: ~450 lines (down from 1628 — removed ~1180 lines).

- [ ] **Step 4: IDA visual final check (user)**

Open IDA, switch theme several times, confirm everything repaints correctly. This is the last visual gate before merge.

- [ ] **Step 5: Merge or hand off**

Either merge `feat/theme-phase1` to `master`, or hand off to the user to review the 5 commits + IDA verification before merging.

---

## Phases 2-4 (outline — separate plans when started)

These are intentionally NOT detailed here. Each gets its own plan created at the start of its phase, after Phase 1 is merged and verified. Per the spec (`docs/superpowers/specs/2026-06-19-theme-system-unification-design.md`):

**Phase 2 — Unify state:** Replace `_branch()` in the 5 `widgets_*.py` files to use `is_dark_tokens(ThemeManager.instance().tokens())` instead of `is_dark_theme()`. Remove `_effective_theme`, `is_dark_theme()`, `get_current_theme()` (KEEP `is_host_theme()`/`_current_theme`/`host_stylesheet` — they answer a different question). Simplify `set_current_theme` to single-arg. TDD the `_branch()` replacement.

**Phase 3 — Unify inline values:** Sweep 204 inline `setStyleSheet` calls across 13 files; replace hardcoded hex with `tokens.*`. One file per commit + IDA visual each. Start with `plan_view.py` (worst offender).

**Phase 4 — Optional root stylesheet paradigm shift:** Only if Phases 1-3 leave the codebase inconsistent. Hoist common styles into one root QSS from tokens via objectName selectors.

## Self-Review (run after writing — completed)

- **Spec coverage:** Phase 1 spec has 3 deliverables (bug fix, dead code removal, test migration) → Tasks 1-2 (bug fix), Task 3 (test migration), Task 4 (dead code), Task 5 (verification). All covered. ✓
- **Placeholder scan:** No TBD/TODO. All code steps show actual code. The only deliberate deferral is "the exact lines from the current body verbatim" in Task 1 Step 2 — that's an extract-refactor where the content is the existing code (shown by the Read step before it), not a placeholder. ✓
- **Type consistency:** `_reapply_minimal_style(self) -> None` used consistently in Task 1 (Produces) and Task 2 (Consumes). ✓
- **Test design:** Task 2 test is a genuine RED→GREEN for the subscription behavior, not a mock-behavior test. It asserts the *contract* (panel repaints on themeChanged) via the slot connection. ✓
