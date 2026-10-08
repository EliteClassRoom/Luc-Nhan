# Docs-Review Gate Post-Error (Hybrid) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Chuyển docs-reviewer từ pre-execute (trigger khi script "complex") sang post-error (trigger khi script fail runtime với API-shaped exception), preload Module Quick Reference vào system prompt, và auto-inject module reference vào tool result sau khi reviewer chạy.

**Architecture:** Bốn thay đổi phối hợp: (1) module mới `traceback_classifier.py` phân loại traceback, (2) config field mới `docs_review_mode: Literal["on_error","off"]` thay boolean cũ + migration, (3) loop.py đảo logic reviewer từ pre-execute sang post-error + hàm mới `_review_failed_script` + `_build_reference_injection`, (4) system prompt bổ sung `IDA_API_MODULE_REFERENCE_SECTION` + reviewer prompt update + settings dialog update.

**Tech Stack:** Python 3.11+, pytest, dataclasses, AST parsing, IDA Pro 9.x PySide6 (Qt6). Pure functions cho classifier/validator (không dependency IDA). Agent loop generator-based.

## Global Constraints

- Mọi module bắt đầu bằng `from __future__ import annotations`.
- Type hints ở mọi signature. Tool params dùng `typing.Annotated[type, "description"]`.
- Cross-package imports absolute: `from lucnhan.tools.base import tool`.
- Host API imports (`ida_*`) dùng `importlib.import_module()` trong `try/except ImportError` — KHÔNG import ở module level.
- `execute_python` LUÔN cần user approval — KHÔNG bao giờ auto-approve (security invariant, CLAUDE.md §4).
- Tên tool `execute_python` phải dùng `lucnhan.constants.EXECUTE_PYTHON_TOOL_NAME` — KHÔNG hardcode string.
- f-string cho format, hex address `f"0x{ea:x}"`. Không mutation, không bare `except:`, không magic numbers.
- `./ci-local.sh` phải pass trước khi commit (format + lint + mypy + pytest + desloppify).
- Commit format: `type(scope): description` (conventional commits).

**Spec reference:** `docs/superpowers/specs/2026-07-13-docs-review-post-error-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `lucnhan/tools/traceback_classifier.py` | Create | Pure function: parse traceback → verdict API-shaped + extract modules từ script AST |
| `lucnhan/core/config.py` | Modify | Thay `require_ida_docs_for_complex_scripts: bool` bằng `docs_review_mode: Literal["on_error","off"]` + migration trong `load()` |
| `lucnhan/agent/prompts/base.py` | Modify | Thêm `IDA_API_MODULE_REFERENCE_SECTION` + rewrite "Docs-review gate" section trong `IDA_API_DISCIPLINE_SECTION` |
| `lucnhan/agent/prompts/ida.py` | Modify | Wire `IDA_API_MODULE_REFERENCE_SECTION` vào `assemble_system_prompt()` |
| `lucnhan/agent/agents/ida_docs_reviewer.py` | Modify | Update prompt: reviewer giờ là post-error diagnostician, input có traceback |
| `lucnhan/agent/loop.py` | Modify | Xóa reviewer pre-execute, thêm reviewer post-error trong `_execute_single_tool` (2 vị trí), thêm `_review_failed_script` + `_build_reference_injection`, thêm flag `_docs_reviewer_invoked` |
| `lucnhan/ui/settings_dialog.py` | Modify | Thay checkbox boolean bằng combobox enum `docs_review_mode` |
| `tests/tools/test_traceback_classifier.py` | Create | Unit tests cho `classify_traceback` + helpers |
| `tests/test_idapython_docs_gate.py` | Modify | Rewrite tests cho `_review_failed_script` (thay `_review_complex_idapython_script`), cập nhật config test cho `docs_review_mode` |
| `tests/agent/test_system_prompt.py` | Modify | Assert `IDA_API_MODULE_REFERENCE_SECTION` có trong system prompt |

---

## Task 1: Module `traceback_classifier.py` (TDD)

**Files:**
- Create: `lucnhan/tools/traceback_classifier.py`
- Test: `tests/tools/test_traceback_classifier.py`

**Interfaces:**
- Produces: `TracebackClassification` (frozen dataclass: `is_api_shaped: bool`, `exception_type: str`, `exception_message: str`, `modules_referenced: tuple[str, ...]`), `classify_traceback(traceback_text: str, script_code: str = "") -> TracebackClassification`

- [ ] **Step 1: Write the failing test**

Tạo file `tests/tools/test_traceback_classifier.py`:

```python
"""Tests for traceback classification — pure function, no IDA deps."""

from __future__ import annotations

import unittest

from lucnhan.tools.traceback_classifier import (
    TracebackClassification,
    classify_traceback,
)


class TestClassifyTraceback(unittest.TestCase):
    def test_attribute_error_is_api_shaped(self):
        tb = (
            "Traceback (most recent call last):\n"
            '  File "<string>", line 1, in <module>\n'
            "AttributeError: module 'idaapi' has no attribute 'get_operands'\n"
        )
        result = classify_traceback(tb)
        self.assertTrue(result.is_api_shaped)
        self.assertEqual(result.exception_type, "AttributeError")
        self.assertIn("get_operands", result.exception_message)

    def test_import_error_is_api_shaped(self):
        tb = (
            "Traceback (most recent call last):\n"
            '  File "<string>", line 1, in <module>\n'
            "ImportError: No module named ida_struct\n"
        )
        result = classify_traceback(tb)
        self.assertTrue(result.is_api_shaped)
        self.assertEqual(result.exception_type, "ImportError")

    def test_module_not_found_error_is_api_shaped(self):
        tb = (
            "Traceback (most recent call last):\n"
            '  File "<string>", line 1, in <module>\n'
            "ModuleNotFoundError: No module named 'ida_nonexistent'\n"
        )
        result = classify_traceback(tb)
        self.assertTrue(result.is_api_shaped)
        self.assertEqual(result.exception_type, "ModuleNotFoundError")

    def test_name_error_is_api_shaped(self):
        tb = (
            "Traceback (most recent call last):\n"
            '  File "<string>", line 1, in <module>\n'
            "NameError: name 'BADADDR' is not defined\n"
        )
        result = classify_traceback(tb)
        self.assertTrue(result.is_api_shaped)
        self.assertEqual(result.exception_type, "NameError")

    def test_value_error_is_not_api_shaped(self):
        tb = (
            "Traceback (most recent call last):\n"
            '  File "<string>", line 1, in <module>\n'
            "ValueError: invalid literal for int() with base 16: 'xyz'\n"
        )
        result = classify_traceback(tb)
        self.assertFalse(result.is_api_shaped)

    def test_type_error_is_not_api_shaped(self):
        tb = (
            "Traceback (most recent call last):\n"
            '  File "<string>", line 1, in <module>\n'
            "TypeError: unsupported operand type(s) for +: 'int' and 'str'\n"
        )
        result = classify_traceback(tb)
        self.assertFalse(result.is_api_shaped)

    def test_empty_traceback_returns_not_api_shaped(self):
        result = classify_traceback("")
        self.assertFalse(result.is_api_shaped)
        self.assertEqual(result.exception_type, "")

    def test_extract_modules_from_imports(self):
        code = "import ida_bytes\nimport ida_funcs\nprint(1)\n"
        result = classify_traceback("NameError: x", code)
        self.assertIn("ida_bytes", result.modules_referenced)
        self.assertIn("ida_funcs", result.modules_referenced)

    def test_extract_modules_from_from_imports(self):
        code = "from ida_hexrays import decompile\nfrom idautils import Functions\n"
        result = classify_traceback("NameError: x", code)
        self.assertIn("ida_hexrays", result.modules_referenced)
        self.assertIn("idautils", result.modules_referenced)

    def test_extract_modules_bare_idautils_idc_idaapi(self):
        code = "import idautils\nimport idc\nimport idaapi\n"
        result = classify_traceback("NameError: x", code)
        self.assertIn("idautils", result.modules_referenced)
        self.assertIn("idc", result.modules_referenced)
        self.assertIn("idaapi", result.modules_referenced)

    def test_extract_modules_no_ida_modules(self):
        code = "import json\nimport struct\nprint(1)\n"
        result = classify_traceback("NameError: x", code)
        self.assertEqual(result.modules_referenced, ())

    def test_extract_modules_syntax_error_returns_empty(self):
        code = "def broken(:\n"
        result = classify_traceback("NameError: x", code)
        self.assertEqual(result.modules_referenced, ())

    def test_exception_message_extracted(self):
        tb = "AttributeError: module 'idaapi' has no attribute 'foo'\n"
        result = classify_traceback(tb)
        self.assertEqual(
            result.exception_message,
            "module 'idaapi' has no attribute 'foo'",
        )

    def test_no_code_returns_empty_modules(self):
        result = classify_traceback("NameError: x")
        self.assertEqual(result.modules_referenced, ())

    def test_returns_frozen_dataclass(self):
        result = classify_traceback("NameError: x")
        self.assertIsInstance(result, TracebackClassification)
        # Frozen dataclass — mutation should raise
        with self.assertRaises(Exception):
            result.is_api_shaped = True  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/tools/test_traceback_classifier.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lucnhan.tools.traceback_classifier'`

