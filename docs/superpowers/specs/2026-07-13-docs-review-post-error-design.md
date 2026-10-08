# Design: Docs-Review Gate chuyển từ Pre-Execute sang Post-Error (Hybrid)

- **Ngày:** 2026-07-13
- **Trạng thái:** Approved (brainstorming)
- **Tác giả:** tuna99 + Claude Code
- **Liên quan:** `lucnhan/agent/loop.py`, `lucnhan/agent/agents/ida_docs_reviewer.py`, `lucnhan/tools/idapython_complexity.py`, `lucnhan/tools/validate_idapython.py`, `lucnhan/core/config.py`

---

## 1. Bối cảnh & vấn đề

Hiện tại, mỗi script `execute_python` được đánh giá là "complex" (≥8 dòng code, dùng ≥2 module IDA, mutating, hoặc heavy module như hexrays/typeinf/frame/domain/kernwin/ua) sẽ spawn **docs-reviewer subagent** **trước** khi script execute. Reviewer là một LLM subagent (tối đa 6 turns, lookup docs offline/online) → chậm, tốn token, dù script có thể chạy ngon.

Người dùng phản hồi: "việc chạy execute_python và luôn review lại khiến việc chạy script rất lâu do phải review lại."

## 2. Mục tiêu

1. **Giảm độ trễ** cho script `execute_python` đúng — chạy ngay sau user approval, không chờ reviewer.
2. **Vẫn có safety net** cho script sai API — reviewer vẫn spawn, nhưng chỉ khi thật sự cần.
3. **Giảm tỷ lệ hallucinate ngay từ đầu** — preload API reference compact vào system prompt main agent.
4. **Giữ invariants security** — `execute_python` vẫn luôn cần user approval; static validator vẫn block hallucinated APIs trước execute.

## 3. Giải pháp (Hybrid)

Bốn thay đổi phối hợp:

### 3.1. Preload API reference compact vào system prompt main agent

Bổ sung một section mới vào system prompt IDA (`lucnhan/agent/prompts/base.py` + `ida.py`): **Module Router** (task→module map) + **Core Patterns** (code samples compact) + **DO NOT USE** table (đã có một phần trong `IDA_API_DISCIPLINE_SECTION`, sẽ bổ sung để đầy đủ).

Phần này lấy từ skill `ida-scripting/SKILL.md` (dòng 159-244) nhưng rút gọn — chỉ giữ bảng Module Router và Core Patterns thiết yếu. Bỏ phần verbose (Domain API, deep reference, fallback URL patterns — những thứ này reviewer subagent vẫn dùng khi cần).

**Lý do không load toàn bộ skill:** System prompt load mỗi session. ~150 dòng compact << ~370 dòng full skill. Token economy.

### 3.2. Bỏ reviewer pre-execute

Xóa logic trigger reviewer dựa trên `classify_idapython_script().is_complex` trong `_execute_single_tool` (loop.py, 2 vị trí: ~dòng 1252 và ~dòng 1891 — main loop + headless loop).

`classify_idapython_script` **không xóa** — giữ module cho analytics tiềm năng và vì `idapython_complexity.py` là pure function không hại. Chỉ không còn dùng để trigger reviewer.

### 3.3. Reviewer post-error — chỉ trigger khi runtime fail + API-shaped exception

Sau khi script execute và throw exception (trong block `except Exception` ở ~dòng 1364), parse traceback. Chỉ spawn reviewer nếu:

- Exception type thuộc **strict whitelist**: `AttributeError`, `ImportError`, `ModuleNotFoundError`, `NameError`.
- **Và** reviewer chưa được invoke cho task gốc hiện tại (max 1 reviewer call per task — flag `_docs_reviewer_invoked`, reset mỗi user message).

Các exception khác (`ValueError`, `TypeError`, `KeyError`, `IndexError`, `ZeroDivisionError`...) là logic bug → main agent tự sửa, không spawn reviewer.

### 3.4. Reviewer verdict → inject reference module liên quan

