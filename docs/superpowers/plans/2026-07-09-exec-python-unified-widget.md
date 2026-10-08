# Execute Python Unified Widget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge the separate `ToolCallWidget` + `ToolApprovalWidget` pair into one `ExecutePythonWidget` for the `execute_python` tool, and replace the docs-review `TEXT_DELTA` messages with a 1-line status driven by a new `DOCS_GATE_STATUS` event.

**Architecture:** A new `DOCS_GATE_STATUS` `TurnEvent` carries docs-review state keyed by `tool_call_id`. `AgentLoop._review_complex_idapython_script()` emits it instead of `TEXT_DELTA`. A new `ExecutePythonWidget` owns the full lifecycle (code display → docs-gate status → approval buttons → result) and is routed by `ChatView` for `execute_python` both live and on history restore. Widget infers state from events (no auto-approve flag).

**Tech Stack:** Python 3.10+ (IDA safe), PySide6 (Qt6), pytest + unittest, IDA Pro 9.x host. Tests stub PySide6 via `tests/qt_stubs.py`.

## Global Constraints

- `from __future__ import annotations` at top of every modified `.py` module.
- All references to the tool name go through `lucnhan.constants.EXECUTE_PYTHON_TOOL_NAME` — never hardcode the string `"execute_python"`.
- Qt imports must come from `lucnhan.ui.qt_compat` (the single Qt import seam) — never import directly from `PySide6`.
- Host API imports (`ida_*`) use `importlib.import_module()` in `try/except ImportError` — N/A to this plan (no new IDA API usage).
- `execute_python` approval is NEVER auto-approved (security invariant). The widget shows buttons only when the loop emits `TOOL_APPROVAL_REQUEST`.
- Follow existing patterns: `@dataclass` for structured data, union types over ad-hoc protocols, f-strings for formatting, `f"0x{ea:x}"` for hex.
- Every test file starts with `from tests.qt_stubs import ensure_pyside6_stubs; ensure_pyside6_stubs()` before importing `lucnhan.ui.*` modules.

---

## File Structure

| File | Responsibility |
|------|---------------|
| `lucnhan/agent/turn.py` | Add `DOCS_GATE_STATUS` enum value + `docs_gate_status()` factory method |
| `lucnhan/agent/loop.py` | Change `_review_complex_idapython_script` to emit `DOCS_GATE_STATUS` instead of `TEXT_DELTA`; change FAILED path to fall-through; blank `_describe_tool_call` for execute_python |
| `lucnhan/ui/tool_widgets.py` | Add `ExecutePythonWidget` class (new); keep `ToolCallWidget` / `ToolApprovalWidget` unchanged for other tools |
| `lucnhan/ui/chat_view.py` | Route `execute_python` to `ExecutePythonWidget` (live + restore); add `DOCS_GATE_STATUS` handler; route `TOOL_APPROVAL_REQUEST` into existing widget; widen `_tool_widgets` type hint |
| `tests/tools/test_execute_python_widget.py` | NEW — unit tests for `ExecutePythonWidget` |
| `tests/test_idapython_docs_gate.py` | UPDATE — assert `DOCS_GATE_STATUS` events, FAILED fall-through |
| `tests/tools/test_tool_widget_logic.py` | UPDATE — add `docs_gate_status` factory test if event factory is tested here (or in `tests/agent/test_turn_events.py`) |

---

## Task 1: Add `DOCS_GATE_STATUS` event type and factory

**Files:**
- Modify: `lucnhan/agent/turn.py:12-45` (enum), `lucnhan/agent/turn.py:63-178` (factory area)
- Test: `tests/agent/test_turn_events.py`

**Interfaces:**
- Produces: `TurnEventType.DOCS_GATE_STATUS` (enum), `TurnEvent.docs_gate_status(tool_call_id, state, reasons=(), summary="")` factory returning a `TurnEvent` with `metadata = {"docs_gate_state": str, "docs_gate_reasons": list[str], "docs_gate_summary": str}` and `tool_call_id` set.

- [ ] **Step 1: Write the failing test**

Append to `tests/agent/test_turn_events.py`:

```python
class TestDocsGateStatusEvent(unittest.TestCase):
    def test_factory_sets_metadata_and_tool_call_id(self):
        ev = TurnEvent.docs_gate_status(
            tool_call_id="abc123",
            state="running",
            reasons=("2 IDA modules", "14 non-comment lines"),
        )
        self.assertEqual(ev.type, TurnEventType.DOCS_GATE_STATUS)
        self.assertEqual(ev.tool_call_id, "abc123")
        self.assertEqual(ev.metadata["docs_gate_state"], "running")
        self.assertEqual(
            ev.metadata["docs_gate_reasons"],
            ["2 IDA modules", "14 non-comment lines"],
        )
        self.assertEqual(ev.metadata["docs_gate_summary"], "")

    def test_factory_defaults_empty_reasons_and_summary(self):
        ev = TurnEvent.docs_gate_status(tool_call_id="x", state="approved")
        self.assertEqual(ev.metadata["docs_gate_reasons"], [])
        self.assertEqual(ev.metadata["docs_gate_summary"], "")

    def test_factory_blocked_with_summary(self):
        ev = TurnEvent.docs_gate_status(
            tool_call_id="x",
            state="blocked",
            summary="ida_bytes.patch_qword does not exist",
        )
        self.assertEqual(ev.metadata["docs_gate_state"], "blocked")
        self.assertEqual(
            ev.metadata["docs_gate_summary"],
            "ida_bytes.patch_qword does not exist",
        )
```

Add `from lucnhan.agent.turn import TurnEvent, TurnEventType` to the imports at the top of the test file if not already present.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/agent/test_turn_events.py::TestDocsGateStatusEvent -v`
Expected: FAIL — `AttributeError: type object 'TurnEventType' has no attribute 'DOCS_GATE_STATUS'`

- [ ] **Step 3: Add the enum value**

In `lucnhan/agent/turn.py`, add to the `TurnEventType` enum (after `KNOWLEDGE_RETRIEVED`, around line 44):

```python
    DOCS_GATE_STATUS = "docs_gate_status"