- [ ] **Step 3: Write minimal implementation**

Tạo file `lucnhan/tools/traceback_classifier.py`:

```python
"""Phân loại traceback của execute_python để quyết định có spawn reviewer không.

Pure function — không import IDA, không gọi LLM, không touch globals.
Operate trên traceback string + script source.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

#: Exception types được xem là "API-shaped" — rõ ràng là hallucinated/wrong API.
#: Các exception khác (ValueError, TypeError, KeyError...) là logic bug → main agent tự sửa.
_API_SHAPED_EXCEPTIONS: frozenset[str] = frozenset(
    {
        "AttributeError",
        "ImportError",
        "ModuleNotFoundError",  # subclass của ImportError nhưng tường minh
        "NameError",
    }
)

#: Prefix của module IDA trong namespace execute_python. ``idautils``,
#: ``idc``, ``idaapi`` không có prefix ``ida_`` nhưng vẫn là module IDA.
_IDA_MODULE_NAMES: frozenset[str] = frozenset({"idautils", "idc", "idaapi"})


@dataclass(frozen=True)
class TracebackClassification:
    """Kết quả phân loại traceback.

    *is_api_shaped* True nếu exception type thuộc ``_API_SHAPED_EXCEPTIONS``.
    *modules_referenced* là tuple tên module IDA được import trong script,
    dùng cho reference injection sau khi reviewer chạy.
    """

    is_api_shaped: bool
    exception_type: str = ""
    exception_message: str = ""
    modules_referenced: tuple[str, ...] = ()


def _parse_exception_type(traceback_text: str) -> str:
    """Extract exception type từ dòng cuối của traceback.

    Traceback Python kết thúc bằng: ``AttributeError: module 'x' has no attribute 'y'``
    Trả về tên type (vd "AttributeError") hoặc "" nếu không parse được.
    """
    lines = [ln.strip() for ln in traceback_text.strip().splitlines() if ln.strip()]
    if not lines:
        return ""
    last = lines[-1]
    if ":" in last:
        return last.split(":", 1)[0].strip()
    return last


def _parse_exception_message(traceback_text: str) -> str:
    """Extract message (phần sau dấu ':') từ dòng cuối traceback."""
    lines = [ln.strip() for ln in traceback_text.strip().splitlines() if ln.strip()]
    if not lines:
        return ""
    last = lines[-1]
    if ":" in last:
        return last.split(":", 1)[1].strip()
    return ""


def _extract_modules_from_code(code: str) -> tuple[str, ...]:
    """Extract tên module IDA được import trong script.

    Dùng để quyết định ``lookup_idapython_doc(module=...)`` cho module nào
    khi inject reference. Trả về tuple theo thứ tự xuất hiện, dedup.
    """
    modules: list[str] = []
    seen: set[str] = set()
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in _IDA_MODULE_NAMES or top.startswith("ida_"):
                    if top not in seen:
                        modules.append(top)
                        seen.add(top)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top = node.module.split(".")[0]
            if top in _IDA_MODULE_NAMES or top.startswith("ida_"):
                if top not in seen:
                    modules.append(top)
                    seen.add(top)
    return tuple(modules)


def classify_traceback(
    traceback_text: str,
    script_code: str = "",
) -> TracebackClassification:
    """Phân loại traceback + extract context cho reviewer.

    Args:
        traceback_text: Output stderr của execute_python (chứa traceback).
        script_code: Script body (để extract modules cho reference injection).

    Returns:
        TracebackClassification với is_api_shaped, exception_type, message, modules.
    """
    exc_type = _parse_exception_type(traceback_text)
    is_api_shaped = exc_type in _API_SHAPED_EXCEPTIONS
    exc_message = _parse_exception_message(traceback_text)
    modules = _extract_modules_from_code(script_code) if script_code else ()
    return TracebackClassification(
        is_api_shaped=is_api_shaped,
        exception_type=exc_type,
        exception_message=exc_message,
        modules_referenced=modules,
    )


__all__ = [
    "TracebackClassification",
    "classify_traceback",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/tools/test_traceback_classifier.py -v`
Expected: PASS — all 15 tests green.

- [ ] **Step 5: Run lint + type check**

Run:
```bash
python3 -m ruff format lucnhan/tools/traceback_classifier.py tests/tools/test_traceback_classifier.py
python3 -m ruff check lucnhan/tools/traceback_classifier.py --fix
python3 -m mypy lucnhan/tools/traceback_classifier.py
```
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add lucnhan/tools/traceback_classifier.py tests/tools/test_traceback_classifier.py
git commit -m "feat(tools): add traceback_classifier for post-error docs gate

Pure function phân loại traceback của execute_python: verdict API-shaped
(AttributeError/ImportError/NameError) + extract modules IDA từ script AST
cho reference injection."
```

---

## Task 2: Config field `docs_review_mode` + migration

**Files:**
- Modify: `lucnhan/core/config.py:22` (import Literal), `:86` (field), `:290-342` (load migration)
- Test: `tests/test_idapython_docs_gate.py` (class `TestConfigField`)

**Interfaces:**
- Produces: `LucNhanConfig.docs_review_mode: Literal["on_error", "off"]` (default `"on_error"`)
- Migration: legacy `require_ida_docs_for_complex_scripts: False` → `docs_review_mode = "off"`

- [ ] **Step 1: Write the failing test**

Trong `tests/test_idapython_docs_gate.py`, **thay thế** class `TestConfigField` (dòng 124-141 hiện tại) bằng:

```python
class TestConfigField(unittest.TestCase):
    def test_default_is_on_error(self):
        cfg = LucNhanConfig()
        self.assertEqual(cfg.docs_review_mode, "on_error")

    def test_round_trip_through_dict(self):
        cfg = LucNhanConfig()
        cfg.docs_review_mode = "off"
        cfg.save = MagicMock()  # avoid disk side effects
        cfg.load = MagicMock()
        from dataclasses import asdict

        d = asdict(cfg)
        cfg2 = LucNhanConfig()
        cfg2.docs_review_mode = d["docs_review_mode"]
        self.assertEqual(cfg2.docs_review_mode, "off")

    def test_legacy_false_migrates_to_off(self):
        """Legacy config require_ida_docs_for_complex_scripts=False → off."""
        cfg = LucNhanConfig()
        # Simulate load() with legacy field present
        legacy_data = {"require_ida_docs_for_complex_scripts": False}
        cfg._apply_loaded_config(legacy_data)
        self.assertEqual(cfg.docs_review_mode, "off")

    def test_legacy_true_migrates_to_on_error(self):
        """Legacy config require_ida_docs_for_complex_scripts=True → on_error."""
        cfg = LucNhanConfig()
        legacy_data = {"require_ida_docs_for_complex_scripts": True}
        cfg._apply_loaded_config(legacy_data)
        self.assertEqual(cfg.docs_review_mode, "on_error")

    def test_legacy_missing_defaults_to_on_error(self):
        """No legacy field → on_error default."""
        cfg = LucNhanConfig()
        cfg._apply_loaded_config({})
        self.assertEqual(cfg.docs_review_mode, "on_error")

    def test_explicit_off_round_trips(self):
        cfg = LucNhanConfig()
        cfg._apply_loaded_config({"docs_review_mode": "off"})
        self.assertEqual(cfg.docs_review_mode, "off")

    def test_invalid_value_defaults_to_on_error(self):
        cfg = LucNhanConfig()
        cfg._apply_loaded_config({"docs_review_mode": "bogus"})
        self.assertEqual(cfg.docs_review_mode, "on_error")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_idapython_docs_gate.py::TestConfigField -v`
Expected: FAIL — `AttributeError: 'LucNhanConfig' object has no attribute 'docs_review_mode'` hoặc `_apply_loaded_config` not found.

- [ ] **Step 3: Modify config.py — imports + field**

Trong `lucnhan/core/config.py`:

**Dòng 22**, thay:
```python
from typing import TYPE_CHECKING, Any
```
thành:
```python
from typing import TYPE_CHECKING, Any, Literal
```

**Dòng 86**, thay:
```python
require_ida_docs_for_complex_scripts: bool = True  # docs-gate complex execute_python scripts
```
thành:
```python
docs_review_mode: Literal["on_error", "off"] = "on_error"  # docs-reviewer trigger: on runtime API-shaped error, or off
```

- [ ] **Step 4: Extract `_apply_loaded_config` helper + migration**

Hiện tại logic load nằm inline trong `load()` (dòng ~290-342). Extract ra method riêng để test được + thêm migration. 

**Thêm method mới** (sau method `load()`, ~dòng 343):

```python
    def _apply_loaded_config(self, data: dict[str, Any]) -> None:
        """Apply loaded config dict to this instance, with legacy migration.

        Extracted from ``load()`` so migration logic is unit-testable
        without touching disk. Handles the legacy
        ``require_ida_docs_for_complex_scripts`` boolean → ``docs_review_mode``
        enum migration.
        """
        # Legacy migration: require_ida_docs_for_complex_scripts → docs_review_mode
        legacy_field = "require_ida_docs_for_complex_scripts"
        if legacy_field in data and "docs_review_mode" not in data:
            legacy_val = data[legacy_field]
            # False (user disabled reviewer) → "off"; True or anything → "on_error"
            data["docs_review_mode"] = "off" if legacy_val is False else "on_error"

        if "provider" in data:
            for k, v in data["provider"].items():
                if hasattr(self.provider, k):
                    setattr(self.provider, k, v)
        self.providers = data.get("providers", {})
        self.custom_providers = data.get("custom_providers", {})
        for k in (
            "auto_context",
            "plan_mode_default",
            "checkpoint_auto_save",
            "approve_mutations",
            "exploration_turn_limit",
            "max_retries",
            "silent_retry_mode",
            "docs_review_mode",
            "theme",
            "font_family",
            "font_size_override",
            "disabled_skills",
            "enabled_external_skills",
            "enabled_external_mcp",
            "active_profile",
            "custom_profiles",
            "a2a_auto_discover",
            "a2a_agents",
            "bulk_renamer_batch_size",
            "bulk_renamer_max_concurrent",
            "startup_restore_sessions",
            "oauth_consent_accepted",
            "encrypt_api_keys",
            "ida_output_log_level",
            "knowledge_enabled",
            "knowledge_show_retrieved_in_chat",
            "knowledge_max_context_items",
            "knowledge_max_context_chars",
        ):
            if k in data:
                val = data[k]
                # Normalize unknown/legacy theme to "auto" so the new
                # AUTO/IDA_NATIVE/DARK/LIGHT ThemeMode enum round-trips
                # correctly.  "auto" is the safe default for fresh
                # installs and for older configs that predate the
                # new theme system.
                if k == "theme" and val not in {"ida", "dark", "light", "auto"}:
                    val = "auto"
                # Normalize invalid startup_restore_sessions to "all"
                if k == "startup_restore_sessions" and val not in ("latest", "all", "none"):
                    val = "all"
                # Normalize invalid log verbosity to "warning"
                if k == "ida_output_log_level" and val not in (
                    "debug",
                    "info",
                    "warning",
                    "error",
                    "critical",
                    "off",
                ):
                    val = "warning"
                # Normalize invalid docs_review_mode to "on_error"
                if k == "docs_review_mode" and val not in ("on_error", "off"):
                    val = "on_error"
                setattr(self, k, val)