Sau khi reviewer trả verdict (`REWRITE_GUIDANCE` + `API_NOTES`), hệ thống tự động pull offline docs của các module IDA mà script reference (extract qua AST), append vào tool result. Main agent nhận: traceback + reviewer verdict + reference docs → sửa script lần 2 với context đầy đủ.

Reference injection gọi `lookup_idapython_doc` trực tiếp (pure Python, không qua tool dispatch, không tốn LLM round-trip). Giới hạn MAX 3 module để tránh phình token.

## 4. Kiến trúc & luồng dữ liệu

### 4.1. Luồng mới

```
execute_python tool call
  │
  ├─ validate_idapython(code)  ← GIỮ (static block hallucinated APIs, instant)
  │     └─ nếu blocked → return error ngay (KHÔNG execute, KHÔNG reviewer)
  │
  ├─ user approval (Allow/Deny) ← GIỮ (invariant: execute_python luôn approve)
  │     └─ nếu denied → return "denied by user"
  │
  ├─ execute script (script_guard.AST check + exec)
  │     ├─ thành công → return result (KHÔNG reviewer)
  │     └─ exception → parse traceback
  │           │
  │           ├─ logic bug (ValueError/TypeError/...)
  │           │     └─ return traceback thẳng (main agent tự sửa)
  │           │
  │           └─ API-shaped (AttributeError/ImportError/NameError)
  │                 │
  │                 ├─ reviewer flag đã set?
  │                 │     └─ YES → return traceback thẳng (đã có reference)
  │                 │
  │                 └─ NO → spawn docs-reviewer subagent
  │                       │
  │                       ├─ reviewer verdict + REWRITE_GUIDANCE + API_NOTES
  │                       │
  │                       ├─ extract modules liên quan từ script (AST)
  │                       │
  │                       ├─ lookup_idapython_doc(module=...) mỗi module
  │                       │     (gọi hàm Python trực tiếp, không qua LLM)
  │                       │
  │                       ├─ set reviewer flag = True (reset mỗi user msg)
  │                       │
  │                       └─ augment tool result:
  │                             traceback + reviewer verdict + reference docs
  │                             → main agent sửa script lần 2
  │
  └─ return ToolResult (sanitized)
```

### 4.2. Thay đổi so với hiện tại

| Khía cạnh | Hiện tại | Mới |
|-----------|----------|-----|
| Reviewer trigger | `classify_idapython_script().is_complex` (pre-execute) | Runtime fail + API-shaped exception (post-error) |
| Reviewer chạy | Trước user approval | Sau execute fail |
| Preload reference | Chỉ trong reviewer subagent (skill auto-load) | Trong main agent system prompt + reviewer vẫn có skill |
| Reference injection | Không (chỉ reviewer verdict) | Auto-inject module docs vào tool result |
| `classify_idapython_script` | Trigger reviewer | Không dùng để trigger (giữ cho analytics) |
| Config | `require_ida_docs_for_complex_scripts: bool` | `docs_review_mode: Literal["on_error", "off"]` |

## 5. Chi tiết kỹ thuật từng component

### 5.1. Module mới: `lucnhan/tools/traceback_classifier.py`

Pure function, không dependency IDA, không LLM, không globals. Operate trên traceback string + script source.

**Public API:**

```python
@dataclass(frozen=True)
class TracebackClassification:
    is_api_shaped: bool
    exception_type: str = ""
    exception_message: str = ""
    modules_referenced: tuple[str, ...] = ()

def classify_traceback(
    traceback_text: str,
    script_code: str = "",
) -> TracebackClassification:
    ...
```

**Logic:**

- `_API_SHAPED_EXCEPTIONS = frozenset({"AttributeError", "ImportError", "ModuleNotFoundError", "NameError"})`
- `_parse_exception_type(traceback_text)`: lấy dòng cuối traceback, split tại `:`, trả về tên type.
- `_extract_modules_from_code(code)`: parse AST, collect top-level module names bắt đầu bằng `ida_` hoặc thuộc `{idautils, idc, idaapi}`. Dùng cho reference injection.

**Design note:** Module này không thay thế `validate_idapython` (static pre-execute) — nó bổ sung phân loại **runtime** traceback. `validate_idapython` vẫn block hallucinated APIs trước execute; `traceback_classifier` bắt các API mà static chưa biết (API tồn tại nhưng dùng sai signature, hoặc module mới chưa có trong blocklist).