```

- [ ] **Step 4: Add the factory method**

In `lucnhan/agent/turn.py`, add a new static method to the `TurnEvent` dataclass (after `tool_approval_request`, around line 178):

```python
    @staticmethod
    def docs_gate_status(
        tool_call_id: str,
        state: str,
        reasons: tuple[str, ...] = (),
        summary: str = "",
    ) -> TurnEvent:
        """Emit a docs-review gate status update for an execute_python call.

        ``state`` is one of ``running`` | ``approved`` | ``blocked`` | ``failed``.
        This is a UI-only signal — it is NOT serialized into assistant text
        or history.  ``reasons`` are the complexity reasons (for ``running``);
        ``summary`` is the reviewer summary (for ``blocked`` / ``failed``).
        """
        return TurnEvent(
            type=TurnEventType.DOCS_GATE_STATUS,
            tool_call_id=tool_call_id,
            metadata={
                "docs_gate_state": state,
                "docs_gate_reasons": list(reasons),
                "docs_gate_summary": summary,
            },
        )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/agent/test_turn_events.py::TestDocsGateStatusEvent -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add lucnhan/agent/turn.py tests/agent/test_turn_events.py
git commit -m "feat(agent): add DOCS_GATE_STATUS event type and factory"
```

---

## Task 2: Loop emits `DOCS_GATE_STATUS` instead of `TEXT_DELTA`; FAILED falls through

**Files:**
- Modify: `lucnhan/agent/loop.py:1164-1168` (gate-fire message), `lucnhan/agent/loop.py:1191-1199` (exception → fall-through), `lucnhan/agent/loop.py:1219-1231` (verdict APPROVED/blocked messages)
- Test: `tests/test_idapython_docs_gate.py`

**Interfaces:**
- Consumes: `TurnEvent.docs_gate_status()` from Task 1.
- Produces: no new public API — internal change to `_review_complex_idapython_script` event emission and return value on exception.

- [ ] **Step 1: Read current code to confirm line numbers**

Run: `python3 -m pytest tests/test_idapython_docs_gate.py -v --co -q` to confirm the existing test file collects. Then read `lucnhan/agent/loop.py` lines 1140-1235 to confirm the three emission sites match the task's "Modify" line ranges (they may have drifted).

- [ ] **Step 2: Write the failing test for event type**

Append to `tests/test_idapython_docs_gate.py` a new test class that drives the full review path and asserts the emitted event types. First check the existing test class name for the integration test (e.g. `TestReviewComplexScript`) by reading the file tail, then add:

```python
class TestDocsGateStatusEmission(unittest.TestCase):
    """The review path emits DOCS_GATE_STATUS, never TEXT_DELTA."""

    def _collect_events(self, script: str, verdict_text: str):
        """Run _review_complex_idapython_script and capture all yielded events.

        Returns the list of TurnEvent objects and the (approved, summary) tuple.
        """
        from lucnhan.agent.loop import AgentLoop
        from lucnhan.agent.turn import TurnEvent, TurnEventType
        from lucnhan.core.types import ToolCall
        from lucnhan.tools.idapython_complexity import (
            classify_idapython_script,
        )
        from lucnhan.tools.validate_idapython import validate_idapython

        loop = _build_minimal_loop(verdict_text=verdict_text)
        tc = ToolCall(id="tc1", name="execute_python", arguments={"code": script})
        validation = validate_idapython(script)
        complexity = classify_idapython_script(script, validation)

        gen = loop._review_complex_idapython_script(tc, complexity, validation)
        events: list[TurnEvent] = []
        try:
            while True:
                events.append(next(gen))
        except StopIteration as stop:
            result = stop.value  # (approved, summary)

        event_types = [e.type for e in events]
        return events, event_types, result

    def test_approved_emits_docs_gate_status_not_text_delta(self):
        # A complex script + APPROVED verdict.
        script = "import idautils\nimport idc\nfor ea in idautils.Functions():\n    print(ea)\n" * 3
        events, types, result = self._collect_events(script, "VERDICT: APPROVED\nLooks good.")
        self.assertIn(TurnEventType.DOCS_GATE_STATUS, types)
        self.assertNotIn(TurnEventType.TEXT_DELTA, types)
        self.assertTrue(result[0])  # approved

    def test_running_state_emitted_before_verdict(self):
        script = "import idautils\nimport idc\nfor ea in idautils.Functions():\n    print(ea)\n" * 3
        events, types, result = self._collect_events(script, "VERDICT: APPROVED")
        # The first DOCS_GATE_STATUS should be state=running.
        gate_events = [e for e in events if e.type == TurnEventType.DOCS_GATE_STATUS]
        self.assertTrue(len(gate_events) >= 2)
        self.assertEqual(gate_events[0].metadata["docs_gate_state"], "running")
        self.assertEqual(gate_events[-1].metadata["docs_gate_state"], "approved")

    def test_blocked_emits_blocked_state_and_returns_false(self):
        script = "import idautils\nimport idc\nfor ea in idautils.Functions():\n    print(ea)\n" * 3
        events, types, result = self._collect_events(script, "VERDICT: REWRITE_REQUIRED\nBad API.")
        gate_events = [e for e in events if e.type == TurnEventType.DOCS_GATE_STATUS]
        self.assertTrue(any(e.metadata["docs_gate_state"] == "blocked" for e in gate_events))
        self.assertFalse(result[0])  # not approved

    def test_reviewer_exception_emits_failed_and_falls_through(self):
        """Behavior change: reviewer crash now returns (True, '') to fall
        through to user approval instead of hard-blocking."""
        loop = _build_minimal_loop(raise_on_run=ValueError("boom"))
        from lucnhan.agent.turn import TurnEventType
        from lucnhan.core.types import ToolCall
        from lucnhan.tools.idapython_complexity import classify_idapython_script
        from lucnhan.tools.validate_idapython import validate_idapython

        script = "import idautils\nimport idc\nfor ea in idautils.Functions():\n    print(ea)\n" * 3
        tc = ToolCall(id="tc2", name="execute_python", arguments={"code": script})
        validation = validate_idapython(script)
        complexity = classify_idapython_script(script, validation)

        gen = loop._review_complex_idapython_script(tc, complexity, validation)
        events: list = []
        try:
            while True:
                events.append(next(gen))
        except StopIteration as stop:
            result = stop.value

        gate_events = [e for e in events if e.type == TurnEventType.DOCS_GATE_STATUS]
        self.assertTrue(any(e.metadata["docs_gate_state"] == "failed" for e in gate_events))
        # Behavior change: returns (True, "") so caller falls through to
        # _wait_for_approval instead of hard-blocking.
        self.assertTrue(result[0])
        self.assertEqual(result[1], "")