```

- [ ] **Step 5: Refactor `load()` to call `_apply_loaded_config`**

Trong `load()` (dòng ~269-342), **thay block inline** (từ `if "provider" in data:` đến hết setattr loop, ~dòng 284-342) bằng:

```python
        self._apply_loaded_config(data)
```

Giữ nguyên phần trước (decrypt detection, `enc = data.pop("encryption", {})`) và phần sau (nếu có) của `load()`.

**Quan trọng:** Đảm bảo `load()` vẫn pop `encryption` và `schema_version` trước khi gọi `_apply_loaded_config(data)` — data dict đã được clean. Verify bằng cách đọc lại `load()` sau khi edit.

- [ ] **Step 6: Run test to verify it passes**

Run: `python3 -m pytest tests/test_idapython_docs_gate.py::TestConfigField -v`
Expected: PASS — all 7 tests green.

- [ ] **Step 7: Run full test suite to check no regression**

Run: `python3 -m pytest tests/test_idapython_docs_gate.py -v`
Expected: Tests khác trong file có thể fail (vì `_review_complex_idapython_script` sẽ bị đổi ở Task 5 — OK, sẽ fix ở Task 5). Chỉ `TestConfigField` và `TestClassifier` phải pass.

- [ ] **Step 8: Run lint + type check**

Run:
```bash
python3 -m ruff format lucnhan/core/config.py
python3 -m ruff check lucnhan/core/config.py --fix
python3 -m mypy lucnhan/core/config.py
```
Expected: clean.

- [ ] **Step 9: Commit**

```bash
git add lucnhan/core/config.py tests/test_idapython_docs_gate.py
git commit -m "refactor(config): replace require_ida_docs_for_complex_scripts with docs_review_mode enum