### 5.2. Config: `docs_review_mode` enum + migration

**Thay field:**

```python
# lucnhan/core/config.py — thay:
require_ida_docs_for_complex_scripts: bool = True

# bằng:
docs_review_mode: Literal["on_error", "off"] = "on_error"
```

**Migration** (trong `load()`, block setattr loop ~dòng 290-342):

- Nếu config file cũ có `require_ida_docs_for_complex_scripts: False` → set `docs_review_mode = "off"` (tôn trọng user đã tắt reviewer).
- Nếu `require_ida_docs_for_complex_scripts: True` hoặc không có → `docs_review_mode = "on_error"` (default mới).
- Field cũ `require_ida_docs_for_complex_scripts` không còn trong dataclass → nếu xuất hiện trong config file legacy, ignore (không crash).

**Lý do enum thay boolean:** Extensibility. Sau này nếu muốn thêm mode `"complex"` (review script nguy hiểm) hoặc `"always"` (paranoid), chỉ thêm giá trị enum thay vì migrate field lần nữa. Tránh naming debt — `require_ida_docs_for_complex_scripts` nói dối nếu semantics đổi.

### 5.3. Loop: `_execute_single_tool` + hàm mới `_review_failed_script`

**Trong `AgentLoop.__init__`** (~dòng 305-335): thêm flag

```python
self._docs_reviewer_invoked: bool = False
```

**Trong `AgentLoop.run()`** (~dòng 2141, đầu method): reset flag mỗi user message

```python
self._docs_reviewer_invoked = False
```

**Trong `_execute_single_tool`** (~dòng 1252-1294 và ~1891-1930 — 2 vị trí trùng lặp, main + headless): xóa block reviewer pre-execute dựa trên `complexity.is_complex`. Giữ lại `validate_idapython` (static block) + user approval.

**Trong block `except Exception`** (~dòng 1364-1368 và ~2001-2006): thêm post-error reviewer logic

```python
except Exception as e:
    tb = traceback.format_exc()
    result = f"Unexpected error: {e}\n{tb}"
    is_error = True
    self._consecutive_errors += 1
    log_error(f"Tool {tc.name} unexpected error: {e}\n{tb}")

    # --- NEW: post-error docs review for execute_python ---
    if (
        tc.name == constants.EXECUTE_PYTHON_TOOL_NAME
        and getattr(self.config, "docs_review_mode", "on_error") == "on_error"
    ):
        from ..tools.traceback_classifier import classify_traceback

        code = tc.arguments.get("code", "") or tc.arguments.get("script", "") or ""
        classification = classify_traceback(tb, code)

        if classification.is_api_shaped and not self._docs_reviewer_invoked:
            augmented = yield from self._review_failed_script(
                tc, tb, code, classification
            )
            if augmented:
                result = augmented
```

**Hàm mới `_review_failed_script`** (thay thế `_review_complex_idapython_script`):

```python
def _review_failed_script(
    self,
    tc: ToolCall,
    traceback_text: str,
    code: str,
    classification: TracebackClassification,
) -> Generator[TurnEvent, None, str]:
    """Spawn docs-reviewer cho script đã fail runtime.

    Trả về augmented result string: traceback + reviewer verdict + reference docs.
    Set _docs_reviewer_invoked = True (chỉ 1 lần per task).
    """
    self._docs_reviewer_invoked = True

    yield TurnEvent.docs_gate_status(
        tc.id,
        state="running",
        reasons=(f"runtime {classification.exception_type}: {classification.exception_message}",),
    )

    # Spawn reviewer (reuse SubagentRunner)
    summary = yield from self._run_docs_reviewer_subagent(tc, code, traceback_text, classification)

    # Inject reference docs của modules liên quan
    reference_block = self._build_reference_injection(classification.modules_referenced)

    # Augment result
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

**Hàm mới `_build_reference_injection`:**

```python
def _build_reference_injection(self, modules: tuple[str, ...]) -> str:
    """Pull offline docs cho mỗi module liên quan, ghép thành 1 block.

    Gọi lookup_idapython_doc core logic trực tiếp (pure Python, không LLM).
    Giới hạn MAX 3 module để tránh phình token.
    """
    from ..tools.idapython_docs import lookup_idapython_doc

    MAX_MODULES = 3
    parts: list[str] = []
    for module in modules[:MAX_MODULES]:
        try:
            # lookup_idapython_doc là @tool-decorated. functools.wraps preserve
            # __wrapped__ → gọi core function trực tiếp, bypass tool dispatch.
            doc_text = lookup_idapython_doc.__wrapped__(module=module, limit=4000)
            parts.append(f"### {module}\n{doc_text}")
        except Exception as e:
            log_debug(f"reference injection skipped for {module}: {e}")
    return "\n\n".join(parts)