```

Also add/confirm the `_build_minimal_loop` helper exists near the top of the file (after imports). If a similar helper already exists (check the existing integration test class), extend it; otherwise add:

```python
@dataclass
class _FakeProvider:
    """Streams a canned reviewer verdict (or raises)."""
    verdict_text: str = "VERDICT: APPROVED"
    raise_on_run: Exception | None = None

    def stream(self, *a, **k):
        raise NotImplementedError  # not used by the reviewer path


def _build_minimal_loop(verdict_text: str = "VERDICT: APPROVED", raise_on_run=None):
    """Build an AgentLoop with just enough wiring for _review_complex_idapython_script.

    The reviewer subagent is driven by SubagentRunner which calls the
    provider; we monkeypatch SubagentRunner.run_task to yield the canned
    verdict or raise.
    """
    from unittest.mock import MagicMock
    from lucnhan.agent.loop import AgentLoop

    loop = MagicMock(spec=AgentLoop)
    loop._DOCS_GATE_VERDICT_PREFIX = AgentLoop._DOCS_GATE_VERDICT_PREFIX

    # _review_complex_idapython_script builds a SubagentRunner inline and
    # calls runner.run_task(...). We patch the class so any instance returns
    # our canned value.
    import lucnhan.agent.loop as loop_mod

    def _fake_run_task(self, task, **kwargs):
        if raise_on_run is not None:
            raise raise_on_run
        return verdict_text

    loop_mod.SubagentRunner.run_task = _fake_run_task
    loop.session = MagicMock()
    loop.session.metadata = {}
    return loop
```

Note: `_review_complex_idapython_script` references `self.session.metadata`, `self.provider`, `self.tools`, `self.config`, `self.host_name`, `self.skills`, and `self` for `SubagentRunner(parent_loop=self)`. The MagicMock provides attribute access; `SubagentRunner.run_task` is patched at class level so the parent_loop wiring does not matter. If the method body accesses other attributes, extend the helper — read the method body (lines 1119-1231) to confirm.

- [ ] **Step 3: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_idapython_docs_gate.py::TestDocsGateStatusEmission -v`
Expected: FAIL — `TEXT_DELTA` is still in `types` (current code emits it); `test_reviewer_exception_emits_failed_and_falls_through` fails because current code returns `(False, msg)`.

- [ ] **Step 4: Replace the gate-fire message**

In `lucnhan/agent/loop.py`, find the `_review_complex_idapython_script` method. Replace the "Notify the chat that the gate is firing" `TEXT_DELTA` block (around lines 1164-1168):

```python
        # Notify the chat that the gate is firing.
        yield TurnEvent.docs_gate_status(
            tc.id,
            state="running",
            reasons=complexity.reasons,
        )
```

- [ ] **Step 5: Change FAILED (exception) to fall-through**

In the same method, replace the `except Exception as e:` block (around lines 1191-1199). Change it to emit a `failed` status and return `(True, "")` so the caller proceeds to user approval:

```python
        except Exception as e:
            log_error(f"docs reviewer failed: {e}")
            yield TurnEvent.docs_gate_status(
                tc.id,
                state="failed",
                summary=f"{type(e).__name__}: {e}",
            )
            # Behavior change: reviewer crash is an infrastructure fault,
            # not a script fault. Fall through to user approval so the
            # user can still decide. Return (True, "") — caller proceeds
            # to _wait_for_approval.
            return (True, "")
```

- [ ] **Step 6: Replace the verdict APPROVED/blocked messages**

In the same method, replace the final verdict rendering (around lines 1219-1231). Find the `if approved:` block and the `reason_msg` block. Replace both `yield TurnEvent.text_delta(...)` calls with `docs_gate_status`:

```python
        if approved:
            yield TurnEvent.docs_gate_status(tc.id, state="approved")
            return (True, summary or "")

        reason_msg = verdict or "REWRITE_REQUIRED"
        yield TurnEvent.docs_gate_status(
            tc.id,
            state="blocked",
            summary=summary or reason_msg,
        )
        return (
            False,
            f"IDA docs review verdict: {reason_msg}. "
            "The script was NOT executed. "
            "Review the reviewer's REWRITE_GUIDANCE and resubmit a corrected script.\n\n"
            f"--- Reviewer summary ---\n{summary or '(no summary returned)'}\n--- end ---",
        )
```

Note: the returned `(False, msg)` for blocked keeps the existing caller behavior (hard block → tool error result). Only the FAILED path changed to `(True, "")`.

- [ ] **Step 7: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_idapython_docs_gate.py -v`
Expected: PASS — all existing tests + 4 new tests green.

- [ ] **Step 8: Run full agent test suite to check for regressions**

Run: `python3 -m pytest tests/test_idapython_docs_gate.py tests/agent/test_agent_loop.py tests/agent/test_turn_events.py -v`
Expected: PASS — no regressions. If an existing test asserts `TEXT_DELTA` was emitted by the review path, update it to assert `DOCS_GATE_STATUS` instead (search: `grep -rn "IDA docs review" tests/`).

- [ ] **Step 9: Commit**

```bash
git add lucnhan/agent/loop.py tests/test_idapython_docs_gate.py
git commit -m "refactor(agent): emit DOCS_GATE_STATUS instead of TEXT_DELTA for docs gate

- Gate-fire, approved, blocked states now emit DOCS_GATE_STATUS
  (UI-only signal) instead of TEXT_DELTA that mixed into the
  assistant bubble and was persisted to history.
- Behavior change: reviewer exception (FAILED) now falls through to
  user approval (returns True) instead of hard-blocking — a subagent
  crash is an infrastructure fault, not a script fault."
```

---

## Task 3: Blank `_describe_tool_call` for execute_python

**Files:**
- Modify: `lucnhan/agent/loop.py:1056-1064` (`_describe_tool_call`)
- Test: `tests/test_idapython_docs_gate.py` (or `tests/agent/test_agent_loop.py`)

**Interfaces:**
- Produces: `_describe_tool_call` returns `""` for `EXECUTE_PYTHON_TOOL_NAME` (the new widget renders its own code, so the description that duplicated the first line is gone).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_idapython_docs_gate.py`:

```python
class TestDescribeToolCallExecutePython(unittest.TestCase):
    def test_execute_python_returns_empty_description(self):
        from lucnhan.agent.loop import AgentLoop
        from lucnhan import constants

        desc = AgentLoop._describe_tool_call(
            constants.EXECUTE_PYTHON_TOOL_NAME,
            {"code": "import idautils\nprint(1)\n"},
        )
        self.assertEqual(desc, "")

    def test_other_mutating_tool_still_described(self):
        from lucnhan.agent.loop import AgentLoop

        desc = AgentLoop._describe_tool_call(
            "rename_function",
            {"old_name": "sub_1000", "new_name": "process_data"},
        )
        self.assertIn("sub_1000", desc)
        self.assertIn("process_data", desc)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_idapython_docs_gate.py::TestDescribeToolCallExecutePython -v`
Expected: FAIL — current code returns `"Run Python code:\n..."`.

- [ ] **Step 3: Modify `_describe_tool_call`**

In `lucnhan/agent/loop.py`, at the top of `_describe_tool_call` (around line 1056), change the `execute_python` branch to return empty:

```python
    @staticmethod
    def _describe_tool_call(name: str, args: dict[str, Any]) -> str:
        """Generate a brief human-readable description of what a tool will do."""
        if name == constants.EXECUTE_PYTHON_TOOL_NAME:
            # The ExecutePythonWidget renders its own code block, so a
            # description here would duplicate the first line. Return empty.
            return ""
        if name in ("rename_function",):
```

(Keep the rest of the method unchanged — it already early-returns for execute_python, so just replace the body of that first `if`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_idapython_docs_gate.py::TestDescribeToolCallExecutePython -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add lucnhan/agent/loop.py tests/test_idapython_docs_gate.py
git commit -m "refactor(agent): blank _describe_tool_call for execute_python

The unified ExecutePythonWidget renders its own code block, so the
description that duplicated the first line of code is no longer needed."
```

---

## Task 4: Create `ExecutePythonWidget` class

**Files:**
- Create: `lucnhan/ui/tool_widgets.py` (append class at end of file)
- Test: `tests/tools/test_execute_python_widget.py` (NEW)

**Interfaces:**
- Consumes (from existing code): `_PythonHighlighter` (line 939), `_build_approval_header` (line 171), `_extract_code`-style JSON parsing, `get_tool_colors()`, `get_tool_approval_*_style()` from `lucnhan.ui.styles`.
- Produces: `ExecutePythonWidget(QFrame)` with:
  - `Signal approved = Signal(str, str)` — `(tool_call_id, "allow" | "allow_all" | "deny")`
  - `__init__(self, tool_call_id: str, parent: QWidget | None = None)`
  - `set_arguments(self, args_text: str) -> None` — parse JSON, extract code, call `set_code`
  - `set_code(self, code: str) -> None`
  - `set_docs_gate_status(self, state: str, reasons: tuple[str, ...] = (), summary: str = "") -> None`
  - `show_approval_buttons(self) -> None`
  - `mark_done(self) -> None`
  - `hide_preview(self) -> None`
  - `set_result(self, result: str, is_error: bool = False) -> None`

- [ ] **Step 1: Write the failing test file**

Create `tests/tools/test_execute_python_widget.py`:

```python
"""Tests for ExecutePythonWidget (unified execute_python lifecycle widget)."""

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
    def test_init_idle_no_buttons_code_collapsed(self):
        w = ExecutePythonWidget("tc1")
        # No code set yet.
        self.assertEqual(w._code, "")
        # Buttons should not be shown until show_approval_buttons().
        self.assertFalse(w._buttons_visible)
        # Result block should be hidden until set_result().
        self.assertFalse(w._result_block_visible)


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
        self.assertIn("bad API", w._status_text)

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
        self.assertTrue(w._result_block_visible)
        self.assertFalse(w._is_error)

    def test_set_result_error_marks_error(self):
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)")
        w.set_result("NameError: x", is_error=True)
        self.assertTrue(w._result_block_visible)
        self.assertTrue(w._is_error)


class TestMarkDone(unittest.TestCase):
    def test_mark_done_is_safe_to_call(self):
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)")
        # mark_done must not raise whether or not result is set.
        w.mark_done()
        w.set_result("ok", is_error=False)
        w.mark_done()


class TestHidePreview(unittest.TestCase):
    def test_hide_preview_collapses_code(self):
        w = ExecutePythonWidget("tc1")
        w.set_code("print(1)\nprint(2)\n")
        w.hide_preview()
        # After hide_preview the code editor should be collapsed.
        self.assertFalse(w._code_expanded)