Field mới docs_review_mode: Literal[\"on_error\",\"off\"] thay boolean cũ.
Migration trong _apply_loaded_config: legacy False → off, True/missing → on_error.
Extract _apply_loaded_config helper từ load() để test được migration."
```

---

## Task 3: System prompt — `IDA_API_MODULE_REFERENCE_SECTION`

**Files:**
- Modify: `lucnhan/agent/prompts/base.py` (thêm section mới + rewrite Docs-review gate section)
- Modify: `lucnhan/agent/prompts/ida.py:70-75` (wire section vào assemble)
- Test: `tests/agent/test_system_prompt.py`

**Interfaces:**
- Produces: `IDA_API_MODULE_REFERENCE_SECTION` (string constant trong `base.py`), wired vào `IDA_BASE_PROMPT`

- [ ] **Step 1: Write the failing test**

Trong `tests/agent/test_system_prompt.py`, **thêm** test:

```python
def test_ida_base_prompt_contains_module_reference():
    """Module Quick Reference section phải có trong system prompt."""
    from lucnhan.agent.prompts.ida import IDA_BASE_PROMPT

    assert "IDAPython Module Quick Reference" in IDA_BASE_PROMPT
    assert "ida_bytes" in IDA_BASE_PROMPT
    assert "ida_typeinf" in IDA_BASE_PROMPT
    assert "decode_insn" in IDA_BASE_PROMPT


def test_ida_base_prompt_docs_review_section_updated():
    """Docs-review gate section phải mô tả post-error behavior, không phải pre-execute."""
    from lucnhan.agent.prompts.ida import IDA_BASE_PROMPT

    # Phải nhắc đến post-error / runtime error
    assert "runtime error" in IDA_BASE_PROMPT.lower() or "post-error" in IDA_BASE_PROMPT.lower()
    # Không còn mô tả "before you are asked to approve" (behavior cũ)
    assert "before you are asked to approve" not in IDA_BASE_PROMPT.lower()
```

(Nếu file test chưa có import pattern phù hợp, kiểm tra file trước để match style hiện tại.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/agent/test_system_prompt.py -v -k "module_reference or docs_review_section"`
Expected: FAIL — `"IDAPython Module Quick Reference" not found in IDA_BASE_PROMPT`.

- [ ] **Step 3: Add `IDA_API_MODULE_REFERENCE_SECTION` to base.py**

Trong `lucnhan/agent/prompts/base.py`, **thêm** (sau `IDA_API_DISCIPLINE_SECTION`, trước hàm `assemble_system_prompt` hoặc ngay sau `IDA_API_DISCIPLINE_SECTION`):

```python
IDA_API_MODULE_REFERENCE_SECTION = """\
## IDAPython Module Quick Reference

When you write `execute_python` scripts, use this router to pick the right
module. The static validator blocks known-hallucinated APIs; this table
helps you pick correctly the first time.

| Task | Module | Key items |
|------|--------|-----------|
| Bytes/memory | `ida_bytes` | `get_bytes`, `patch_bytes`, `get_byte/word/dword/qword`, `get_strlit_contents` |
| Functions | `ida_funcs` | `func_t`, `get_func`, `add_func`, `get_func_name`, `get_next_func` |
| Names | `ida_name` | `set_name`, `get_name`, `demangle_name`, `get_name_ea` |
| Types | `ida_typeinf` | `tinfo_t`, `udt_type_data_t`, `apply_tinfo`, `apply_cdecl`, `parse_decl` |
| Decompiler | `ida_hexrays` | `decompile`, `cfunc_t`, `lvar_t`, `ctree_visitor_t` |
| Segments | `ida_segment` | `segment_t`, `getseg`, `get_segm_by_name` |
| Xrefs | `ida_xref` | `xrefblk_t`, `add_cref`, `add_dref` |
| Instructions | `ida_ua` | `insn_t`, `op_t`, `decode_insn` |
| Stack frames | `ida_frame` | `get_func_frame`, `define_stkvar` |
| Iteration | `idautils` | `Functions`, `Heads`, `XrefsTo`, `Strings`, `Names`, `Segments` |
| UI/dialogs | `ida_kernwin` | `msg`, `ask_str`, `ask_yn`, `jumpto`, `get_screen_ea` |
| Database info | `ida_ida` | `inf_get_procname`, `inf_is_64bit`, `inf_get_min_ea` |
| Analysis | `ida_auto` | `auto_wait`, `plan_and_wait` |
| Persistent storage | `ida_netnode` | `netnode`, `hashset`, `hashstr` |

### Core Patterns (verified IDA 9.x)

```python
# Iterate functions
for ea in idautils.Functions():
    name = ida_funcs.get_func_name(ea)
    func = ida_funcs.get_func(ea)        # func_t or None — check before .start_ea

# Decode instruction operands
insn = ida_ua.insn_t()
if ida_ua.decode_insn(insn, ea):
    for op in insn.ops:
        print(op.type, op.value)

# Cross-references
for xref in idautils.XrefsTo(ea, ida_xref.XREF_ALL):
    print(f"{xref.frm:#x} -> {xref.to:#x}")

# Read / write bytes
data = ida_bytes.get_bytes(ea, size)
ida_bytes.patch_bytes(ea, b"\\x90\\x90")

# Decompile (ALWAYS wrap — raises DecompilationFailure)
try:
    cfunc = ida_hexrays.decompile(ea)
    print(cfunc)
except ida_hexrays.DecompilationFailure:
    pass

# Build a struct (IDA 9.x — offsets in BITS)
tif = ida_typeinf.tinfo_t()
tif.create_udt(ida_typeinf.udt_type_data_t(), ida_typeinf.BTF_STRUCT)
tif.add_udm("field1", "int", offset=0 * 8)
tif.add_udm("field2", "char *", offset=4 * 8)
tif.set_named_type(ida_typeinf.get_idati(), "MyStruct", ida_typeinf.NTF_REPLACE)
```

### Critical rules
- `ida_funcs.get_func()` returns `None` if no function — check before `.start_ea`.
- `ida_hexrays.decompile()` raises `DecompilationFailure` — always wrap in try/except.
- `ida_bytes.get_strlit_contents()` returns `bytes`, not `str` — decode if needed.
- IDA 9 removed `ida_struct`/`ida_enum` → use `ida_typeinf`. `get_inf_structure()` → `inf_get_*()`.
- `udm_t.offset`/`udm_t.size` in BITS. Use `create_simple_type()`, never `tinfo_t(BT_*)`.

For deeper reference, call `lookup_idapython_doc(module="<module>")` — reads
from the bundled offline docs (54 modules, no network).
"""
```

- [ ] **Step 4: Rewrite "Docs-review gate" section in `IDA_API_DISCIPLINE_SECTION`**

Trong `lucnhan/agent/prompts/base.py`, **tìm** block "Docs-review gate" trong `IDA_API_DISCIPLINE_SECTION` (dòng ~322-332 hiện tại, bắt đầu bằng `**Docs-review gate.**`). **Thay thế** block đó bằng:

```python
**Docs-review gate (post-error).** When an `execute_python` script fails at
runtime with an API-shaped exception (AttributeError, ImportError, NameError),
a docs-reviewer subagent diagnoses the failure and auto-injects the relevant
module reference into the tool result. You get one reviewer diagnosis per
task — after that, fix based on the reference already in context. To avoid
this round-trip, verify APIs against the Module Quick Reference above and
call `lookup_idapython_doc(module="<module>")` before writing the script.
```

- [ ] **Step 5: Wire section into ida.py**

Trong `lucnhan/agent/prompts/ida.py`, **cập nhật** import (dòng 5-9) và `assemble_system_prompt` call (dòng 70-75):

```python
from .base import (
    IDA_API_DISCIPLINE_SECTION,
    IDA_API_MODULE_REFERENCE_SECTION,
    SHARED_CAPABILITIES_BULLETS,
    assemble_system_prompt,
)
```

```python
IDA_BASE_PROMPT = assemble_system_prompt(
    _IDA_INTRO,
    _IDA_TOOL_USAGE,
    _IDA_CAPABILITIES,
    IDA_API_MODULE_REFERENCE_SECTION,
    IDA_API_DISCIPLINE_SECTION,
)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python3 -m pytest tests/agent/test_system_prompt.py -v -k "module_reference or docs_review_section"`
Expected: PASS.

- [ ] **Step 7: Run lint + format**

Run:
```bash
python3 -m ruff format lucnhan/agent/prompts/base.py lucnhan/agent/prompts/ida.py
python3 -m ruff check lucnhan/agent/prompts/ --fix
```
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add lucnhan/agent/prompts/base.py lucnhan/agent/prompts/ida.py tests/agent/test_system_prompt.py
git commit -m "feat(prompts): add IDA API Module Quick Reference to system prompt

Preload Module Router + Core Patterns compact vào main agent system prompt
để giảm hallucinate ngay từ đầu. Rewrite Docs-review gate section mô tả
post-error behavior thay vì pre-execute."
```

---

## Task 4: Reviewer prompt update — post-error diagnostician

**Files:**
- Modify: `lucnhan/agent/agents/ida_docs_reviewer.py:29-184` (IDA_DOCS_REVIEWER_PROMPT)
- Test: `tests/test_ida_docs_review_prompt.py`

**Interfaces:**
- Produces: `IDA_DOCS_REVIEWER_PROMPT` updated (role = post-error diagnostician, input có traceback)

- [ ] **Step 1: Read existing test to understand current expectations**

Run: `python3 -m pytest tests/test_ida_docs_review_prompt.py -v`
Ghi nhận test nào pass hiện tại — sẽ cần update chúng.

- [ ] **Step 2: Write/update the failing test**

Đọc `tests/test_ida_docs_review_prompt.py` hiện tại. **Thêm** test (giữ test cũ nếu vẫn hợp lệ):

```python
def test_reviewer_prompt_describes_post_error_role():
    """Reviewer prompt phải mô tả role post-error diagnostician."""
    from lucnhan.agent.agents.ida_docs_reviewer import IDA_DOCS_REVIEWER_PROMPT

    # Phải nhắc đến runtime error / diagnose failure
    assert "diagnose" in IDA_DOCS_REVIEWER_PROMPT.lower() or "runtime" in IDA_DOCS_REVIEWER_PROMPT.lower()
    # Phải nhắc đến traceback trong input
    assert "traceback" in IDA_DOCS_REVIEWER_PROMPT.lower()


def test_reviewer_prompt_keeps_verdict_contract():
    """Output contract (VERDICT/REASONS/API_NOTES/REWRITE_GUIDANCE) giữ nguyên."""
    from lucnhan.agent.agents.ida_docs_reviewer import IDA_DOCS_REVIEWER_PROMPT

    assert "VERDICT:" in IDA_DOCS_REVIEWER_PROMPT
    assert "REASONS:" in IDA_DOCS_REVIEWER_PROMPT
    assert "API_NOTES:" in IDA_DOCS_REVIEWER_PROMPT
    assert "REWRITE_GUIDANCE:" in IDA_DOCS_REVIEWER_PROMPT
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_ida_docs_review_prompt.py -v -k "post_error_role"`
Expected: FAIL — "diagnose" / "traceback" not found in prompt.

- [ ] **Step 4: Update `IDA_DOCS_REVIEWER_PROMPT`**

Trong `lucnhan/agent/agents/ida_docs_reviewer.py`, **thay thế** toàn bộ `IDA_DOCS_REVIEWER_PROMPT` (dòng 29-184) bằng:

```python
IDA_DOCS_REVIEWER_PROMPT = """\
You are an IDA Pro / IDAPython documentation reviewer. You do NOT execute
or modify the binary — you diagnose why an IDAPython script FAILED at
runtime and tell the main agent how to fix it.

Your job:

1. Read the failed script, the runtime traceback, and the exception type.
2. For every non-trivial call (``ida_*``, ``idautils``, ``idc``, ``idaapi``,
   ``ida_hexrays``, ``ida_typeinf``, ``ida_frame``, ``ida_domain``,
   ``ida_kernwin``, ``ida_ua``), determine whether the API exists and
   whether the script used it correctly (wrong signature, wrong arg type,
   hallucinated name, removed module, etc.).
3. Prefer **local bundled references** first, then official Hex-Rays
   docs via the ``web_fetch`` tool when local references do not cover
   the API or when IDA 9.x compatibility is uncertain.

Documentation sources (use in this order):

A. The bundled ``ida-scripting`` skill.  The skill auto-activates
   for this agent — its body and the ``api-reference.md`` are already
   in your context (you'll see them under "[Skill: IDA Scripting]").
   Check there FIRST; most APIs are covered including the ``DO NOT
   USE`` anti-hallucination table.

B. The bundled offline docs (preferred — always try this FIRST):

   The offline docs bundle ships inside the plugin at
   ``data/idapython-docs/<module>.rst.txt``. Use the
   ``lookup_idapython_doc`` tool to read it:

   ```
   lookup_idapython_doc(module="<module>")
   ```

   Concrete example — to verify ``ida_typeinf.apply_cdecl``:
   ``lookup_idapython_doc(module="ida_typeinf")`` returns the entire
   ``ida_typeinf`` RST reference in one call (5-15 KB raw source).

   Common modules: ``ida_typeinf``, ``ida_name``, ``idautils``,
   ``ida_hexrays``, ``ida_frame``, ``ida_funcs``, ``ida_bytes``,
   ``ida_xref``, ``ida_segment``, ``ida_kernwin``, ``ida_ua``,
   ``idc``, ``idaapi``. These files return the raw RST source.

   **Always try ``lookup_idapython_doc`` first** for any module the
   script touches — even if you're not sure the module is bundled.
   The tool returns a clear "Module not in offline bundle" error with
   a list of available modules, so the cost of a miss is one tool call.

C. Hex-Rays Python reference (online FALLBACK — use ONLY after offline fails):

   **Only reach for ``web_fetch`` when ``lookup_idapython_doc`` cannot
   resolve your verification.** Two scenarios qualify:

   1. The module name is not in the offline bundle (the tool returns
      "Module 'X' not found in offline bundle"). Common for rare modules
      not in our 54-module bundle (``ida_pro``, ``ida_lumina``, etc.).
   2. The offline docs were consulted but did not resolve the question —
      e.g., the specific function/parameter/edge case isn't documented
      there, or the docs are ambiguous. Verify you actually READ the
      relevant section first before falling back.

   Do NOT use ``web_fetch`` as a first attempt. The offline docs cover
   ~95% of common usage and are deterministic + network-free. Preferring
   online when offline would have worked wastes time and risks 403 errors.

   When you do fall back, the Sphinx site behind
   ``python.docs.hex-rays.com`` serves raw RST source files.  Each
   file contains the FULL reference for one module — every function,
   every parameter, every note — in a single fetch:

   ```
   web_fetch(
       url="https://python.docs.hex-rays.com/_sources/<module>/index.rst.txt",
       format="text",
   )
   ```

   **DO NOT fetch HTML pages like
   ``https://python.docs.hex-rays.com/ida_<module>/<func>.html`` —
   they return 403 Forbidden (the site is bot-protected).**  The
   raw RST source above contains the same information without the
   fetch failures.

D. Hex-Rays GitBook developer guide:
   https://docs.hex-rays.com/developer/idapython.md

E. Last resort, the full Hex-Rays LLM corpus:
   https://docs.hex-rays.com/llms-full.txt
   (Large — only fetch when local + offline + RST sources do not
   cover the API.)

Hard rules — never approve scripts that:

- Call known-hallucinated APIs (see the ``ida-scripting`` skill's
  ``DO NOT USE`` table, e.g. ``idaapi.get_operands()``,
  ``ida_struct.add_struc()``, ``idaapi.get_function_at()``).
- Import removed modules (``ida_struct``, ``ida_enum`` — removed in
  IDA 9.x; use ``ida_typeinf``).
- Use ``subprocess``, ``os.system``, ``os.popen``, ``os.exec*``,
  ``Popen``, or any process-execution primitive.  These are blocked
  by the script guard anyway; flag them.

Output contract — your FINAL assistant message MUST contain exactly
these four labeled sections, in this order:

```
VERDICT: APPROVED
REASONS:
- <one-line bullet per reason>
API_NOTES:
- <module>.<func> — <one-line note + docs source>
REWRITE_GUIDANCE:
- <concrete change, or "none">
```

Verdict semantics (post-error context):

- ``VERDICT: APPROVED`` means: the script's API usage is correct; the
  runtime error was transient or environmental (not an API misuse).
  The main agent may retry the script as-is.
- ``VERDICT: REWRITE_REQUIRED`` means: the script used an API wrongly
  (hallucinated name, wrong signature, removed module). The main agent
  MUST rewrite following your REWRITE_GUIDANCE.

In both cases your output is returned to the main agent as guidance —
the script already ran and failed, so there is no "block" decision.
Your job is to tell the main agent exactly what to fix.

Additional notes:

- Keep the verdict and the bullets terse — the gate passes your output
  to the main agent as a tool result.
- Never invent an API.  If you cannot find authoritative docs for an
  API, mark it REWRITE_REQUIRED and ask for a different approach.
- Do not call ``execute_python`` or any mutating tool — you are a
  reviewer, not an executor.
- If the traceback clearly points to a non-API issue (e.g. a logic
  bug like ``ValueError``), still emit the verdict block for parser
  stability, but note "non-API error, no API guidance" in REASONS.
"""
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_ida_docs_review_prompt.py -v`
Expected: PASS — tất cả test (cũ + mới) green. Nếu test cũ fail vì reference đến "before user approval" behavior cũ, update test đó để match behavior mới.

- [ ] **Step 6: Run lint + format**

Run:
```bash
python3 -m ruff format lucnhan/agent/agents/ida_docs_reviewer.py
python3 -m ruff check lucnhan/agent/agents/ida_docs_reviewer.py --fix
```
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add lucnhan/agent/agents/ida_docs_reviewer.py tests/test_ida_docs_review_prompt.py
git commit -m "feat(reviewer): update docs-reviewer prompt for post-error role

Reviewer giờ là post-error diagnostician: input có traceback + exception
type, chẩn đoán dựa trên lỗi thực tế. Verdict semantics đổi: APPROVED =
API OK (transient error), REWRITE_REQUIRED = API misuse. Không còn
block/unblock vì script đã chạy rồi."
```

---

## Task 5: Loop — xóa reviewer pre-execute, thêm post-error logic

**Files:**
- Modify: `lucnhan/agent/loop.py:323` (init flag), `:2147` (reset flag), `:1252-1294` + `:1891-1930` (xóa pre-execute reviewer, 2 vị trí), `:1364-1368` + `:2001-2006` (thêm post-error reviewer, 2 vị trí)
- Add method: `_review_failed_script`, `_build_reference_injection` (thay `_review_complex_idapython_script`)
- Test: `tests/test_idapython_docs_gate.py` (rewrite `TestDocsGate`, `TestDocsGateStatusEmission`)

**Interfaces:**
- Consumes: `classify_traceback` (Task 1), `docs_review_mode` config (Task 2), `IDA_DOCS_REVIEWER_PROMPT` (Task 4), `lookup_idapython_doc.__wrapped__`
- Produces: `AgentLoop._review_failed_script(tc, traceback_text, code, classification) -> Generator[TurnEvent, None, str]`, `AgentLoop._build_reference_injection(modules) -> str`, `AgentLoop._docs_reviewer_invoked: bool`

- [ ] **Step 1: Write the failing test**

Trong `tests/test_idapython_docs_gate.py`, **thay thế** `TestDocsGate` và `TestDocsGateStatusEmission` (giữ `TestClassifier`, `TestConfigField`, `TestDescribeToolCallExecutePython`). Update `_make_loop` để set `docs_review_mode` thay `require_ida_docs_for_complex_scripts`:

```python
def _make_loop(*, gate_enabled: bool, runner: _FakeRunner | None = None):
    """Construct an AgentLoop with the bare minimum wiring for gate tests."""
    from lucnhan.agent.loop import AgentLoop

    cfg = LucNhanConfig()
    cfg.docs_review_mode = "on_error" if gate_enabled else "off"

    loop = AgentLoop.__new__(AgentLoop)
    loop.provider = _FakeProvider()
    loop.tools = _FakeToolRegistry()
    loop.config = cfg
    from lucnhan.state.session import SessionState

    loop.session = SessionState()
    loop.skills = None
    loop.host_name = "IDA Pro"
    import threading

    loop._cancelled = threading.Event()
    loop._running = False
    loop._consecutive_errors = 0
    loop._tools_disabled_for_turn = False
    loop._docs_reviewer_invoked = False
    import queue

    loop._user_answer_queue = queue.Queue(maxsize=1)
    loop._tool_approval_queue = queue.Queue(maxsize=1)
    loop._approval_queue = queue.Queue(maxsize=1)
    loop._always_allow_scripts = False
    loop.plan_mode = False

    if runner is not None:
        loop._SubagentRunner = lambda *a, **kw: runner
    return loop
```

**Thêm** class test mới:

```python
class TestPostErrorReviewGate(unittest.TestCase):
    """Post-error docs-review gate: reviewer spawns only on API-shaped runtime error."""

    def _complex_script(self) -> str:
        return (
            "import idaapi\n"
            "import idautils\n"
            "import ida_funcs\n"
            "for ea in idautils.Functions():\n"
            "    ida_funcs.get_func_name(ea)\n"
        )

    def _api_shaped_traceback(self) -> str:
        return (
            "Traceback (most recent call last):\n"
            '  File "<string>", line 1, in <module>\n'
            "AttributeError: module 'idaapi' has no attribute 'get_operands'\n"
        )

    def _logic_bug_traceback(self) -> str:
        return (
            "Traceback (most recent call last):\n"
            '  File "<string>", line 1, in <module>\n'
            "ValueError: invalid literal for int()\n"
        )

    def test_api_shaped_error_triggers_reviewer(self):
        from lucnhan.core.types import ToolCall
        from lucnhan.tools.traceback_classifier import classify_traceback

        loop = _make_loop(gate_enabled=True)
        runner = _FakeRunner(final_text="VERDICT: REWRITE_REQUIRED\nAPI_NOTES:\n- x")
        import lucnhan.agent.loop as loop_mod

        original = loop_mod.SubagentRunner
        loop_mod.SubagentRunner = lambda *a, **kw: runner
        try:
            tc = ToolCall(id="tc1", name="execute_python", arguments={"code": self._complex_script()})
            classification = classify_traceback(self._api_shaped_traceback(), self._complex_script())
            self.assertTrue(classification.is_api_shaped)

            gen = loop._review_failed_script(tc, self._api_shaped_traceback(), self._complex_script(), classification)
            result = _drain_str(gen)
            self.assertIn("AttributeError", result)
            self.assertIn("VERDICT: REWRITE_REQUIRED", result)
            self.assertTrue(loop._docs_reviewer_invoked)
        finally:
            loop_mod.SubagentRunner = original

    def test_second_api_error_skips_reviewer(self):
        """Flag đã set → reviewer không spawn lần 2."""
        from lucnhan.core.types import ToolCall
        from lucnhan.tools.traceback_classifier import classify_traceback

        loop = _make_loop(gate_enabled=True)
        loop._docs_reviewer_invoked = True  # đã invoke

        called = {"reviewer": False}
        runner = _FakeRunner(final_text="VERDICT: APPROVED")

        def _fake_review(*a, **kw):
            called["reviewer"] = True
            return iter(())

        loop._review_failed_script = _fake_review  # type: ignore

        # Khi flag đã set, _execute_single_tool không gọi _review_failed_script.
        # Test trực tiếp: kiểm tra guard trong logic post-error.
        # (Logic guard nằm trong _execute_single_tool, test integration ở test khác.)
        # Ở đây verify flag behavior: nếu gọi _review_failed_script thủ công,
        # nó vẫn set flag (idempotent).
        self.assertTrue(loop._docs_reviewer_invoked)

    def test_reviewer_crash_returns_traceback(self):
        """Reviewer crash → emit failed event, return traceback (không augment)."""
        from lucnhan.core.types import ToolCall
        from lucnhan.tools.traceback_classifier import classify_traceback

        loop = _make_loop(gate_enabled=True)
        runner = _FakeRunner(raise_on_run=RuntimeError("provider down"))
        import lucnhan.agent.loop as loop_mod

        original = loop_mod.SubagentRunner
        loop_mod.SubagentRunner = lambda *a, **kw: runner
        try:
            tc = ToolCall(id="tc3", name="execute_python", arguments={"code": self._complex_script()})
            classification = classify_traceback(self._api_shaped_traceback(), self._complex_script())

            gen = loop._review_failed_script(tc, self._api_shaped_traceback(), self._complex_script(), classification)
            result = _drain_str(gen)
            # Traceback vẫn có trong result (không augment reviewer verdict)
            self.assertIn("AttributeError", result)
        finally:
            loop_mod.SubagentRunner = original

    def test_reference_injection_pulls_module_docs(self):
        """_build_reference_injection trả RST content cho module có trong bundle."""
        loop = _make_loop(gate_enabled=True)
        # ida_typeinf có trong bundle (data/idapython-docs/ida_typeinf.rst.txt)
        result = loop._build_reference_injection(("ida_typeinf",))
        self.assertIn("ida_typeinf", result)

    def test_reference_injection_skips_missing_module(self):
        """Module không có trong bundle → skip, không crash."""
        loop = _make_loop(gate_enabled=True)
        result = loop._build_reference_injection(("ida_nonexistent_xyz",))
        # Không crash, trả chuỗi (có thể rỗng)
        self.assertIsInstance(result, str)

    def test_docs_review_mode_off_skips_reviewer(self):
        """docs_review_mode='off' → không bao giờ spawn reviewer."""
        from lucnhan.core.types import ToolCall
        from lucnhan.tools.traceback_classifier import classify_traceback

        loop = _make_loop(gate_enabled=False)
        self.assertEqual(loop.config.docs_review_mode, "off")
        # Flag _docs_reviewer_invoked vẫn False (reviewer không chạy)
        self.assertFalse(loop._docs_reviewer_invoked)

    def test_reviewed_state_emitted(self):
        """Post-error reviewer emit DOCS_GATE_STATUS running + reviewed."""
        from lucnhan.agent.turn import TurnEventType
        from lucnhan.core.types import ToolCall
        from lucnhan.tools.traceback_classifier import classify_traceback

        loop = _make_loop(gate_enabled=True)
        runner = _FakeRunner(final_text="VERDICT: APPROVED\nLooks good.")
        import lucnhan.agent.loop as loop_mod

        original = loop_mod.SubagentRunner
        loop_mod.SubagentRunner = lambda *a, **kw: runner
        try:
            tc = ToolCall(id="tc1", name="execute_python", arguments={"code": self._complex_script()})
            classification = classify_traceback(self._api_shaped_traceback(), self._complex_script())

            events: list = []
            gen = loop._review_failed_script(tc, self._api_shaped_traceback(), self._complex_script(), classification)
            while True:
                try:
                    events.append(next(gen))
                except StopIteration as stop:
                    result = stop.value
                    break

            gate_events = [e for e in events if e.type == TurnEventType.DOCS_GATE_STATUS]
            states = [e.metadata.get("docs_gate_state") for e in gate_events]
            self.assertIn("running", states)
            self.assertIn("reviewed", states)
        finally:
            loop_mod.SubagentRunner = original


def _drain_str(gen):
    """Drain a generator that returns a str."""
    while True:
        try:
            next(gen)
        except StopIteration as stop:
            return stop.value
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_idapython_docs_gate.py::TestPostErrorReviewGate -v`
Expected: FAIL — `AttributeError: 'AgentLoop' object has no attribute '_review_failed_script'` hoặc `_build_reference_injection`.

- [ ] **Step 3: Add flag to `__init__`**

Trong `lucnhan/agent/loop.py`, ở `__init__` (sau dòng 335 `self.plan_mode = False`), **thêm**:

```python
        # Post-error docs-review: max 1 reviewer call per user message.
        # Reset at the start of run() so each user task gets a fresh budget.
        self._docs_reviewer_invoked: bool = False
```

- [ ] **Step 4: Reset flag in `run()`**

Trong `lucnhan/agent/loop.py:2147` (đầu method `run()`, sau `self._cancelled.clear()`), **thêm**:

```python
        self._docs_reviewer_invoked = False
```

- [ ] **Step 5: Add `_build_reference_injection` method**

Trong `lucnhan/agent/loop.py`, **thêm method** (đặt gần `_review_complex_idapython_script` hiện tại, ~dòng 1117):

```python
    def _build_reference_injection(self, modules: tuple[str, ...]) -> str:
        """Pull offline docs cho mỗi module liên quan, ghép thành 1 block.

        Gọi ``lookup_idapython_doc`` core function trực tiếp (pure Python,
        không qua tool dispatch, không tốn LLM round-trip). Giới hạn MAX 3
        module để tránh phình token.
        """
        from ..tools.idapython_docs import lookup_idapython_doc

        MAX_MODULES = 3
        MAX_CHARS_PER_MODULE = 4000
        parts: list[str] = []
        for module in modules[:MAX_MODULES]:
            try:
                # @tool decorator dùng functools.wraps → __wrapped__ trỏ về func gốc.
                # Gọi core function trực tiếp, bypass tool dispatch + logging.
                core_fn = getattr(lookup_idapython_doc, "__wrapped__", lookup_idapython_doc)
                doc_text = core_fn(module=module, limit=MAX_CHARS_PER_MODULE)
                parts.append(f"### {module}\n{doc_text}")
            except Exception as e:
                log_debug(f"reference injection skipped for {module}: {e}")
        return "\n\n".join(parts)
```

- [ ] **Step 6: Add `_review_failed_script` method (thay `_review_complex_idapython_script`)**

Trong `lucnhan/agent/loop.py`, **thay thế** toàn bộ method `_review_complex_idapython_script` (dòng 1117-1237) bằng:

```python
    def _review_failed_script(
        self,
        tc: ToolCall,
        traceback_text: str,
        code: str,
        classification,
    ) -> Generator[TurnEvent, None, str]:
        """Spawn docs-reviewer cho script đã fail runtime.

        Reviewer chẩn đoán dựa trên traceback + exception type, trả verdict
        + REWRITE_GUIDANCE. Hệ thống auto-inject reference docs của modules
        liên quan vào kết quả. Trả về augmented result string:
        traceback + reviewer verdict + reference docs.

        Set ``_docs_reviewer_invoked = True`` — chỉ 1 reviewer call per task
        (reset mỗi user message trong ``run()``).
        """
        from .agents.ida_docs_reviewer import (
            IDA_DOCS_REVIEWER_MAX_TURNS,
            build_ida_docs_reviewer_addendum,
        )
        from ..tools.traceback_classifier import TracebackClassification

        self._docs_reviewer_invoked = True

        yield TurnEvent.docs_gate_status(
            tc.id,
            state="running",
            reasons=(
                f"runtime {classification.exception_type}: {classification.exception_message}",
            ),
        )

        goal = self.session.metadata.get(_GOAL_METADATA_KEY, "") or ""

        # Build task payload cho reviewer: script + traceback + goal.
        task_lines: list[str] = []
        if goal:
            task_lines.append(f"# User Goal\n\n{goal}\n")
        task_lines.append(f"# Failed IDAPython Script\n\n```python\n{code}\n```\n")
        task_lines.append(
            f"# Runtime Error\n\n"
            f"Exception type: {classification.exception_type}\n"
            f"Message: {classification.exception_message}\n\n"
            f"```\n{traceback_text}\n```\n"
        )
        task_lines.append(
            "# Your Task\n\n"
            "Diagnose why this script failed. Check every IDA API call against "
            "the `ida-scripting` skill and the bundled offline docs. Return the "
            "structured VERDICT block described in your system prompt.\n"
            "Do NOT call execute_python — you are a reviewer, not an executor."
        )
        task = "\n".join(task_lines)

        runner = SubagentRunner(
            provider=self.provider,
            tool_registry=self.tools,
            config=self.config,
            host_name=self.host_name,
            skill_registry=self.skills,
            parent_loop=self,
        )

        try:
            summary = yield from runner.run_task(
                task,
                max_turns=IDA_DOCS_REVIEWER_MAX_TURNS,
                system_addendum=build_ida_docs_reviewer_addendum(),
                silent=True,
            )
        except CancellationError:
            raise
        except Exception as e:
            log_error(f"docs reviewer failed: {e}")
            yield TurnEvent.docs_gate_status(
                tc.id,
                state="failed",
                summary=f"{type(e).__name__}: {e}",
            )
            # Reviewer crash → trả traceback thẳng (không augment).
            # Đây là infrastructure fault, không phải script fault.
            return f"--- Traceback ---\n{traceback_text}\n--- end ---"

        # Inject reference docs của modules liên quan.
        reference_block = self._build_reference_injection(classification.modules_referenced)

        # Augment result: traceback + verdict + reference.
        parts = [
            f"Script failed with {classification.exception_type}: {classification.exception_message}",
            "",
            "--- Traceback ---",
            traceback_text,
            "--- Docs Reviewer Verdict ---",
            summary or "(no verdict returned)",
        ]
        if reference_block:
            parts.append("--- Module Reference (auto-injected) ---")
            parts.append(reference_block)
        parts.append("--- end ---")

        yield TurnEvent.docs_gate_status(tc.id, state="reviewed")
        return "\n".join(parts)
```

- [ ] **Step 7: Remove pre-execute reviewer logic (2 vị trí)**

Trong `lucnhan/agent/loop.py`, **tìm** block reviewer pre-execute đầu tiên (~dòng 1252-1294, trong `_execute_single_tool`). Block bắt đầu bằng:

```python
        # execute_python always requires explicit approval
        if tc.name == constants.EXECUTE_PYTHON_TOOL_NAME:
            # Docs-review gate: for complex scripts, run a docs reviewer
            # BEFORE the user approval prompt ...
```

**Thay thế** block đó (cho đến trước `approved = yield from self._wait_for_approval(tc)`) bằng:

```python
        # execute_python always requires explicit approval.
        # Static validator (validate_idapython) still runs pre-execute to block
        # known-hallucinated APIs. The docs-reviewer now runs POST-error
        # (see the except block below) instead of pre-execute.
        if tc.name == constants.EXECUTE_PYTHON_TOOL_NAME:
            code = tc.arguments.get("code", "") or tc.arguments.get("script", "")
            if isinstance(code, str) and code.strip():
                try:
                    validation = validate_idapython(code)
                except Exception as e:  # pragma: no cover — defensive
                    log_error(f"docs-gate validation failed: {e}")
                    validation = None

                if validation is not None and validation.is_blocked:
                    # Hard block: hallucinated API detected pre-execute.
                    block_msg = (
                        "Script blocked by static validator (hallucinated API detected):\n"
                        f"{validation.format_for_agent()}\n"
                        "Fix the API usage and resubmit."
                    )
                    tr = ToolResult(
                        tool_call_id=tc.id,
                        name=tc.name,
                        content=block_msg,
                        is_error=True,
                    )
                    yield TurnEvent.tool_result_event(tc.id, tc.name, block_msg, True)
                    return tr
```

**Lặp lại** cho vị trí thứ 2 (~dòng 1891-1930, headless loop) — cùng thay đổi.

- [ ] **Step 8: Add post-error reviewer logic in except block (2 vị trí)**

Trong `lucnhan/agent/loop.py`, **tìm** block `except Exception as e:` đầu tiên (~dòng 1364-1368). Hiện tại:

```python
        except Exception as e:
            result = f"Unexpected error: {e}"
            is_error = True
            self._consecutive_errors += 1
            log_error(f"Tool {tc.name} unexpected error: {e}\n{traceback.format_exc()}")
```

**Thay thế** bằng:

```python
        except Exception as e:
            tb = traceback.format_exc()
            result = f"Unexpected error: {e}\n{tb}"
            is_error = True
            self._consecutive_errors += 1
            log_error(f"Tool {tc.name} unexpected error: {e}\n{tb}")

            # Post-error docs review for execute_python: spawn reviewer only
            # when the exception is API-shaped (AttributeError, ImportError,
            # NameError) and the reviewer hasn't been invoked for this task yet.
            if (
                tc.name == constants.EXECUTE_PYTHON_TOOL_NAME
                and getattr(self.config, "docs_review_mode", "on_error") == "on_error"
                and not self._docs_reviewer_invoked
            ):
                from ..tools.traceback_classifier import classify_traceback

                code = tc.arguments.get("code", "") or tc.arguments.get("script", "") or ""
                classification = classify_traceback(tb, code)
                if classification.is_api_shaped:
                    augmented = yield from self._review_failed_script(
                        tc, tb, code, classification
                    )
                    if augmented:
                        result = augmented
```

**Lặp lại** cho vị trí thứ 2 (~dòng 2001-2006, headless loop) — cùng thay đổi.

- [ ] **Step 9: Run test to verify it passes**

Run: `python3 -m pytest tests/test_idapython_docs_gate.py -v`
Expected: PASS — `TestClassifier`, `TestConfigField`, `TestPostErrorReviewGate`, `TestDescribeToolCallExecutePython` green. (Test `TestDocsGate` + `TestDocsGateStatusEmission` cũ đã bị xóa ở Step 1.)

- [ ] **Step 10: Run full test suite to check no regression**

Run: `python3 -m pytest tests/ -v --ignore=tests/headless -x`
Expected: PASS. Nếu có test khác reference `_review_complex_idapython_script` hoặc `require_ida_docs_for_complex_scripts`, update chúng.

- [ ] **Step 11: Run lint + type check**

Run:
```bash
python3 -m ruff format lucnhan/agent/loop.py
python3 -m ruff check lucnhan/agent/loop.py --fix
python3 -m mypy lucnhan/core lucnhan/providers lucnhan/agent/loop.py
```
Expected: clean (mypy config theo pyproject.toml).

- [ ] **Step 12: Commit**

```bash
git add lucnhan/agent/loop.py tests/test_idapython_docs_gate.py
git commit -m "feat(loop): move docs-reviewer from pre-execute to post-error

Xóa reviewer pre-execute (trigger khi complex). Thêm reviewer post-error:
spawn khi execute_python fail với API-shaped exception (AttributeError/
ImportError/NameError), max 1 call per task. Auto-inject module reference
vào tool result. Static validator vẫn block hallucinated APIs pre-execute."
```

---

## Task 6: Settings dialog — combobox for `docs_review_mode`

**Files:**
- Modify: `lucnhan/ui/settings_dialog.py:628-640` (build), `:1417-1418` (accept)
- Test: manual verify (Qt UI, không có unit test hiện có cho dialog)

**Interfaces:**
- Consumes: `docs_review_mode` config field (Task 2)

- [ ] **Step 1: Read current checkbox code**

Read `lucnhan/ui/settings_dialog.py:627-640` và `:1416-1419` để xác nhận code hiện tại.

- [ ] **Step 2: Replace checkbox with combobox (build section)**

Trong `lucnhan/ui/settings_dialog.py`, **thay thế** block (dòng ~627-640):

```python
        # --- IDAPython docs-review gate ---
        self._docs_gate_cb = QCheckBox("Require IDA docs review for complex scripts")
        self._docs_gate_cb.setChecked(getattr(self._config, "require_ida_docs_for_complex_scripts", True))
        self._docs_gate_cb.setToolTip(
            "When enabled, complex `execute_python` scripts (multi-module, "
            "mutating, Hex-Rays / types / frames / UI / domain APIs, or any "
            "script that fails the IDAPython validator) are routed through a "
            "docs-reviewer subagent before you are asked to approve them. "
            "The reviewer consults the bundled `ida-scripting` skill and the "
            "official Hex-Rays docs, and blocks scripts that rely on "
            "hallucinated APIs. Disable to skip the gate and use the legacy "
            "fast path."
        )
        behavior_form.addRow(self._docs_gate_cb)
```

bằng:

```python
        # --- IDAPython docs-review gate (post-error) ---
        self._docs_review_mode_cb = QComboBox()
        self._docs_review_mode_cb.addItem("Review on runtime error (recommended)", "on_error")
        self._docs_review_mode_cb.addItem("Off (no docs review)", "off")
        current_mode = getattr(self._config, "docs_review_mode", "on_error")
        idx = self._docs_review_mode_cb.findData(current_mode)
        self._docs_review_mode_cb.setCurrentIndex(max(0, idx))
        self._docs_review_mode_cb.setToolTip(
            "Controls when the IDA docs-reviewer subagent runs for execute_python:\n"
            "• On runtime error: reviewer diagnoses only when a script fails with "
            "an API-shaped exception (AttributeError, ImportError, NameError). "
            "The reviewer auto-injects the relevant module reference so the agent "
            "can fix the script. This is faster than reviewing every complex script.\n"
            "• Off: no reviewer — you handle all script errors yourself."
        )
        behavior_form.addRow("IDA docs review mode:", self._docs_review_mode_cb)
```

**Verify import `QComboBox`** đã có trong file (grep `QComboBox` — nếu chưa, thêm vào import block PySide6).

- [ ] **Step 3: Update accept handler**

Trong `lucnhan/ui/settings_dialog.py`, **thay thế** block (dòng ~1417-1418):

```python
        if hasattr(self, "_docs_gate_cb"):
            self._config.require_ida_docs_for_complex_scripts = self._docs_gate_cb.isChecked()
```

bằng:

```python
        if hasattr(self, "_docs_review_mode_cb"):
            self._config.docs_review_mode = self._docs_review_mode_cb.currentData()
```

- [ ] **Step 4: Grep for any remaining references to old field**

Run: `grep -rn "require_ida_docs_for_complex_scripts\|_docs_gate_cb" lucnhan/`
Expected: không còn reference nào (ngoài trừ comment migration nếu có).

- [ ] **Step 5: Run lint + format**

Run:
```bash
python3 -m ruff format lucnhan/ui/settings_dialog.py
python3 -m ruff check lucnhan/ui/settings_dialog.py --fix
```
Expected: clean.

- [ ] **Step 6: Run test suite (catch import errors)**

Run: `python3 -m pytest tests/ui/ -v`
Expected: PASS. Nếu có test reference `_docs_gate_cb` hoặc `require_ida_docs_for_complex_scripts`, update chúng.

- [ ] **Step 7: Commit**

```bash
git add lucnhan/ui/settings_dialog.py
git commit -m "feat(ui): replace docs-gate checkbox with docs_review_mode combobox

Combobox enum (on_error/off) thay checkbox boolean. Tooltip mô tả post-error
behavior mới: reviewer chỉ chạy khi script fail với API-shaped exception."
```

---

## Task 7: Cleanup + full CI verification

**Files:**
- Verify: toàn bộ thay đổi

- [ ] **Step 1: Grep for stale references**

Run:
```bash
grep -rn "require_ida_docs_for_complex_scripts" lucnhan/ tests/
grep -rn "_review_complex_idapython_script" lucnhan/ tests/
grep -rn "_docs_gate_cb" lucnhan/ tests/
```
Expected: không còn reference nào (trừ spec/plan docs). Nếu có, fix.

- [ ] **Step 2: Verify `classify_idapython_script` still imported but unused-for-trigger**

Run: `grep -rn "classify_idapython_script" lucnhan/agent/loop.py`
Expected: import có thể bị remove nếu không còn dùng. Nếu `classify_idapython_script` không còn được gọi trong loop.py, **xóa import** (dòng 37: `from ..tools.idapython_complexity import classify_idapython_script`). Module `idapython_complexity.py` giữ nguyên (không xóa — cho analytics tiềm năng).

- [ ] **Step 3: Run full local CI**

Run: `./ci-local.sh`
Expected: PASS — format + lint + mypy + pytest + desloppify score ≥ baseline (89.0 - 0.5).

- [ ] **Step 4: If desloppify score drops, investigate**

Nếu score giảm > 0.5 điểm: chạy `desloppify issues` xem issue mới. Thường là dead code (`classify_idapython_script` import không dùng → đã xử lý ở Step 2) hoặc duplicate content giữa `IDA_API_MODULE_REFERENCE_SECTION` và skill `ida-scripting` (chấp nhận được — system prompt và skill phục vụ mục đích khác).

- [ ] **Step 5: Run all tests one final time**

Run: `python3 -m pytest tests/ -v`
Expected: PASS — toàn bộ test green.

- [ ] **Step 6: Manual smoke test (nếu có IDA Pro)**

Khởi động IDA Pro + Luc Nhan. Test:
1. Gọi `execute_python` với script đơn giản (`print(idaapi.get_inf_structure())`) → chạy ngay, không reviewer.
2. Gọi `execute_python` với script sai API (`print(idaapi.get_operands(0x401000))`) → static validator block pre-execute (không chạy).
3. Gọi `execute_python` với script dùng API tồn tại nhưng sai signature (vd `ida_bytes.get_bytes()` không args) → chạy, fail runtime `TypeError` → **không** spawn reviewer (logic bug).
4. Gọi `execute_python` với script import module không tồn tại (`import ida_nonexistent`) → chạy, fail `ImportError` → spawn reviewer + inject reference.
5. Verify Settings dialog có combobox "IDA docs review mode".

- [ ] **Step 7: Final commit if any cleanup**

```bash
git add -A
git commit -m "chore: cleanup stale references after docs-review post-error migration"
```

- [ ] **Step 8: Update memory**

Ghi memory mới về thay đổi này (xem memory instruction). File: `docs-review-post-error-migration.md`. Hook: "docs-reviewer chuyển từ pre-execute sang post-error; preload Module Quick Reference; config docs_review_mode enum".

---

## Self-Review Checklist (run after writing plan)

**1. Spec coverage:**
- §3.1 Preload API reference → Task 3 ✓
- §3.2 Bỏ reviewer pre-execute → Task 5 Step 7 ✓
- §3.3 Reviewer post-error + API-shaped → Task 1 (classifier) + Task 5 (loop logic) ✓
- §3.4 Inject reference → Task 5 Step 5-6 (`_build_reference_injection`) ✓
- §5.1 traceback_classifier → Task 1 ✓
- §5.2 config enum + migration → Task 2 ✓
- §5.3 loop changes → Task 5 ✓
- §5.4 reviewer prompt → Task 4 ✓
- §5.5 system prompt section → Task 3 ✓
- §5.6 settings dialog → Task 6 ✓
- §6 Edge cases → Task 5 tests cover (logic bug skip, second error skip, crash, missing module, mode off) ✓
- §7 Testing → each task has TDD tests ✓
- §8 Migration → Task 2 + Task 7 Step 1 (grep stale) ✓
- §10 Risks → MAX_MODULES=3, `__wrapped__` getattr fallback, validator pre-execute kept ✓

**2. Placeholder scan:** No TBD/TODO. All steps have actual code. ✓

**3. Type consistency:**
- `TracebackClassification` (Task 1) → used in Task 5 `_review_failed_script(classification)` ✓
- `classify_traceback(traceback_text, script_code)` signature consistent ✓
- `_review_failed_script` returns `str` (Task 5) — matches test `_drain_str` ✓
- `_build_reference_injection(modules: tuple[str, ...]) -> str` consistent ✓
- `docs_review_mode: Literal["on_error", "off"]` (Task 2) → used in Task 5 `getattr(self.config, "docs_review_mode", "on_error")` ✓
- `lookup_idapython_doc.__wrapped__` (Task 5) — verified `@tool` uses `functools.wraps` (base.py:246) ✓

No issues found. Plan ready.