```

**Verify `__wrapped__`:** `@tool` decorator dùng `@functools.wraps(func)` (base.py:246) → `wrapper.__wrapped__` trỏ về `func` gốc. Gọi `lookup_idapython_doc.__wrapped__(module="ida_typeinf", limit=4000)` chạy core logic đọc file RST, không qua tool dispatch (không log_trace, không exception wrapping). An toàn.

### 5.4. Reviewer prompt update: `ida_docs_reviewer.py`

Review giờ là **post-error diagnostician**, input khác trước:

- **Trước:** input = script + goal + complexity reasons + validation hints. Reviewer verify API trước execute.
- **Sau:** input = script + traceback (runtime fail) + exception type. Reviewer chẩn đoán **dựa trên lỗi thực tế**, không phải dự đoán.

Cập nhật `IDA_DOCS_REVIEWER_PROMPT`:

- Mô tả role: "You diagnose why an IDAPython script failed at runtime."
- Input section: thêm `# Runtime Error` chứa traceback + exception type.
- Output contract giữ nguyên (VERDICT + REASONS + API_NOTES + REWRITE_GUIDANCE).
- Verdict semantics đổi nhẹ: `APPROVED` = "script logic OK, error was transient/env issue, safe to retry as-is"; `REWRITE_REQUIRED` = "API usage wrong, must rewrite". (Cả 2 case đều trả result về main agent — không còn "block" vì script đã chạy rồi. Khác biệt chỉ là guidance cho main agent.)

**Lưu ý quan trọng:** Hàm `_review_failed_script` **không** parse verdict để block/unblock như `_review_complex_idapython_script` cũ. Script đã execute và fail rồi — không có "block" nữa. Reviewer verdict chỉ là **guidance** augment vào tool result. Main agent tự quyết sửa hay không.

### 5.5. System prompt: `IDA_API_MODULE_REFERENCE_SECTION`

Thêm section mới vào `lucnhan/agent/prompts/base.py`:

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

**Wire vào `ida.py`:**

```python
IDA_BASE_PROMPT = assemble_system_prompt(
    _IDA_INTRO,
    _IDA_TOOL_USAGE,
    _IDA_CAPABILITIES,
    IDA_API_MODULE_REFERENCE_SECTION,  # NEW
    IDA_API_DISCIPLINE_SECTION,
)
```

**Cập nhật `IDA_API_DISCIPLINE_SECTION`:** Section "Docs-review gate" hiện tại (dòng 322-332) mô tả behavior cũ (review trước khi execute). Cần rewrite thành mô tả behavior mới:

```
**Docs-review gate (post-error).** When an `execute_python` script fails at
runtime with an API-shaped exception (AttributeError, ImportError, NameError),
a docs-reviewer subagent diagnoses the failure and auto-injects the relevant
module reference into the tool result. You get one reviewer diagnosis per
task — after that, fix based on the reference already in context. To avoid
this round-trip, verify APIs against the Module Quick Reference above and
call `lookup_idapython_doc(module="<module>")` before writing the script.
```

### 5.6. Settings dialog: `lucnhan/ui/settings_dialog.py`

Thay checkbox boolean bằng combobox enum (~dòng 628-640 và ~1417-1418):