class TestCodeDisplayedOnce(unittest.TestCase):
    def test_no_redundant_description_label(self):
        """The widget must not carry a redundant 'Run Python code: ...'
        description — code is shown once in the code editor."""
        w = ExecutePythonWidget("tc1")
        w.set_arguments(json.dumps({"code": "import idautils\nprint(1)\n"}))
        # There should be no _description_label attribute holding a
        # duplicate of the first code line.
        self.assertFalse(getattr(w, "_description_label", None))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/tools/test_execute_python_widget.py -v`
Expected: FAIL — `ImportError: cannot import name 'ExecutePythonWidget' from 'lucnhan.ui.tool_widgets'`.

- [ ] **Step 3: Implement `ExecutePythonWidget`**

Append to the END of `lucnhan/ui/tool_widgets.py` (after the existing `ToolApprovalWidget` class). Use these imports — they are already at the top of the file (`QFrame`, `QHBoxLayout`, `QLabel`, `QPlainTextEdit`, `QToolButton`, `QVBoxLayout`, `QWidget`, `Signal`, `Qt`, `json`, and the style helpers). Add `constants` import is already present (line 10).

```python
class ExecutePythonWidget(QFrame):
    """Unified lifecycle widget for the ``execute_python`` tool.

    Renders code, an optional docs-review status line, approval buttons,
    and the execution result — all in one card.  State is inferred from
    the events received (no auto-approve flag): the widget starts IDLE,
    shows buttons only when ``show_approval_buttons()`` is called (driven
    by TOOL_APPROVAL_REQUEST), and shows the result after ``set_result()``.
    """

    approved = Signal(str, str)  # (tool_call_id, "allow"/"allow_all"/"deny")

    def __init__(self, tool_call_id: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("message_tool")
        self._tool_call_id = tool_call_id
        self._code = ""
        self._code_expanded = False
        self._buttons_visible = False
        self._status_visible = False
        self._status_text = ""
        self._result_block_visible = False
        self._is_error = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.setSpacing(2)

        layout.addLayout(self._build_header())
        layout.addWidget(self._build_code_section())
        layout.addWidget(self._build_status_line())
        layout.addLayout(self._build_approval_buttons())
        layout.addWidget(self._build_result_block())

        self._apply_card_style()
        ThemeManager.instance().themeChanged.connect(self._apply_card_style)

    # ------------------------------------------------------------------
    # Builders
    # ------------------------------------------------------------------

    def _apply_card_style(self, _tokens: object = None) -> None:
        self.setStyleSheet(_tool_card_css())

    def _build_header(self) -> QHBoxLayout:
        tool_colors = get_tool_colors()
        color = _tool_color(constants.EXECUTE_PYTHON_TOOL_NAME)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(0)

        self._toggle_btn = QToolButton()
        self._toggle_btn.setObjectName("collapse_button")
        self._toggle_btn.setText("▶")
        self._toggle_btn.setFixedSize(14, 14)
        self._toggle_btn.clicked.connect(self._toggle_code)
        header.addWidget(self._toggle_btn)

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

        section.setVisible(False)
        return section

    def _build_status_line(self) -> QLabel:
        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.setVisible(False)
        return self._status_label

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
        layout.addWidget(self._result_header_label)

        self._result_label = _HeightCachedLabel()
        self._result_label.setObjectName("tool_content")
        self._result_label.setWordWrap(True)
        self._result_label.setTextInteractionFlags(
            Qt.TextInteractionFlag(
                Qt.TextInteractionFlag.TextSelectableByMouse.value
                | Qt.TextInteractionFlag.TextSelectableByKeyboard.value
            )
        )
        layout.addWidget(self._result_label)

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
            self._code_info_label.setText(
                f"Python code — {len(lines)} line{'s' if len(lines) != 1 else ''}"
            )
            self._code_info_label.setVisible(True)
            visible = min(len(lines), 15)
            line_height = self._code_edit.fontMetrics().lineSpacing()
            self._code_edit.setFixedHeight(line_height * visible + 16)
        self._code_section().setVisible(True)
        # Default: collapsed (show only when IDLE/auto-allow path).
        self._set_code_expanded(self._code_expanded)

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
            self._status_text = "🔍 Reviewing script..."
            if reasons:
                self._status_text += f" (complex: {', '.join(reasons[:3])})"
            self._status_label.setStyleSheet(
                f"color: {tool_colors['preview']}; font-size: inherit;"
            )
            self._status_icon.setText("⟳")
        elif state == "approved":
            self._status_text = "✓ Docs review passed"
            self._status_label.setStyleSheet(
                f"color: {tool_colors['status_success']}; font-size: inherit; opacity: 0.7;"
            )
            self._status_icon.setText("✓")
        elif state == "blocked":
            self._status_text = f"✗ Docs review blocked: {summary}"
            self._status_label.setStyleSheet(
                f"color: {tool_colors['status_error']}; font-weight: bold; font-size: inherit;"
            )
            self._status_icon.setText("✗")
            self._buttons_visible = False
            self._buttons_container.setVisible(False)
        elif state == "failed":
            self._status_text = f"⚠ Docs review error — review manually. ({summary})"
            self._status_label.setStyleSheet(
                f"color: {tool_colors['status_spinner']}; font-size: inherit;"
            )
            self._status_icon.setText("⚠")
            # FAILED keeps buttons visible so the user can still approve.
        else:
            self._status_text = ""
            self._status_visible = False

        self._status_label.setText(self._status_text)
        self._status_label.setVisible(self._status_visible)

    def show_approval_buttons(self) -> None:
        if not self._status_visible or not self._status_text.startswith("✗"):
            # Keep buttons hidden if currently hard-blocked by docs gate.
            self._buttons_visible = True
            self._buttons_container.setVisible(True)
        # Expand code so the user can review before deciding.
        self._set_code_expanded(True)

    def mark_done(self) -> None:
        """Mark the call complete (used by history restore). Safe to call
        multiple times."""
        if self._status_icon.text() not in ("✓", "✗"):
            tool_colors = get_tool_colors()
            self._status_icon.setText("✓")
            self._status_icon.setStyleSheet(
                f"color: {tool_colors['status_success']}; font-size: inherit;"
            )

    def hide_preview(self) -> None:
        """Collapse the code editor (used by tool grouping)."""
        self._set_code_expanded(False)

    def set_result(self, result: str, is_error: bool = False) -> None:
        tool_colors = get_tool_colors()
        self._is_error = is_error
        self._result_block_visible = True
        display = (
            result[:_MAX_RESULT_DISPLAY] + "\n... (truncated)"
            if len(result) > _MAX_RESULT_DISPLAY
            else result
        )
        self._result_label.setText(display)
        self._result_label.pin_height()
        self._result_block.setVisible(True)
        # Hide approval buttons after result arrives.
        self._buttons_visible = False
        self._buttons_container.setVisible(False)

        if is_error:
            self._result_label.setStyleSheet(
                f"color: {tool_colors['status_error']}; font-size: inherit;"
            )
            self._status_icon.setText("✗")
            self._status_icon.setStyleSheet(
                f"color: {tool_colors['status_error']}; font-size: inherit;"
            )
            self._bullet.setStyleSheet(
                f"color: {tool_colors['status_error']}; font-size: inherit;"
            )
        else:
            self._result_label.setStyleSheet(
                f"color: {tool_colors['preview']}; font-size: inherit;"
            )
            self._status_icon.setText("✓")
            self._status_icon.setStyleSheet(
                f"color: {tool_colors['status_success']}; font-size: inherit;"
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _code_section(self) -> QWidget:
        # The code section is the 2nd widget in the main layout.
        return self.layout().itemAt(1).widget()

    def _set_code_expanded(self, expanded: bool) -> None:
        self._code_expanded = expanded
        self._code_edit.setVisible(expanded)
        self._code_info_label.setVisible(expanded and bool(self._code))
        self._toggle_btn.setText("▼" if expanded else "▶")

    def _toggle_code(self) -> None:
        self._set_code_expanded(not self._code_expanded)

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

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/tools/test_execute_python_widget.py -v`
Expected: PASS (all tests). If a test fails because a helper (e.g. `_tool_card_css`, `_tool_color`) is not defined or has a different name, grep for the correct name: `grep -n "_tool_card_css\|def _tool_color\|_HeightCachedLabel" lucnhan/ui/tool_widgets.py` and adjust the implementation to match the real names.

- [ ] **Step 5: Verify imports resolve**

Run: `python3 -c "from tests.qt_stubs import ensure_pyside6_stubs; ensure_pyside6_stubs(); import lucnhan.ui.tool_widgets; print('OK', hasattr(lucnhan.ui.tool_widgets, 'ExecutePythonWidget'))"`
Expected: `OK True`

- [ ] **Step 6: Commit**

```bash
git add lucnhan/ui/tool_widgets.py tests/tools/test_execute_python_widget.py
git commit -m "feat(ui): add ExecutePythonWidget unified lifecycle widget

Renders code, docs-review status, approval buttons, and result in one
card. State is inferred from events — no auto-approve flag. Hard-blocks
hide buttons on docs-gate BLOCKED; keeps them on FAILED."
```

---

## Task 5: Route `execute_python` to `ExecutePythonWidget` in ChatView (live events)

**Files:**
- Modify: `lucnhan/ui/chat_view.py:505` (type hint), `lucnhan/ui/chat_view.py:930-966` (`_handle_tool_event`), `lucnhan/ui/chat_view.py:751-790` (event dispatch — add `DOCS_GATE_STATUS`)
- Test: `tests/tools/test_chat_view.py` (or `tests/ui/`)

**Interfaces:**
- Consumes: `ExecutePythonWidget` from Task 4, `DOCS_GATE_STATUS` from Task 1.
- Produces: ChatView routes `execute_python` calls to `ExecutePythonWidget`, routes `DOCS_GATE_STATUS` into it, and routes `TOOL_APPROVAL_REQUEST` for `execute_python` into the existing widget instead of creating a new `ToolApprovalWidget`.

- [ ] **Step 1: Read the event dispatch method**

Read `lucnhan/ui/chat_view.py` lines 751-800 (the `handle_event` dispatch) and 930-966 (`_handle_tool_event`) to confirm exact structure. The dispatch likely has `if etype in (...): self._handle_tool_event(event)` — confirm `TOOL_RESULT` and `TOOL_APPROVAL_REQUEST` are dispatched there.

- [ ] **Step 2: Write the failing test**

Append to `tests/tools/test_chat_view.py` (read the file header first to match its existing import/setup pattern). If the file already installs qt stubs, match that; otherwise add the standard stub block:

```python
class TestExecutePythonRouting(unittest.TestCase):
    """ChatView routes execute_python to ExecutePythonWidget."""

    def setUp(self):
        from lucnhan.ui.chat_view import ChatView
        from lucnhan.agent.turn import TurnEvent, TurnEventType
        from lucnhan import constants
        self.ChatView = ChatView
        self.TurnEvent = TurnEvent
        self.TurnEventType = TurnEventType
        self.EXEC_PY = constants.EXECUTE_PYTHON_TOOL_NAME

    def _make_view(self):
        view = self.ChatView.__new__(self.ChatView)
        # Minimal init to avoid Qt container setup.
        view._tool_widgets = {}
        view._tool_run_ids = []
        view._tool_run_names = []
        view._tool_run_widgets = []
        view._tool_group = None
        view._group_map = {}
        view._current_assistant = None
        return view

    def test_tool_call_start_creates_execute_python_widget(self):
        view = self._make_view()
        ev = self.TurnEvent.tool_call_start("tc1", self.EXEC_PY)
        view._handle_tool_event(ev)
        from lucnhan.ui.tool_widgets import ExecutePythonWidget
        self.assertIsInstance(view._tool_widgets["tc1"], ExecutePythonWidget)

    def test_other_tool_still_uses_tool_call_widget(self):
        view = self._make_view()
        ev = self.TurnEvent.tool_call_start("tc2", "rename_function")
        view._handle_tool_event(ev)
        from lucnhan.ui.tool_widgets import ToolCallWidget
        self.assertIsInstance(view._tool_widgets["tc2"], ToolCallWidget)

    def test_tool_call_done_sets_code(self):
        view = self._make_view()
        view._handle_tool_event(self.TurnEvent.tool_call_start("tc1", self.EXEC_PY))
        import json
        view._handle_tool_event(
            self.TurnEvent.tool_call_done("tc1", self.EXEC_PY, json.dumps({"code": "print(1)"}))
        )
        self.assertEqual(view._tool_widgets["tc1"]._code, "print(1)")

    def test_docs_gate_status_routes_to_widget(self):
        view = self._make_view()
        view._handle_tool_event(self.TurnEvent.tool_call_start("tc1", self.EXEC_PY))
        ev = self.TurnEvent.docs_gate_status("tc1", "running", reasons=("2 IDA modules",))
        view.handle_event(ev)
        self.assertIn("Reviewing", view._tool_widgets["tc1"]._status_text)

    def test_approval_request_routes_into_existing_widget(self):
        """TOOL_APPROVAL_REQUEST for execute_python must NOT create a new
        ToolApprovalWidget — it routes into the existing ExecutePythonWidget."""
        view = self._make_view()
        view._handle_tool_event(self.TurnEvent.tool_call_start("tc1", self.EXEC_PY))
        ev = self.TurnEvent.tool_approval_request("tc1", self.EXEC_PY, '{"code":"x"}', "")
        view._handle_tool_event(ev)
        from lucnhan.ui.tool_widgets import ExecutePythonWidget
        self.assertIsInstance(view._tool_widgets["tc1"], ExecutePythonWidget)
        self.assertTrue(view._tool_widgets["tc1"]._buttons_visible)
```

Note: `_make_view` bypasses `__init__` to avoid Qt container construction. If `_handle_tool_event` calls methods that need more attributes (e.g. `_insert_widget`, `_scroll_to_bottom`), extend `_make_view` to stub them: `view._insert_widget = lambda w: None; view._scroll_to_bottom = lambda: None; view._hide_thinking = lambda: None; view._reset_tool_run = lambda: None`. Read the method body first to list what's needed.

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/tools/test_chat_view.py::TestExecutePythonRouting -v`
Expected: FAIL — `execute_python` still creates `ToolCallWidget` (current code).

- [ ] **Step 4: Update the type hint**

In `lucnhan/ui/chat_view.py` line 505, change:

```python
        self._tool_widgets: dict[str, ToolCallWidget] = {}
```
to:
```python
        self._tool_widgets: dict[str, ToolCallWidget | ExecutePythonWidget] = {}
```

Ensure `ExecutePythonWidget` is imported at the top of `chat_view.py`. Find the existing `from .tool_widgets import ...` line (search `grep -n "from .tool_widgets import" lucnhan/ui/chat_view.py`) and add `ExecutePythonWidget` to the import list.

- [ ] **Step 5: Route `TOOL_CALL_START`**

In `_handle_tool_event` (`chat_view.py:932-937`), change the `TOOL_CALL_START` branch:

```python
        if etype == TurnEventType.TOOL_CALL_START:
            self._hide_thinking()
            if event.tool_name == constants.EXECUTE_PYTHON_TOOL_NAME:
                tw: ToolCallWidget | ExecutePythonWidget = ExecutePythonWidget(event.tool_call_id)
            else:
                tw = ToolCallWidget(event.tool_name, event.tool_call_id)
            self._tool_widgets[event.tool_call_id] = tw
            self._register_tool_widget(event.tool_name, event.tool_call_id, tw)
            self._scroll_to_bottom()
```

Ensure `constants` is imported in `chat_view.py` (search `grep -n "from .. import constants\|import constants" lucnhan/ui/chat_view.py`; if missing add `from .. import constants`).

- [ ] **Step 6: Route `TOOL_CALL_DONE` with code**

In the `TOOL_CALL_DONE` branch (`chat_view.py:942-945`), keep calling `set_arguments` (it now works for both widgets — `ExecutePythonWidget.set_arguments` parses code):

```python
        elif etype == TurnEventType.TOOL_CALL_DONE:
            existing_tw = self._tool_widgets.get(event.tool_call_id)
            if existing_tw is not None:
                existing_tw.set_arguments(event.tool_args)
```

No change needed here — `set_arguments` is polymorphic. Verify `ToolCallWidget.set_arguments` signature still accepts a single string arg (it does — line 610).

- [ ] **Step 7: Route `TOOL_APPROVAL_REQUEST` into existing widget**

In the `TOOL_APPROVAL_REQUEST` branch (`chat_view.py:955-966`), add a check: if an `ExecutePythonWidget` already exists for this `tool_call_id`, call `show_approval_buttons()` instead of creating a new `ToolApprovalWidget`:

```python
        elif etype == TurnEventType.TOOL_APPROVAL_REQUEST:
            self._hide_thinking()
            self._reset_tool_run()
            existing = self._tool_widgets.get(event.tool_call_id)
            if isinstance(existing, ExecutePythonWidget):
                existing.show_approval_buttons()
                existing.approved.connect(self._on_tool_approval)
            else:
                widget = ToolApprovalWidget(
                    event.tool_call_id,
                    event.tool_name,
                    event.tool_args,
                    event.text,
                )
                widget.approved.connect(self._on_tool_approval)
                self._insert_widget(widget)
            self._scroll_to_bottom()
```

- [ ] **Step 8: Add `DOCS_GATE_STATUS` to the dispatch**

In `handle_event` (around line 751-790), find where event types are dispatched to handlers. Add `DOCS_GATE_STATUS` routing. Read the dispatch to find the right spot — it likely looks like:

```python
        if etype in (TurnEventType.TEXT_DELTA, TurnEventType.TEXT_DONE):
            self._handle_text_event(event)
            return
        if etype in (TOOL_CALL_START, ..., TOOL_APPROVAL_REQUEST):
            self._handle_tool_event(event)
            return
```

Add before or after the tool-event dispatch:

```python
        if etype == TurnEventType.DOCS_GATE_STATUS:
            self._handle_docs_gate_status(event)
            return
```

And add the handler method to the `ChatView` class (near `_handle_tool_event`):

```python
    def _handle_docs_gate_status(self, event: TurnEvent) -> None:
        """Route a docs-review gate status update to the matching widget."""
        md = event.metadata or {}
        tw = self._tool_widgets.get(event.tool_call_id)
        if isinstance(tw, ExecutePythonWidget):
            tw.set_docs_gate_status(
                md.get("docs_gate_state", ""),
                reasons=tuple(md.get("docs_gate_reasons", [])),
                summary=md.get("docs_gate_summary", ""),
            )
            self._scroll_to_bottom()
```

- [ ] **Step 9: Run tests to verify they pass**

Run: `python3 -m pytest tests/tools/test_chat_view.py::TestExecutePythonRouting -v`
Expected: PASS (5 tests). If `_make_view` stubbing is incomplete, read the method body and add the missing no-op stubs.

- [ ] **Step 10: Run the full UI test suite for regressions**

Run: `python3 -m pytest tests/tools/test_chat_view.py tests/ui/ tests/tools/test_tool_widget_logic.py -v`
Expected: PASS — no regressions. If `test_chat_view_restore.py` breaks, fix in Task 6.

- [ ] **Step 11: Commit**

```bash
git add lucnhan/ui/chat_view.py tests/tools/test_chat_view.py
git commit -m "feat(ui): route execute_python to ExecutePythonWidget in ChatView

- TOOL_CALL_START creates ExecutePythonWidget for execute_python
- DOCS_GATE_STATUS routes into the widget's status line
- TOOL_APPROVAL_REQUEST reuses the existing widget instead of
  creating a separate ToolApprovalWidget
- Widen _tool_widgets type hint to union"
```

---

## Task 6: Route `execute_python` in history restore

**Files:**
- Modify: `lucnhan/ui/chat_view.py:2088-2096` (`_build_restored_tool_widgets`)
- Test: `tests/ui/test_chat_view_restore.py`

**Interfaces:**
- Consumes: `ExecutePythonWidget` from Task 4.
- Produces: restored `execute_python` calls render as `ExecutePythonWidget` (state DONE, code shown, result applied), not `ToolCallWidget`.

- [ ] **Step 1: Read the current restore method**

Read `lucnhan/ui/chat_view.py` lines 2075-2110 to confirm the loop structure and the `ToolSpec` fields available (`ts.name`, `ts.id`, `ts.arguments_json`, `ts.result_content`, `ts.result_is_error`).

- [ ] **Step 2: Write the failing test**

Append to `tests/ui/test_chat_view_restore.py` (read its header to match setup). Add a test that builds a `ToolSpec` for `execute_python` and asserts the restored widget is `ExecutePythonWidget`:

```python
class TestRestoreExecutePython(unittest.TestCase):
    def test_execute_python_restores_as_execute_python_widget(self):
        from lucnhan.ui.chat_view import ChatView
        from lucnhan.ui.tool_widgets import ExecutePythonWidget
        from lucnhan import constants
        # Find the ToolSpec class used by the restore path.
        import lucnhan.ui.chat_view as cv_mod
        ToolSpec = cv_mod.ToolSpec

        ts = ToolSpec(
            id="rc1",
            name=constants.EXECUTE_PYTHON_TOOL_NAME,
            arguments_json='{"code": "print(1)"}',
            result_content="42",
            result_is_error=False,
        )
        view = ChatView.__new__(ChatView)
        view._tool_widgets = {}
        view._group_map = {}
        view._container = None
        widgets = view._build_restored_tool_widgets((ts,))
        # Single tool — returned directly (not in a group).
        self.assertEqual(len(widgets), 1)
        self.assertIsInstance(widgets[0], ExecutePythonWidget)
        self.assertEqual(widgets[0]._code, "print(1)")
        self.assertTrue(widgets[0]._result_block_visible)
        self.assertFalse(widgets[0]._is_error)
```

Note: confirm the `ToolSpec` field names by reading its definition (`grep -n "class ToolSpec" lucnhan/ui/chat_view.py` then read that class — around line 86). Adjust field names in the test if they differ (`arguments_json` vs `arguments`, `result_content` vs `result`).

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/ui/test_chat_view_restore.py::TestRestoreExecutePython -v`
Expected: FAIL — restore creates `ToolCallWidget`.

- [ ] **Step 4: Add the branch to `_build_restored_tool_widgets`**

In `lucnhan/ui/chat_view.py` lines 2088-2096, change the loop body:

```python
        tool_widgets = []
        for ts in tool_specs:
            if ts.name == constants.EXECUTE_PYTHON_TOOL_NAME:
                tw = ExecutePythonWidget(ts.id, parent=self._container)
            else:
                tw = ToolCallWidget(ts.name, ts.id, parent=self._container)
            tw.set_arguments(ts.arguments_json)
            tw.mark_done()
            if ts.result_content or ts.result_is_error:
                tw.set_result(ts.result_content, ts.result_is_error)
            self._tool_widgets[ts.id] = tw
            tool_widgets.append(tw)
```

Ensure `constants` and `ExecutePythonWidget` are imported (done in Task 5).

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/ui/test_chat_view_restore.py -v`
Expected: PASS — new test + all existing restore tests green.

- [ ] **Step 6: Commit**

```bash
git add lucnhan/ui/chat_view.py tests/ui/test_chat_view_restore.py
git commit -m "feat(ui): restore execute_python as ExecutePythonWidget from history

Restored execute_python calls now render with the unified widget (code
editor + result block) instead of the ToolCallWidget QLabel-based layout,
fixing the large-gap layout issue on session reload."
```

---

## Task 7: Local CI verification and final integration check

**Files:**
- No code changes (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `python3 -m pytest tests/ -v -x`
Expected: PASS — all tests green. If `-x` stops on first failure, read the failure and fix it (most likely a test asserting old `TEXT_DELTA` behavior or an import ordering issue).

- [ ] **Step 2: Run format + lint**

Run: `python3 -m ruff format lucnhan/ tests/ && python3 -m ruff check lucnhan/ tests/ --fix`
Expected: clean. If ruff reports issues, re-run `--fix` then verify.

- [ ] **Step 3: Run type check**

Run: `python3 -m mypy lucnhan/core lucnhan/providers lucnhan/agent lucnhan/ui`
Expected: no new errors compared to baseline. If `_tool_widgets: dict[str, ToolCallWidget | ExecutePythonWidget]` introduces a mypy error in a call site that expects `ToolCallWidget`, narrow with `isinstance` or adjust the call site.

- [ ] **Step 4: Run local CI mirror**

Run: `./ci-local.sh --fix`
Expected: all checks pass (format + lint + mypy + pytest + desloppify score ≥ baseline − 0.5).

- [ ] **Step 5: Manual smoke test checklist (in IDA, if available)**

If IDA Pro is available, load the plugin and verify:
- [ ] Simple `execute_python` (1 line): no docs-review status line, code shown once, approval buttons appear.
- [ ] Complex `execute_python` (2+ IDA modules): docs-review status shows "🔍 Reviewing...", then "✓ Docs review passed", then approval buttons.
- [ ] Docs-gate BLOCKED: buttons hidden, status shows reason.
- [ ] Always-allow (click "Always Allow" once, then run another script): no approval buttons, code collapsed, result shown directly.
- [ ] User Deny: result shows "denied" error.
- [ ] Reload session: `execute_python` calls render as unified widget (not QLabel layout).

If IDA is not available, mark this step skipped and note it in the PR description.

- [ ] **Step 6: Commit any CI fixes**

```bash
git add -A
git commit -m "test: fix CI lint/format/type issues from unified widget"
```

(Only if Step 2-4 produced changes. Otherwise skip this commit.)

---

## Self-Review

After the plan was written, I checked it against the spec (`docs/superpowers/specs/2026-07-09-exec-python-unified-widget-design.md`):

**1. Spec coverage:**
- ✓ `DOCS_GATE_STATUS` event — Task 1
- ✓ Loop emits `DOCS_GATE_STATUS` not `TEXT_DELTA` — Task 2
- ✓ FAILED fall-through behavior change — Task 2 Step 5
- ✓ `_describe_tool_call` blank for execute_python — Task 3
- ✓ `ExecutePythonWidget` with all methods — Task 4
- ✓ ChatView live routing (TOOL_CALL_START, DONE, APPROVAL_REQUEST, DOCS_GATE_STATUS) — Task 5
- ✓ ChatView restore routing — Task 6
- ✓ Type hint union — Task 5 Step 4
- ✓ Interface contract (set_arguments, mark_done, hide_preview) — Task 4
- ✓ Blocked hides buttons, FAILED keeps buttons — Task 4 tests
- ✓ Fix Image #2 (code editor with max height, not QLabel) — Task 4 `_build_code_section`
- ✓ Fix Image #3 (no redundant description) — Task 3 + Task 4 test `test_no_redundant_description_label`

**2. Placeholder scan:** No TBD/TODO. Every code step contains full code. Test steps contain full test code. The `_make_view` stubbing note (Task 5 Step 2) instructs the implementer to read the method body — this is explicit guidance, not a placeholder.

**3. Type consistency:** `docs_gate_status(tool_call_id, state, reasons=(), summary="")` is consistent across Task 1 (factory), Task 2 (loop calls), Task 4 (widget method `set_docs_gate_status`), Task 5 (handler unwraps metadata). `approved` signal `(str, str)` consistent across Task 4 and Task 5. `set_arguments(args_text: str)` consistent across Task 4, 5, 6.

One ambiguity resolved: the widget stores `_status_text` and `_buttons_visible` as testable attributes (used by Task 5 tests) — these are documented in the implementation in Task 4 Step 3.