```python
# Build:
self._docs_review_mode_cb = QComboBox()
self._docs_review_mode_cb.addItem("Review on runtime error (recommended)", "on_error")
self._docs_review_mode_cb.addItem("Off (no docs review)", "off")
current = getattr(self._config, "docs_review_mode", "on_error")
idx = self._docs_review_mode_cb.findData(current)
self._docs_review_mode_cb.setCurrentIndex(max(0, idx))
self._docs_review_mode_cb.setToolTip(
    "Controls when the IDA docs-reviewer subagent runs for execute_python:\n"
    "• On runtime error: reviewer diagnoses only when a script fails with "
    "an API-shaped exception (AttributeError, ImportError, NameError).\n"
    "• Off: no reviewer — you handle all script errors yourself."
)
behavior_form.addRow("IDA docs review mode:", self._docs_review_mode_cb)

# Accept:
if hasattr(self, "_docs_review_mode_cb"):
    self._config.docs_review_mode = self._docs_review_mode_cb.currentData()
```

## 6. Xử lý edge cases

| Edge case | Xử lý |
|-----------|-------|
| Script fail với exception không phải API-shaped (ValueError, TypeError...) | Không spawn reviewer. Trả traceback thẳng. Main agent tự sửa. |
| Script fail API-shaped lần 2 (reviewer đã invoke) | Không spawn reviewer lần 2. Trả traceback thẳng (main agent đã có reference từ lần 1). |
| Reviewer subagent crash (provider down) | Emit `DOCS_GATE_STATUS` state="failed", return traceback thẳng (không augment). Main agent tự xử. Kế thừa Decision #6 từ code hiện tại. |
| Script không import module IDA nào (pure stdlib) | `modules_referenced = ()` → reference block rỗng → chỉ augment traceback + verdict. |
| Module không có trong offline bundle | `lookup_idapython_doc.__wrapped__` trả error message "Module not in offline bundle" → skip module đó, log debug. |
| `docs_review_mode = "off"` | Bỏ qua hoàn toàn post-error reviewer. Trả traceback thẳng. |
| Config legacy có `require_ida_docs_for_complex_scripts` | Migration trong `load()`: False → "off", True/missing → "on_error". |
| Cancellation giữa reviewer chạy | `CancellationError` propagate lên outer loop (giữ behavior hiện tại). |
| Script bị static validator block | Không execute → không có runtime error → reviewer không spawn. (Validator vẫn pre-execute, chỉ reviewer đổi sang post-error.) |

## 7. Testing

### 7.1. Unit tests mới: `tests/tools/test_traceback_classifier.py`

- `test_attribute_error_is_api_shaped`
- `test_import_error_is_api_shaped`
- `test_module_not_found_is_api_shaped`
- `test_name_error_is_api_shaped`
- `test_value_error_is_not_api_shaped`
- `test_type_error_is_not_api_shaped`
- `test_empty_traceback_returns_not_api_shaped`
- `test_extract_modules_from_imports`
- `test_extract_modules_from_from_imports`
- `test_extract_modules_no_ida_modules`
- `test_extract_modules_syntax_error_returns_empty`
- `test_exception_message_extracted`

### 7.2. Cập nhật `tests/test_idapython_docs_gate.py`

Test hiện tại dựa trên `_review_complex_idapython_script` (pre-execute). Cần:

- **Xóa/rewrite** các test gọi `_review_complex_idapython_script` trực tiếp (method sẽ bị xóa/đổi tên).
- **Thêm test mới** cho `_review_failed_script`:
  - `test_api_shaped_error_triggers_reviewer`
  - `test_logic_bug_error_skips_reviewer`
  - `test_second_api_error_skips_reviewer` (flag đã set)
  - `test_reviewer_crash_returns_traceback` (không augment)
  - `test_reference_injection_pulls_module_docs`
  - `test_reference_injection_skips_missing_module`
  - `test_docs_review_mode_off_skips_reviewer`
- **Giữ** `TestClassifier` (classify_idapython_script vẫn tồn tại, không đổi).
- **Giữ** `TestDescribeToolCallExecutePython` (không liên quan).
- **Cập nhật** `TestConfigField` → test `docs_review_mode` round-trip + migration từ legacy field.

### 7.3. Integration test: reference injection

Test `_build_reference_injection` gọi thật `lookup_idapython_doc.__wrapped__` với module có trong bundle (vd `ida_typeinf`) → assert trả RST content. Test với module không có (vd `ida_nonexistent`) → assert skip.

### 7.4. System prompt test

Test `IDA_API_MODULE_REFERENCE_SECTION` có trong `IDA_BASE_PROMPT`. Test không trùng lặp nội dung với `IDA_API_DISCIPLINE_SECTION`.

## 8. Migration & backward compat

- **Config file legacy:** User có config.json cũ với `require_ida_docs_for_complex_scripts`. Sau upgrade, `load()` migration → `docs_review_mode`. Config file ghi lại field mới ở lần save tiếp theo. Field cũ bị bỏ (không crash nếu còn trong file).
- **Test file:** `tests/test_idapython_docs_gate.py` có test reference `_review_complex_idapython_script`. Phải cập nhật trước khi xóa method, nếu không import error.
- **Skill `ida-scripting`:** Không đổi. Reviewer subagent vẫn auto-load skill khi spawn (post-error). Main agent giờ có Module Quick Reference trong system prompt, nhưng skill vẫn có giá trị khi cần deep reference (ctree, microcode, hooks).

## 9. Những gì KHÔNG thay đổi

- `execute_python` vẫn luôn cần user approval (security invariant).
- `validate_idapython` static validator vẫn chạy pre-execute, block hallucinated APIs.
- `script_guard._check_ast` vẫn block subprocess/dunder/reflective access.
- `classify_idapython_script` module giữ nguyên (không xóa) — chỉ không dùng để trigger reviewer.
- Skill `ida-scripting` không đổi.
- `lookup_idapython_doc` tool không đổi (chỉ gọi core function trực tiếp trong injection).
- `TurnEvent.DOCS_GATE_STATUS` event type giữ nguyên (reuse cho post-error state).
- Cancellation handling giữ nguyên.

## 10. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| Script hallucinated API chạy thật trước khi bị bắt → có thể cause side effect (mutation) | `validate_idapython` static validator vẫn pre-execute, bắt phần lớn hallucinated APIs đã biết. Reviewer post-error chỉ là safety net cho API chưa có trong blocklist. Mutation tools vẫn có `/undo`. |
| Main agent không nghe reviewer guidance, retry cùng script sai | Max 1 reviewer call per task. Lần 2 fail → traceback thẳng, main agent tự xử. Nếu vẫn loop, `_consecutive_errors >= 5` sẽ disable tools (logic hiện tại ở loop.py:664). |
| Reference injection phình token (nhiều module) | MAX 3 module, limit 4000 chars mỗi module. Total tối đa ~12KB — dưới TOOL_RESULT_TRUNCATE_LEN (8000) nếu trim, hoặc chấp nhận slightly over cho context hữu ích. |
| `__wrapped__` không tồn tại nếu `@tool` decorator đổi | Verify ở implementation: `hasattr(lookup_idapython_doc, "__wrapped__")`. Nếu không có, fallback gọi qua `ToolRegistry.execute()` hoặc extract core logic ra helper riêng. |
| 2 vị trí trùng lặp logic trong loop.py (main + headless) | Cần update cả 2. Nếu thấy trùng lặp quá, extract helper chung (nhưng đó là refactor riêng, không thuộc scope này). |

## 11. Success criteria

- [ ] Script `execute_python` đúng chạy ngay sau approval, không chờ reviewer.
- [ ] Script fail với `AttributeError`/`ImportError`/`NameError` → spawn reviewer 1 lần, inject reference.
- [ ] Script fail với `ValueError`/`TypeError` → không spawn reviewer.
- [ ] Script fail API-shaped lần 2 → không spawn reviewer (flag đã set).
- [ ] `docs_review_mode = "off"` → không bao giờ spawn reviewer.
- [ ] Config legacy `require_ida_docs_for_complex_scripts` migrate đúng.
- [ ] System prompt có Module Quick Reference.
- [ ] `./ci-local.sh` pass (format + lint + mypy + pytest + desloppify).
- [ ] Test coverage ≥ 80% cho code mới.
