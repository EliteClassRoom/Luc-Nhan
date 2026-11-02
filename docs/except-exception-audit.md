# Kiểm kê `except Exception` trong `lucnhan/`

**Ngày:** 2026-11-02
**Phạm vi:** toàn bộ `lucnhan/**/*.py` (99 file có hit).
**Nhánh / commit tại thời điểm kiểm kê:** `fix/narrow-except-exception` @ `00592c9`
(nhánh đang checkout trong worktree; task mô tả nhánh `master` — kiểm kê thực tế trên nhánh đang có).
**Lệnh grep đã dùng:**

```bash
rg -n "except Exception" lucnhan -g '*.py'
# → 410 dòng khớp (409 handler `except Exception` thực sự + 1 dòng nằm trong docstring)
```

**Baseline test:** `4192 passed, 170 skipped` — *baseline do director cung cấp*, không chạy lại trong
phiên kiểm kê này (read-only, không sửa source).

> ⚠️ **Đây là trạng thái TRƯỚC khi sửa.** Số dòng trong bảng là vị trí tại thời điểm kiểm kê; sau khi
> áp dụng thay đổi, line number sẽ lệch. Hãy dùng `file:line` + tên hàm/class để định vị lại.
>
> **Trạng thái sau Turn A + Turn B (2026-11-02, `fix/narrow-except-exception`):** đã sửa **18 chỗ**
> (11 ở Turn A, 7 ở Turn B) — xem `## Đã sửa trong lượt này`. Số handler `except Exception` còn lại
> trong `lucnhan/**/*.py`: **398 handler thật** (đếm AST), **399 dòng khớp `rg`** trên **94 file**
> (1 dòng là docstring `ui/panel_core.py:3457`). Các số **A=32 / B=160 / C=217** và **409 handler**
> bên dưới là **trạng thái TRƯỚC khi sửa**, giữ nguyên để đối chiếu. Phần trạng thái ở cuối mỗi dòng
> đã sửa ghi commit + line mới + test tương ứng.

## Tổng số hit

| Chỉ số | Giá trị |
|---|---|
| Tổng dòng khớp `rg` | 410 |
| Handler `except Exception` thực sự (AST xác nhận) | **409** |
| Dòng khớp không phải handler | 1 — `lucnhan/ui/panel_core.py:3457`, nằm trong docstring của `_history_load_worker` (nhắc tới `except Exception` trong mô tả) |
| File có hit | 99 |

## Đếm theo loại

| Loại | Số | Mô tả |
|---|---|---|
| **A — ưu tiên sửa** | **32** | nằm trong module ưu tiên **và** nuốt lỗi mà không log / không trả lỗi về caller |
| ├─ A1 mutation / undo / tool-dispatch | 5 | `agent/mutation.py`, `agent/loop.py` (mutation, undo, tool dispatch, knowledge ingest) |
| ├─ A2 `tools/` + `ida/tools/` | 3 | bề mặt thực thi tool |
| ├─ A3 `memory/` | 11 | ghi/đọc bộ nhớ trung tâm |
| └─ A-?(giữ) — *không chắc* | 13 | có log nhưng mức `debug`, hoặc hợp đồng đã ghi rõ best-effort → **không đoán bừa thành A** |
| **B — giữ** | **160** | ranh giới UI/Qt, cleanup/finally, `shutdown()`, thread worker, telemetry best-effort |
| **C — giữ nguyên** | **217** | đã `log.exception`/`log.warning` kèm re-raise hoặc trả `ToolError`; hoặc là import-probe (`DO-NOT-TOUCH`) |
| **Tổng** | **409** | |

Trong nhóm C có **40** handler là **import-probe** (`importlib.import_module` / `from x import y` của
module tùy chọn) — đánh dấu **DO-NOT-TOUCH**, không được thu hẹp. Ngoài ra **11** handler
ném `ToolError` và **34** handler re-raise (sau cleanup/rollback) — lỗi vẫn truyền về caller.

## Nhóm A — danh sách đề xuất sửa (ưu tiên)

Có **32** mục: **19** chắc chắn nên sửa (A1/A2/A3) và **13** mục
*A-?(giữ)* — đã log nhưng mức `debug` hoặc hợp đồng ghi rõ best-effort, **không chắc** có đáng sửa nên
không đoán bừa thành A.

| Mức | Vị trí | Hàm/Class | Vì sao nên sửa |
|---|---|---|---|
| `A1` | `lucnhan/agent/loop.py:2097` | `AgentLoop._execute_single_tool` | docs-gate validation lỗi → `validation=None`, chống hallucination bị bỏ qua mà không chặn script |
| `A1` | `lucnhan/agent/loop.py:2308` | `AgentLoop._handle_exploration_report_tool` | ingest exploration_report lỗi → tool vẫn trả kết quả thành công, finding không vào memory — **ĐÃ SỬA** (`75e02ec`; mới: `loop.py:2318-2319`; test: `TestKnowledgeIngestErrorLogging`).
| `A1` | `lucnhan/agent/loop.py:2481` | `AgentLoop._handle_research_note_tool` | ingest research_note lỗi → tool vẫn trả 'Note saved', ghi chú không vào memory — **ĐÃ SỬA** (`75e02ec`; mới: `loop.py:2492-2493`; test: `TestKnowledgeIngestErrorLogging`).
| `A1` | `lucnhan/agent/loop.py:2895` | `AgentLoop._verify_mutation` | post-state verification lỗi → mutation không được đẩy vào undo stack (mất undo) mà chỉ log_debug |
| `A1` | `lucnhan/agent/mutation.py:1145` | `capture_pre_state` | capture_pre_state nuốt lỗi → `pre` thiếu state, bản ghi undo vẫn được tạo với pre-state rỗng |
| `A2` | `lucnhan/ida/tools/function_index.py:132` | `_build_index` | getseg lỗi → `pass` ⇒ cờ `is_import` sai âm thầm, có thể đổi tên hàm thuộc import |
| `A2` | `lucnhan/ida/tools/types_tools.py:980` | `get_variable_type` | decompile lỗi → return `""` ⇒ LLM tưởng không có type; đây chính là pre-state cho undo — **ĐÃ SỬA** (`93ea290`; mới: `types_tools.py:983`, `get_variable_type`; test: `TestGetVariableTypeDecompileFailure`).
| `A2` | `lucnhan/tools/cache.py:72` | `ToolResultCache._make_key` | dựng cache-key lỗi → `frozen=()` cho mọi args ⇒ mọi tool dùng chung 1 khoá cache, trả nhầm kết quả |
| `A3` | `lucnhan/memory/context.py:242` | `build_retrieved_context_with_pack` | retrieve lỗi → return `("", None)`: khối Retrieved Knowledge biến mất khỏi prompt, không log — **ĐÃ SỬA** (`7e09bdd`; mới: `context.py:243-245`; test: `TestBuildRetrievedContextFailure`).
| `A3` | `lucnhan/memory/ingest.py:168` | `ingest_save_memory` | upsert memory + append_observation lỗi → `pass`, không log: memory lưu mất mà không ai biết — **ĐÃ SỬA** (`b71a243`; mới: `ingest.py:171-173`; test: `test_ingest_save_memory_logs_jsonl_failure`).
| `A3` | `lucnhan/memory/ingest.py:338` | `_write_exploration_to_jsonl` | upsert_entity lỗi → `pass`, không log: entity của finding không có trong knowledge graph — **ĐÃ SỬA** (`b71a243`; mới: `ingest.py:341-342`; test: `test_exploration_entity_write_failure_is_logged`).
| `A3` | `lucnhan/memory/ingest.py:382` | `_write_exploration_to_jsonl` | upsert_memory + append_observation lỗi → `pass`, không log: finding không được ghi — **ĐÃ SỬA** (`b71a243`; mới: `ingest.py:385-386`).
| `A3` | `lucnhan/memory/ingest.py:557` | `_write_research_to_jsonl` | upsert note-entity lỗi → `pass`, không log: ghi chú mất liên kết trong graph — **ĐÃ SỬA** (`b71a243`; mới: `ingest.py:561`; test: `test_research_note_entity_write_failure_is_logged`).
| `A3` | `lucnhan/memory/ingest.py:584` | `_write_research_to_jsonl` | upsert function-entity + relation lỗi → `pass`, không log: quan hệ 'mentions' mất — **ĐÃ SỬA** (`b71a243`; mới: `ingest.py:588`).
| `A3` | `lucnhan/memory/ingest.py:613` | `_write_research_to_jsonl` | upsert note-memory lỗi → `pass`, không log: memory của ghi chú không được tạo — **ĐÃ SỬA** (`b71a243`; mới: `ingest.py:617`; test: `test_research_note_memory_write_failure_is_logged`).
| `A3` | `lucnhan/memory/ingest.py:640` | `_write_research_to_jsonl` | upsert relation 'related_to' lỗi → `pass`, không log: quan hệ ghi chú liên quan mất — **ĐÃ SỬA** (`b71a243`; mới: `ingest.py:644`; test: `test_research_note_related_write_failure_is_logged`).
| `A3` | `lucnhan/memory/ingest.py:662` | `_write_research_to_jsonl` | append_observation lỗi → `pass`, không log: mục timeline của ghi chú mất — **ĐÃ SỬA** (`b71a243`; mới: `ingest.py:666`; test: `test_research_note_observation_failure_is_logged`).
| `A3` | `lucnhan/memory/report.py:440` | `build_report_context` | list_notes lỗi → `pass`: phần trích dẫn ghi chú mất khỏi report, không log — **ĐÃ SỬA** (`7e09bdd`; mới: `report.py:441-442`; test: `TestBuildReportContextNotesFailure`).
| `A3` | `lucnhan/memory/retrieve.py:348` | `retrieve` | list_notes lỗi → `parsed_notes=[]`: trích dẫn ghi chú mất khỏi context, không log — **ĐÃ SỬA** (`b71a243`; mới: `retrieve.py:349-351`; test: `TestRetrieveNotesFailure`).
| `A-?(giữ)` | `lucnhan/agent/loop.py:861` | `AgentLoop._build_system_prompt` | log_debug ⇒ mất `get_binary_info` trong system prompt (chỉ ảnh hưởng context). Không chắc có đáng sửa |
| `A-?(giữ)` | `lucnhan/agent/loop.py:866` | `AgentLoop._build_system_prompt` | log_debug ⇒ mất cursor/function trong system prompt (chỉ ảnh hưởng context). Không chắc có đáng sửa |
| `A-?(giữ)` | `lucnhan/agent/loop.py:882` | `AgentLoop._build_system_prompt` | log_debug ⇒ mất central-memory context trong system prompt (chỉ ảnh hưởng context). Không chắc có đáng sửa |
| `A-?(giữ)` | `lucnhan/agent/loop.py:1009` | `AgentLoop._build_retrieved_knowledge_section` | `pass`, không log ⇒ mất metadata `last_knowledge_retrieval` (chỉ ảnh hưởng chỉ báo UI). Không chắc có đáng sửa — **ĐÃ SỬA** (`75e02ec`; mới: `loop.py:1010-1011`; test: `TestKnowledgeMetadataLogging`).
| `A-?(giữ)` | `lucnhan/agent/loop.py:1013` | `AgentLoop._build_retrieved_knowledge_section` | log_debug + return `""` ⇒ cả khối Retrieved Knowledge biến mất khỏi prompt (log ở mức debug, mặc định không thấy). Đã ghi chú là best-effort — không chắc có đáng sửa |
| `A-?(giữ)` | `lucnhan/ida/tools/emulation.py:1023` | `_pc` | `return entry_pc` khi đọc PC lỗi ⇒ PC sai được báo cho LLM, không log. Không chắc có đáng sửa |
| `A-?(giữ)` | `lucnhan/ida/tools/emulation.py:1237` | `run_emulation` | `continue` khi đọc register lỗi ⇒ register thiếu trong `final_registers`, không log. Không chắc có đáng sửa |
| `A-?(giữ)` | `lucnhan/ida/tools/microcode_optim.py:69` | `DynamicInsnOptimizer.func` | log_debug + `return 0` ⇒ optimizer động âm thầm không làm gì (NOP 0 lần). Không chắc có đáng sửa |
| `A-?(giữ)` | `lucnhan/ida/tools/microcode_optim.py:88` | `DynamicBlockOptimizer.func` | log_debug + `return 0` ⇒ optimizer động âm thầm không làm gì (NOP 0 lần). Không chắc có đáng sửa |
| `A-?(giữ)` | `lucnhan/ida/tools/strings.py:75` | `_iter_ida_strings` | `text=""` cho từng string lỗi, không log ⇒ entry rỗng lọt vào string cache. Không chắc có đáng sửa |
| `A-?(giữ)` | `lucnhan/memory/ingest.py:104` | `make_store` | `make_store` trả `(None, None)`, không log ⇒ lỗi dựng store bị lẫn với 'không có IDB'. Hợp đồng đã ghi rõ là best-effort — không chắc có đáng sửa — **ĐÃ SỬA** (`b71a243`; mới: `ingest.py:106-108`; test: `test_make_store_logs_path_build_failure`).

## Backlog

Các mục A vượt quá 30 chỗ đầu (xếp theo thứ tự ưu tiên A1 → A2 → A3 → A-?(giữ)):

| `A-?(giữ)` | `lucnhan/memory/ingest.py:539` | `_write_research_to_jsonl` | `parsed=None` khi parse note lỗi ⇒ mất genre/tags/addresses của ghi chú, không log. Không chắc có đáng sửa — **BACKLOG** (parse_note; xem `## Backlog còn lại`).
| `A-?(giữ)` | `lucnhan/tools/skill_reference.py:68` | `_reference_root` | `pass` khi đọc config lỗi ⇒ user skill directory bị bỏ qua âm thầm (mất override), không log. Không chắc có đáng sửa |

## Bảng chi tiết

| # | File:Line | Hàm/Class | Loại | Nuốt lỗi? | Lý do 1 dòng |
|---|---|---|---|---|---|
| 1 | `lucnhan/agent/loop.py:2097` | `AgentLoop._execute_single_tool` | A1 | log-only | docs-gate validation lỗi → `validation=None`, chống hallucination bị bỏ qua mà không chặn script |
| 2 | `lucnhan/agent/loop.py:2308` | `AgentLoop._handle_exploration_report_tool` | A1 | log-only | ingest exploration_report lỗi → tool vẫn trả kết quả thành công, finding không vào memory — **ĐÃ SỬA** (`75e02ec`; mới: `loop.py:2318-2319`; test: `TestKnowledgeIngestErrorLogging`).
| 3 | `lucnhan/agent/loop.py:2481` | `AgentLoop._handle_research_note_tool` | A1 | log-only | ingest research_note lỗi → tool vẫn trả 'Note saved', ghi chú không vào memory — **ĐÃ SỬA** (`75e02ec`; mới: `loop.py:2492-2493`; test: `TestKnowledgeIngestErrorLogging`).
| 4 | `lucnhan/agent/loop.py:2895` | `AgentLoop._verify_mutation` | A1 | return <default> | post-state verification lỗi → mutation không được đẩy vào undo stack (mất undo) mà chỉ log_debug |
| 5 | `lucnhan/agent/mutation.py:1145` | `capture_pre_state` | A1 | log-only | capture_pre_state nuốt lỗi → `pre` thiếu state, bản ghi undo vẫn được tạo với pre-state rỗng |
| 6 | `lucnhan/ida/tools/function_index.py:132` | `_build_index` | A2 | pass | getseg lỗi → `pass` ⇒ cờ `is_import` sai âm thầm, có thể đổi tên hàm thuộc import |
| 7 | `lucnhan/ida/tools/types_tools.py:980` | `get_variable_type` | A2 | return <default> | decompile lỗi → return `""` ⇒ LLM tưởng không có type; đây chính là pre-state cho undo — **ĐÃ SỬA** (`93ea290`; mới: `types_tools.py:983`, `get_variable_type`; test: `TestGetVariableTypeDecompileFailure`).
| 8 | `lucnhan/tools/cache.py:72` | `ToolResultCache._make_key` | A2 | nuốt, không log | dựng cache-key lỗi → `frozen=()` cho mọi args ⇒ mọi tool dùng chung 1 khoá cache, trả nhầm kết quả |
| 9 | `lucnhan/memory/context.py:242` | `build_retrieved_context_with_pack` | A3 | return <default> | retrieve lỗi → return `("", None)`: khối Retrieved Knowledge biến mất khỏi prompt, không log — **ĐÃ SỬA** (`7e09bdd`; mới: `context.py:243-245`; test: `TestBuildRetrievedContextFailure`).
| 10 | `lucnhan/memory/ingest.py:168` | `ingest_save_memory` | A3 | pass | upsert memory + append_observation lỗi → `pass`, không log: memory lưu mất mà không ai biết — **ĐÃ SỬA** (`b71a243`; mới: `ingest.py:171-173`; test: `test_ingest_save_memory_logs_jsonl_failure`).
| 11 | `lucnhan/memory/ingest.py:338` | `_write_exploration_to_jsonl` | A3 | pass | upsert_entity lỗi → `pass`, không log: entity của finding không có trong knowledge graph — **ĐÃ SỬA** (`b71a243`; mới: `ingest.py:341-342`; test: `test_exploration_entity_write_failure_is_logged`).
| 12 | `lucnhan/memory/ingest.py:382` | `_write_exploration_to_jsonl` | A3 | pass | upsert_memory + append_observation lỗi → `pass`, không log: finding không được ghi — **ĐÃ SỬA** (`b71a243`; mới: `ingest.py:385-386`).
| 13 | `lucnhan/memory/ingest.py:557` | `_write_research_to_jsonl` | A3 | pass | upsert note-entity lỗi → `pass`, không log: ghi chú mất liên kết trong graph — **ĐÃ SỬA** (`b71a243`; mới: `ingest.py:561`; test: `test_research_note_entity_write_failure_is_logged`).
| 14 | `lucnhan/memory/ingest.py:584` | `_write_research_to_jsonl` | A3 | pass | upsert function-entity + relation lỗi → `pass`, không log: quan hệ 'mentions' mất — **ĐÃ SỬA** (`b71a243`; mới: `ingest.py:588`).
| 15 | `lucnhan/memory/ingest.py:613` | `_write_research_to_jsonl` | A3 | pass | upsert note-memory lỗi → `pass`, không log: memory của ghi chú không được tạo — **ĐÃ SỬA** (`b71a243`; mới: `ingest.py:617`; test: `test_research_note_memory_write_failure_is_logged`).
| 16 | `lucnhan/memory/ingest.py:640` | `_write_research_to_jsonl` | A3 | pass | upsert relation 'related_to' lỗi → `pass`, không log: quan hệ ghi chú liên quan mất — **ĐÃ SỬA** (`b71a243`; mới: `ingest.py:644`; test: `test_research_note_related_write_failure_is_logged`).
| 17 | `lucnhan/memory/ingest.py:662` | `_write_research_to_jsonl` | A3 | pass | append_observation lỗi → `pass`, không log: mục timeline của ghi chú mất — **ĐÃ SỬA** (`b71a243`; mới: `ingest.py:666`; test: `test_research_note_observation_failure_is_logged`).
| 18 | `lucnhan/memory/report.py:440` | `build_report_context` | A3 | pass | list_notes lỗi → `pass`: phần trích dẫn ghi chú mất khỏi report, không log — **ĐÃ SỬA** (`7e09bdd`; mới: `report.py:441-442`; test: `TestBuildReportContextNotesFailure`).
| 19 | `lucnhan/memory/retrieve.py:348` | `retrieve` | A3 | nuốt, không log | list_notes lỗi → `parsed_notes=[]`: trích dẫn ghi chú mất khỏi context, không log — **ĐÃ SỬA** (`b71a243`; mới: `retrieve.py:349-351`; test: `TestRetrieveNotesFailure`).
| 20 | `lucnhan/agent/loop.py:861` | `AgentLoop._build_system_prompt` | A-?(giữ) | log-only | log_debug ⇒ mất `get_binary_info` trong system prompt (chỉ ảnh hưởng context). Không chắc có đáng sửa |
| 21 | `lucnhan/agent/loop.py:866` | `AgentLoop._build_system_prompt` | A-?(giữ) | log-only | log_debug ⇒ mất cursor/function trong system prompt (chỉ ảnh hưởng context). Không chắc có đáng sửa |
| 22 | `lucnhan/agent/loop.py:882` | `AgentLoop._build_system_prompt` | A-?(giữ) | log-only | log_debug ⇒ mất central-memory context trong system prompt (chỉ ảnh hưởng context). Không chắc có đáng sửa |
| 23 | `lucnhan/agent/loop.py:1009` | `AgentLoop._build_retrieved_knowledge_section` | A-?(giữ) | pass | `pass`, không log ⇒ mất metadata `last_knowledge_retrieval` (chỉ ảnh hưởng chỉ báo UI). Không chắc có đáng sửa — **ĐÃ SỬA** (`75e02ec`; mới: `loop.py:1010-1011`; test: `TestKnowledgeMetadataLogging`).
| 24 | `lucnhan/agent/loop.py:1013` | `AgentLoop._build_retrieved_knowledge_section` | A-?(giữ) | return <default> | log_debug + return `""` ⇒ cả khối Retrieved Knowledge biến mất khỏi prompt (log ở mức debug, mặc định không thấy). Đã ghi chú là best-effort — không chắc có đáng sửa |
| 25 | `lucnhan/ida/tools/emulation.py:1023` | `_pc` | A-?(giữ) | return <default> | `return entry_pc` khi đọc PC lỗi ⇒ PC sai được báo cho LLM, không log. Không chắc có đáng sửa |
| 26 | `lucnhan/ida/tools/emulation.py:1237` | `run_emulation` | A-?(giữ) | continue | `continue` khi đọc register lỗi ⇒ register thiếu trong `final_registers`, không log. Không chắc có đáng sửa |
| 27 | `lucnhan/ida/tools/microcode_optim.py:69` | `DynamicInsnOptimizer.func` | A-?(giữ) | return <default> | log_debug + `return 0` ⇒ optimizer động âm thầm không làm gì (NOP 0 lần). Không chắc có đáng sửa |
| 28 | `lucnhan/ida/tools/microcode_optim.py:88` | `DynamicBlockOptimizer.func` | A-?(giữ) | return <default> | log_debug + `return 0` ⇒ optimizer động âm thầm không làm gì (NOP 0 lần). Không chắc có đáng sửa |
| 29 | `lucnhan/ida/tools/strings.py:75` | `_iter_ida_strings` | A-?(giữ) | nuốt, không log | `text=""` cho từng string lỗi, không log ⇒ entry rỗng lọt vào string cache. Không chắc có đáng sửa |
| 30 | `lucnhan/memory/ingest.py:104` | `make_store` | A-?(giữ) | return <default> | `make_store` trả `(None, None)`, không log ⇒ lỗi dựng store bị lẫn với 'không có IDB'. Hợp đồng đã ghi rõ là best-effort — không chắc có đáng sửa — **ĐÃ SỬA** (`b71a243`; mới: `ingest.py:106-108`; test: `test_make_store_logs_path_build_failure`).
| 31 | `lucnhan/memory/ingest.py:539` | `_write_research_to_jsonl` | A-?(giữ) | nuốt, không log | `parsed=None` khi parse note lỗi ⇒ mất genre/tags/addresses của ghi chú, không log. Không chắc có đáng sửa — **BACKLOG** (parse_note; xem `## Backlog còn lại`).
| 32 | `lucnhan/tools/skill_reference.py:68` | `_reference_root` | A-?(giữ) | pass | `pass` khi đọc config lỗi ⇒ user skill directory bị bỏ qua âm thầm (mất override), không log. Không chắc có đáng sửa |
| 33 | `lucnhan/ida/ui/actions.py:366` | `LucNhanUIHooks.database_inited` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 34 | `lucnhan/ida/ui/panel.py:108` | `_early_log_crash_safe` | B | pass | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 35 | `lucnhan/ida/ui/panel.py:121` | `_log_teardown` | B | pass | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 36 | `lucnhan/ida/ui/panel.py:158` | `_get_ida_theme_colors` | B | nuốt, không log | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 37 | `lucnhan/ida/ui/panel.py:161` | `_get_ida_theme_colors` | B | nuốt, không log | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 38 | `lucnhan/ida/ui/panel.py:313` | `LucNhanPanel.OnCreate` | B | re-raise | biên giới UI/Qt — ghi crash-log rồi re-raise (lỗi vẫn nổi lên host) |
| 39 | `lucnhan/ida/ui/panel.py:336` | `LucNhanPanel.OnCreate` | B | re-raise | biên giới UI/Qt — ghi crash-log rồi re-raise (lỗi vẫn nổi lên host) |
| 40 | `lucnhan/ida/ui/panel.py:347` | `LucNhanPanel.OnCreate` | B | re-raise | biên giới UI/Qt — ghi crash-log rồi re-raise (lỗi vẫn nổi lên host) |
| 41 | `lucnhan/ida/ui/panel.py:354` | `LucNhanPanel.OnCreate` | B | re-raise | biên giới UI/Qt — ghi crash-log rồi re-raise (lỗi vẫn nổi lên host) |
| 42 | `lucnhan/ida/ui/panel.py:446` | `LucNhanPanel._maybe_start_theme_watcher` | B | pass | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 43 | `lucnhan/ida/ui/panel.py:452` | `LucNhanPanel._maybe_start_theme_watcher` | B | pass | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 44 | `lucnhan/ida/ui/panel.py:482` | `LucNhanPanel._apply_font_override` | B | pass | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 45 | `lucnhan/ida/ui/panel.py:489` | `LucNhanPanel._apply_font_override` | B | pass | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 46 | `lucnhan/ida/ui/panel.py:512` | `LucNhanPanel._apply_font_override` | B | return None | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 47 | `lucnhan/ida/ui/panel.py:519` | `LucNhanPanel._apply_font_override` | B | pass | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 48 | `lucnhan/ida/ui/panel.py:547` | `LucNhanPanel.shutdown` | B | nuốt, không log | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 49 | `lucnhan/ida/ui/session_controller.py:156` | `IdaSessionController.next_function_chunk` | B | nuốt, không log | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 50 | `lucnhan/ui/a2a_widget.py:235` | `_A2ATaskRunner._run` | B | nuốt, không log | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 51 | `lucnhan/ui/a2a_widget.py:241` | `_A2ATaskRunner._run` | B | pass | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 52 | `lucnhan/ui/a2a_widget.py:595` | `A2ABridgeWidget._refresh_agents` | B | return None | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 53 | `lucnhan/ui/a2a_widget.py:797` | `A2ABridgeWidget._on_view_output_clicked` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 54 | `lucnhan/ui/a2a_widget.py:960` | `A2ABridgeWidget._update_row_status` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 55 | `lucnhan/ui/a2a_widget.py:1079` | `_safe_connect` | B | return <default> | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 56 | `lucnhan/ui/a2a_widget.py:1099` | `_safe_call` | B | return <default> | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 57 | `lucnhan/ui/bulk_renamer.py:660` | `BulkRenamerWidget._schedule_filter` | B | nuốt, không log | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 58 | `lucnhan/ui/context_bar.py:39` | `_function_name_at` | B | return <default> | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 59 | `lucnhan/ui/context_bar.py:197` | `ContextBar._update_cursor` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 60 | `lucnhan/ui/history_panel.py:135` | `_current_focused_widget` | B | pass | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 61 | `lucnhan/ui/input_area.py:257` | `InputArea.__init__` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 62 | `lucnhan/ui/input_area.py:273` | `InputArea.apply_theme` | B | return None | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 63 | `lucnhan/ui/input_area.py:277` | `InputArea.apply_theme` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 64 | `lucnhan/ui/input_area.py:281` | `InputArea.apply_theme` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 65 | `lucnhan/ui/input_area.py:289` | `InputArea.apply_theme` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 66 | `lucnhan/ui/input_area.py:294` | `InputArea.apply_theme` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 67 | `lucnhan/ui/knowledge_panel.py:244` | `KnowledgePanel.set_disabled_state` | B | pass | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 68 | `lucnhan/ui/knowledge_panel.py:394` | `KnowledgePanel._on_row_selected` | B | nuốt, không log | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 69 | `lucnhan/ui/message_widgets.py:1320` | `UserQuestionWidget._on_option` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 70 | `lucnhan/ui/model_picker.py:842` | `_dock_width` | B | return <default> | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 71 | `lucnhan/ui/panel_core.py:231` | `_palette_from_tokens` | B | return <default> | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 72 | `lucnhan/ui/panel_core.py:335` | `LucNhanPanelCore.__init__` | B | re-raise | biên giới UI/Qt — ghi crash-log rồi re-raise (lỗi vẫn nổi lên host) |
| 73 | `lucnhan/ui/panel_core.py:359` | `LucNhanPanelCore.__init__` | B | re-raise | biên giới UI/Qt — ghi crash-log rồi re-raise (lỗi vẫn nổi lên host) |
| 74 | `lucnhan/ui/panel_core.py:477` | `LucNhanPanelCore.__init__` | B | re-raise | biên giới UI/Qt — ghi crash-log rồi re-raise (lỗi vẫn nổi lên host) |
| 75 | `lucnhan/ui/panel_core.py:494` | `LucNhanPanelCore._warm_oauth` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 76 | `lucnhan/ui/panel_core.py:506` | `LucNhanPanelCore.__init__` | B | re-raise | biên giới UI/Qt — ghi crash-log rồi re-raise (lỗi vẫn nổi lên host) |
| 77 | `lucnhan/ui/panel_core.py:527` | `LucNhanPanelCore.__init__` | B | re-raise | biên giới UI/Qt — ghi crash-log rồi re-raise (lỗi vẫn nổi lên host) |
| 78 | `lucnhan/ui/panel_core.py:712` | `LucNhanPanelCore._apply_surface_palettes` | B | return None | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 79 | `lucnhan/ui/panel_core.py:740` | `LucNhanPanelCore._apply_transcript_surface` | B | return None | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 80 | `lucnhan/ui/panel_core.py:746` | `LucNhanPanelCore._apply_transcript_surface` | B | return None | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 81 | `lucnhan/ui/panel_core.py:946` | `LucNhanPanelCore._build_ui` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 82 | `lucnhan/ui/panel_core.py:1562` | `LucNhanPanelCore._on_model_picker_select` | B | return None | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 83 | `lucnhan/ui/panel_core.py:1885` | `LucNhanPanelCore._apply_font_override` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 84 | `lucnhan/ui/panel_core.py:1970` | `LucNhanPanelCore._on_theme_changed` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 85 | `lucnhan/ui/panel_core.py:1983` | `LucNhanPanelCore._on_theme_changed` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 86 | `lucnhan/ui/panel_core.py:1993` | `LucNhanPanelCore._on_theme_changed` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 87 | `lucnhan/ui/panel_core.py:2003` | `LucNhanPanelCore._on_theme_changed` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 88 | `lucnhan/ui/panel_core.py:2381` | `LucNhanPanelCore._on_export_tab` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 89 | `lucnhan/ui/panel_core.py:2541` | `LucNhanPanelCore._reset_token_display` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 90 | `lucnhan/ui/panel_core.py:2633` | `LucNhanPanelCore.set_theme` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 91 | `lucnhan/ui/panel_core.py:2649` | `LucNhanPanelCore.set_theme` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 92 | `lucnhan/ui/panel_core.py:2679` | `LucNhanPanelCore.shutdown` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 93 | `lucnhan/ui/panel_core.py:2687` | `LucNhanPanelCore.shutdown` | B | pass | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 94 | `lucnhan/ui/panel_core.py:2696` | `LucNhanPanelCore.shutdown` | B | pass | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 95 | `lucnhan/ui/panel_core.py:2705` | `LucNhanPanelCore.shutdown` | B | pass | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 96 | `lucnhan/ui/panel_core.py:2753` | `LucNhanPanelCore.shutdown` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 97 | `lucnhan/ui/panel_core.py:2767` | `LucNhanPanelCore.shutdown` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 98 | `lucnhan/ui/panel_core.py:3468` | `LucNhanPanelCore._history_load_worker` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 99 | `lucnhan/ui/panel_core.py:3605` | `LucNhanPanelCore._history_list_worker` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 100 | `lucnhan/ui/panel_core.py:4232` | `LucNhanPanelCore._history_delete_worker` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 101 | `lucnhan/ui/panel_core.py:4748` | `LucNhanPanelCore._ensure_tools_panel_created` | B | return None | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 102 | `lucnhan/ui/panel_core.py:4796` | `LucNhanPanelCore._ensure_tab_initialized` | B | return None | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 103 | `lucnhan/ui/panel_core.py:4853` | `LucNhanPanelCore._ensure_knowledge_tab_initialized` | B | pass | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 104 | `lucnhan/ui/panel_core.py:5020` | `LucNhanPanelCore._on_knowledge_show_changed` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 105 | `lucnhan/ui/panel_core.py:5022` | `LucNhanPanelCore._on_knowledge_show_changed` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 106 | `lucnhan/ui/panel_core.py:5048` | `LucNhanPanelCore._refresh_knowledge_panel` | B | nuốt, không log | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 107 | `lucnhan/ui/panel_core.py:5083` | `LucNhanPanelCore._refresh_knowledge_panel` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 108 | `lucnhan/ui/panel_core.py:5121` | `LucNhanPanelCore._refresh_knowledge_panel` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 109 | `lucnhan/ui/panel_core.py:5142` | `LucNhanPanelCore._on_knowledge_event_refresh` | B | pass | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 110 | `lucnhan/ui/panel_core.py:5209` | `LucNhanPanelCore._sync_composer_state` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 111 | `lucnhan/ui/plan_view.py:109` | `PlanStepWidget._apply_status_style` | B | nuốt, không log | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 112 | `lucnhan/ui/plan_view.py:252` | `PlanView._apply_styles` | B | return None | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 113 | `lucnhan/ui/session_controller_base.py:233` | `SessionControllerBase._initialize_runtime` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 114 | `lucnhan/ui/session_controller_base.py:519` | `SessionControllerBase.select_model` | B | re-raise | biên giới UI/Qt — ghi crash-log rồi re-raise (lỗi vẫn nổi lên host) |
| 115 | `lucnhan/ui/session_controller_base.py:604` | `SessionControllerBase.get_provider` | B | return <default> | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 116 | `lucnhan/ui/session_controller_base.py:659` | `SessionControllerBase.ensure_advanced_tools_ready` | B | return <default> | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 117 | `lucnhan/ui/session_controller_base.py:690` | `SessionControllerBase.reset_deferred_tools` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 118 | `lucnhan/ui/session_controller_base.py:764` | `SessionControllerBase._sync_web_tool_config` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 119 | `lucnhan/ui/session_controller_base.py:784` | `SessionControllerBase.start_agent` | B | return <default> | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 120 | `lucnhan/ui/session_controller_base.py:887` | `SessionControllerBase._wire_central_memory` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 121 | `lucnhan/ui/session_controller_base.py:915` | `SessionControllerBase._wire_central_memory` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 122 | `lucnhan/ui/session_controller_base.py:928` | `SessionControllerBase._wire_central_memory` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 123 | `lucnhan/ui/session_controller_base.py:935` | `SessionControllerBase._wire_central_memory` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 124 | `lucnhan/ui/session_controller_base.py:1016` | `SessionControllerBase.on_agent_finished` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 125 | `lucnhan/ui/session_controller_base.py:1398` | `SessionControllerBase._mcp_reload_worker` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 126 | `lucnhan/ui/session_controller_base.py:1432` | `SessionControllerBase.shutdown` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 127 | `lucnhan/ui/settings_dialog.py:228` | `_ModelFetcher.fetch` | B | return None | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 128 | `lucnhan/ui/settings_dialog.py:238` | `_ModelFetcher._run` | B | nuốt, không log | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 129 | `lucnhan/ui/settings_dialog.py:408` | `SettingsDialog.__init__` | B | pass | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 130 | `lucnhan/ui/settings_dialog.py:1048` | `SettingsDialog._resize_fetch_button` | B | return None | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 131 | `lucnhan/ui/settings_dialog.py:1747` | `SettingsDialog._maybe_prompt_zai_migration` | B | nuốt, không log | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 132 | `lucnhan/ui/settings_dialog.py:1813` | `SettingsDialog._apply_theme_state` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 133 | `lucnhan/ui/settings_dialog.py:1823` | `SettingsDialog._apply_theme_state` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 134 | `lucnhan/ui/settings_dialog.py:1850` | `SettingsDialog._apply_theme_styles` | B | return None | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 135 | `lucnhan/ui/settings_dialog.py:1859` | `SettingsDialog._apply_theme_styles` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 136 | `lucnhan/ui/settings_dialog.py:1896` | `SettingsDialog._deferred_init` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 137 | `lucnhan/ui/settings_dialog.py:1956` | `SettingsDialog._populate_builtin_models` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 138 | `lucnhan/ui/settings_dialog.py:2040` | `SettingsDialog._current_theme_state` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 139 | `lucnhan/ui/settings_dialog.py:2258` | `SettingsDialog._update_auth_status` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 140 | `lucnhan/ui/settings_dialog.py:2706` | `SettingsDialog._on_accept` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 141 | `lucnhan/ui/settings_dialog.py:2725` | `SettingsDialog._on_accept` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 142 | `lucnhan/ui/settings_service.py:62` | `SettingsService.save_mcp_servers` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 143 | `lucnhan/ui/settings_service.py:93` | `SettingsService._discover_mcp` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 144 | `lucnhan/ui/theme/applicator.py:164` | `bind_theme` | B | pass | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 145 | `lucnhan/ui/theme/applicator.py:185` | `bind_theme` | B | pass | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 146 | `lucnhan/ui/theme/manager.py:182` | `_DummySignal.emit` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 147 | `lucnhan/ui/theme/manager.py:410` | `ThemeManager._host_probe_widget` | B | return <default> | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 148 | `lucnhan/ui/theme/manager.py:502` | `ThemeManager._sync_legacy_theme_helpers` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 149 | `lucnhan/ui/theme/manager.py:528` | `ThemeManager._apply_now` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 150 | `lucnhan/ui/theme/manager.py:575` | `ThemeManager._compute_tokens` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 151 | `lucnhan/ui/theme/palette_ida.py:63` | `_read_qpalette_colors` | B | nuốt, không log | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 152 | `lucnhan/ui/theme/watcher.py:100` | `IDAThemeWatcher._tick` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 153 | `lucnhan/ui/tool_widgets.py:2117` | `_refresh_highlighter` | B | pass | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 154 | `lucnhan/ui/tool_widgets.py:2265` | `ToolApprovalWidget._apply_styles` | B | return None | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 155 | `lucnhan/ui/tools_panel.py:265` | `ToolsPanel._on_tab_changed` | B | log-only | biên giới UI/Qt — đã log, nuốt lỗi để không làm sập Qt event loop |
| 156 | `lucnhan/ui/tools_panel.py:312` | `ToolsPanel._replace_tab` | B | pass | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 157 | `lucnhan/ui/tools_panel.py:369` | `ToolsPanel.shutdown` | B | pass | biên giới UI/Qt — teardown / best-effort, nuốt lỗi |
| 158 | `lucnhan/agent/bulk_renamer.py:319` | `BulkRenamerEngine.preload_decompilation` | B | log-only | preload disassembly best-effort — render tiếp không cần disasm |
| 159 | `lucnhan/agent/bulk_renamer.py:510` | `BulkRenamerEngine._quick_decompile_jobs` | B | log-only | preload disassembly best-effort — render tiếp không cần disasm |
| 160 | `lucnhan/core/early_log.py:116` | `_resolve_paths` | B | return <default> | early-log path resolution — không được raise lúc khởi động |
| 161 | `lucnhan/core/early_log.py:152` | `_open_file` | B | return <default> | early-log open file — nuốt lỗi, fallback về buffer |
| 162 | `lucnhan/core/early_log.py:170` | `_write_banner` | B | nuốt, không log | early-log banner — best-effort, lúc này chưa có logger |
| 163 | `lucnhan/core/early_log.py:174` | `_write_banner` | B | nuốt, không log | early-log banner — best-effort, lúc này chưa có logger |
| 164 | `lucnhan/core/early_log.py:178` | `_write_banner` | B | nuốt, không log | early-log banner — best-effort, lúc này chưa có logger |
| 165 | `lucnhan/core/early_log.py:193` | `_write_banner` | B | nuốt, không log | early-log banner — best-effort, lúc này chưa có logger |
| 166 | `lucnhan/core/early_log.py:204` | `_write_banner` | B | nuốt, không log | early-log banner — best-effort, lúc này chưa có logger |
| 167 | `lucnhan/core/early_log.py:222` | `_early_log` | B | nuốt, không log | early-log format — degrade về placeholder thay vì crash |
| 168 | `lucnhan/core/early_log.py:231` | `_early_log` | B | pass | early-log write — nuốt lỗi, chưa có logger |
| 169 | `lucnhan/core/early_log.py:243` | `_early_log` | B | pass | early-log write — nuốt lỗi, chưa có logger |
| 170 | `lucnhan/core/early_log.py:245` | `_early_log` | B | pass | early-log write — nuốt lỗi, chưa có logger |
| 171 | `lucnhan/core/early_log.py:250` | `_early_log` | B | pass | early-log write — nuốt lỗi, chưa có logger |
| 172 | `lucnhan/core/early_log.py:267` | `_early_log_crash` | B | nuốt, không log | early-log crash path — fallback sang stderr |
| 173 | `lucnhan/core/early_log.py:289` | `_early_log_crash` | B | pass | early-log crash path — nuốt lỗi, chưa có logger |
| 174 | `lucnhan/core/early_log.py:294` | `_early_log_crash` | B | pass | early-log crash path — nuốt lỗi, chưa có logger |
| 175 | `lucnhan/core/early_log.py:296` | `_early_log_crash` | B | pass | early-log crash path — fallback sang stderr |
| 176 | `lucnhan/core/early_log.py:300` | `_early_log_crash` | B | pass | early-log crash path — nuốt lỗi, chưa có logger |
| 177 | `lucnhan/core/early_log.py:331` | `_reset_for_tests` | B | pass | test reset helper — nuốt lỗi cleanup |
| 178 | `lucnhan/core/log_sinks.py:99` | `set_host_log_level` | B | pass | log level set — nuốt lỗi, chưa có logger |
| 179 | `lucnhan/core/log_sinks.py:129` | `_stderr_write` | B | pass | stderr sink — last resort, nuốt lỗi |
| 180 | `lucnhan/core/log_sinks.py:133` | `_stderr_write` | B | pass | stderr sink — last resort, nuốt lỗi |
| 181 | `lucnhan/core/startup_timing.py:228` | `_StartupSession.flush_to_log` | B | return None | startup-timing flush — telemetry best-effort |
| 182 | `lucnhan/ida/headless_bootstrap.py:54` | `_log_best_effort` | B | pass | best-effort log khi stderr cũng hỏng — nuốt lỗi |
| 183 | `lucnhan/ida/headless_bootstrap.py:93` | `_clean_exit_ida` | B | nuốt, không log | shutdown flush — nuốt lỗi rồi chuyển sang os._exit |
| 184 | `lucnhan/ida/headless_bootstrap.py:211` | `_run_one_shot` | B | nuốt, không log | shutdown trước khi thoát — nuốt lỗi |
| 185 | `lucnhan/ida/headless_bootstrap.py:224` | `_do_prompt` | B | log-only | worker thread — nắm exception vào `error_holder` để báo ra ngoài |
| 186 | `lucnhan/ida/headless_bootstrap.py:240` | `_run_one_shot` | B | nuốt, không log | shutdown trước khi thoát — nuốt lỗi |
| 187 | `lucnhan/ida/headless_bootstrap.py:248` | `_run_one_shot` | B | nuốt, không log | shutdown trước khi thoát — nuốt lỗi |
| 188 | `lucnhan/ida/headless_bootstrap.py:288` | `_run_one_shot` | B | nuốt, không log | shutdown sau one-shot — nuốt lỗi |
| 189 | `lucnhan/ida/headless_bootstrap.py:340` | `_run_server` | B | nuốt, không log | shutdown trong `finally` — nuốt lỗi |
| 190 | `lucnhan/ida/headless_bootstrap.py:380` | `main` | B | nuốt, không log | config load lỗi → exit code 4 kèm thông báo cho user |
| 191 | `lucnhan/ida/headless_bootstrap.py:404` | `main` | B | log-only | bootstrap crash — log_error + traceback ra stderr |
| 192 | `lucnhan/ida/headless_bootstrap.py:410` | `main` | B | nuốt, không log | shutdown sau bootstrap failure — nuốt lỗi |
| 193 | `lucnhan/agent/a2a/client.py:89` | `A2AClient.discover` | C | return <default> | log_error + trả giá trị lỗi/default về caller |
| 194 | `lucnhan/agent/a2a/client.py:263` | `_A2ASession._run` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) |
| 195 | `lucnhan/agent/a2a/registry.py:77` | `ExternalAgentRegistry._materialize_a2a_agents` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) |
| 196 | `lucnhan/agent/a2a/registry.py:102` | `ExternalAgentRegistry._load_a2a_agents` | C | return <default> | trả giá trị default về caller, không log |
| 197 | `lucnhan/agent/a2a/subprocess_bridge.py:170` | `SubprocessBridge._drain_stdout` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) |
| 198 | `lucnhan/agent/a2a/subprocess_bridge.py:229` | `SubprocessBridge.run_task` | C | return <default> | trả giá trị default về caller, không log |
| 199 | `lucnhan/agent/bulk_renamer.py:325` | `BulkRenamerEngine.preload_decompilation` | C | break | log_error + nuốt lỗi (best-effort, đã ghi log) |
| 200 | `lucnhan/agent/bulk_renamer.py:382` | `BulkRenamerEngine.undo_all` | C | log-only | log_error + nuốt lỗi (best-effort, đã ghi log) |
| 201 | `lucnhan/agent/bulk_renamer.py:452` | `BulkRenamerEngine._run_quick` | C | log-only | log_error + nuốt lỗi (best-effort, đã ghi log) |
| 202 | `lucnhan/agent/bulk_renamer.py:516` | `BulkRenamerEngine._quick_decompile_jobs` | C | log-only | log_error + nuốt lỗi (best-effort, đã ghi log) |
| 203 | `lucnhan/agent/bulk_renamer.py:620` | `BulkRenamerEngine._quick_run_one_batch` | C | log-only | log_error + nuốt lỗi (best-effort, đã ghi log) |
| 204 | `lucnhan/agent/bulk_renamer.py:708` | `BulkRenamerEngine._apply_quick_results` | C | log-only | log_error + nuốt lỗi (best-effort, đã ghi log) |
| 205 | `lucnhan/agent/bulk_renamer.py:774` | `BulkRenamerEngine._run_deep_common` | C | log-only | log_error + nuốt lỗi (best-effort, đã ghi log) |
| 206 | `lucnhan/agent/bulk_renamer.py:844` | `BulkRenamerEngine._deep_analyze_job` | C | return None | nuốt lỗi, không log, không trả lỗi về caller |
| 207 | `lucnhan/agent/bulk_renamer.py:898` | `BulkRenamerEngine._deep_analyze_job` | C | return None | nuốt lỗi, không log, không trả lỗi về caller |
| 208 | `lucnhan/agent/bulk_renamer.py:948` | `BulkRenamerEngine._deep_analyze_job` | C | log-only | log_error + nuốt lỗi (best-effort, đã ghi log) |
| 209 | `lucnhan/agent/hypothesis_verification.py:98` | `_drain` | C | return <default> | trả giá trị default về caller, không log |
| 210 | `lucnhan/agent/hypothesis_verification.py:289` | `verify_hypotheses` | C | return <default> | log_error + trả giá trị lỗi/default về caller |
| 211 | `lucnhan/agent/loop.py:841` | `AgentLoop._handle_case_command` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) |
| 212 | `lucnhan/agent/loop.py:1949` | `AgentLoop._build_reference_injection` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) |
| 213 | `lucnhan/agent/loop.py:2024` | `AgentLoop._review_failed_script` | C | return <default> | log_error + trả giá trị lỗi/default về caller |
| 214 | `lucnhan/agent/loop.py:2197` | `AgentLoop._execute_single_tool` | C | log-only | log_error/traceback + nuốt lỗi (best-effort, đã ghi log) — **ĐÃ SỬA một phần** (`4c054a8`): bookkeeping mutation đã tách khỏi `try` (mới: `loop.py:2198-2237`, `_execute_single_tool`); nhánh docs-gate `validation=None` giữ nguyên. Test: `TestMutationBookkeepingFailure`.
| 215 | `lucnhan/agent/loop.py:2414` | `AgentLoop._handle_save_memory_tool` | C | nuốt, không log | nuốt lỗi, không log, không trả lỗi về caller — **ĐÃ SỬA** (`4c054a8`; mới: `loop.py:2424-2427`, `_handle_save_memory_tool`; test: `TestSaveMemoryErrorPath`).
| 216 | `lucnhan/agent/loop.py:2569` | `AgentLoop._handle_spawn_subagent_tool` | C | return <default> | log_error + trả giá trị lỗi/default về caller |
| 217 | `lucnhan/agent/loop.py:2629` | `AgentLoop._join_subagents` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) |
| 218 | `lucnhan/agent/loop.py:2740` | `AgentLoop._handle_delegate_external_task_tool` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) |
| 219 | `lucnhan/agent/loop.py:2743` | `AgentLoop._handle_delegate_external_task_tool` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) |
| 220 | `lucnhan/agent/loop.py:2774` | `AgentLoop._handle_delegate_external_task_tool` | C | nuốt, không log | nuốt lỗi, không log, không trả lỗi về caller |
| 221 | `lucnhan/agent/loop.py:3182` | `AgentLoop.run` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) |
| 222 | `lucnhan/agent/loop.py:3249` | `AgentLoop.run` | C | log-only | log_error/traceback + nuốt lỗi (best-effort, đã ghi log) |
| 223 | `lucnhan/agent/loop.py:3491` | `BackgroundAgentRunner._run` | C | log-only | log_error/traceback + nuốt lỗi (best-effort, đã ghi log) |
| 224 | `lucnhan/agent/loop_commands.py:212` | `_handle_memory_command` | C | nuốt, không log | nuốt lỗi, không log, không trả lỗi về caller |
| 225 | `lucnhan/agent/loop_commands.py:359` | `_handle_report_command` | C | return None | nuốt lỗi, không log, không trả lỗi về caller |
| 226 | `lucnhan/agent/loop_commands.py:401` | `_handle_report_command` | C | return None | nuốt lỗi, không log, không trả lỗi về caller |
| 227 | `lucnhan/agent/loop_commands.py:442` | `_handle_report_command` | C | return None | nuốt lỗi, không log, không trả lỗi về caller |
| 228 | `lucnhan/agent/loop_commands.py:545` | `_handle_knowledge_command` | C | return None | nuốt lỗi, không log, không trả lỗi về caller |
| 229 | `lucnhan/agent/loop_commands.py:659` | `_handle_doctor_command` | C | pass | import probe `core.dependencies` (tùy chọn) — DO-NOT-TOUCH |
| 230 | `lucnhan/agent/loop_commands.py:705` | `_handle_verify_command` | C | return None | nuốt lỗi, không log, không trả lỗi về caller |
| 231 | `lucnhan/agent/loop_commands.py:783` | `_handle_verify_command` | C | return None | nuốt lỗi, không log, không trả lỗi về caller |
| 232 | `lucnhan/agent/modes/a2a.py:120` | `run_a2a_mode` | C | nuốt, không log | nuốt lỗi, không log, không trả lỗi về caller |
| 233 | `lucnhan/agent/modes/exploration.py:242` | `_finalize_explore_memory` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) |
| 234 | `lucnhan/agent/modes/exploration.py:269` | `_finalize_explore_memory` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) |
| 235 | `lucnhan/agent/modes/exploration.py:290` | `_finalize_explore_memory` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) |
| 236 | `lucnhan/agent/modes/plan.py:134` | `persist_plan` | C | log-only | log_error + nuốt lỗi (best-effort, đã ghi log) |
| 237 | `lucnhan/agent/modes/research.py:367` | `write_and_review_note` | C | log-only | log_error + nuốt lỗi (best-effort, đã ghi log) |
| 238 | `lucnhan/agent/orchestra/main_agent.py:150` | `OrchestraMainAgent._build_system_prompt` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) |
| 239 | `lucnhan/agent/orchestra/main_agent.py:155` | `OrchestraMainAgent._build_system_prompt` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) |
| 240 | `lucnhan/agent/orchestra/main_agent.py:519` | `OrchestraMainAgent.run` | C | log-only | log_error + nuốt lỗi (best-effort, đã ghi log) |
| 241 | `lucnhan/agent/orchestra/main_agent.py:558` | `OrchestraMainAgent.run` | C | log-only | log_error + nuốt lỗi (best-effort, đã ghi log) |
| 242 | `lucnhan/agent/orchestra/orchestra_config.py:80` | `OrchestraConfig.load` | C | return <default> | trả giá trị default về caller, không log |
| 243 | `lucnhan/agent/report_evidence.py:95` | `fetch_binary_info` | C | return <default> | trả giá trị default về caller, không log |
| 244 | `lucnhan/agent/report_evidence.py:108` | `_available_tool_names` | C | return <default> | trả giá trị default về caller, không log |
| 245 | `lucnhan/agent/report_evidence.py:144` | `_evidence_for_address` | C | return <default> | trả giá trị default về caller, không log |
| 246 | `lucnhan/agent/report_evidence.py:159` | `_evidence_for_address` | C | return <default> | trả giá trị default về caller, không log |
| 247 | `lucnhan/agent/report_review.py:53` | `_drain` | C | return <default> | trả giá trị default về caller, không log |
| 248 | `lucnhan/agent/subagent_manager.py:589` | `SubagentManager._run_agent` | C | return None | log_error + trả giá trị lỗi/default về caller |
| 249 | `lucnhan/cli/headless.py:262` | `_launch_ida_ask` | C | return <default> | trả giá trị default về caller, không log |
| 250 | `lucnhan/cli/headless.py:403` | `_launch_ida_serve` | C | return <default> | trả giá trị default về caller, không log |
| 251 | `lucnhan/cli/headless.py:488` | `_http_post` | C | return <default> | trả giá trị default về caller, không log |
| 252 | `lucnhan/cli/headless.py:492` | `_http_post` | C | return <default> | trả giá trị default về caller, không log |
| 253 | `lucnhan/cli/headless.py:510` | `_http_get` | C | return <default> | trả giá trị default về caller, không log |
| 254 | `lucnhan/cli/headless.py:514` | `_http_get` | C | return <default> | trả giá trị default về caller, không log |
| 255 | `lucnhan/control/server.py:130` | `ShutdownCallback.trigger` | C | log-only | log_exception + nuốt lỗi (best-effort, đã ghi log) |
| 256 | `lucnhan/control/server.py:298` | `EventBroker._mark_finished` | C | log-only | log_warning + nuốt lỗi (best-effort, đã ghi log) |
| 257 | `lucnhan/control/server.py:629` | `ControlHandler._handle_answer` | C | return None | log_exception + trả giá trị lỗi/default về caller |
| 258 | `lucnhan/control/server.py:698` | `ControlHandler._handle_tool_approval` | C | return None | log_exception + trả giá trị lỗi/default về caller |
| 259 | `lucnhan/control/server.py:773` | `ControlHandler._handle_approval` | C | return None | log_exception + trả giá trị lỗi/default về caller |
| 260 | `lucnhan/control/server.py:914` | `ControlHandler._handle_shutdown` | C | log-only | log_exception + nuốt lỗi (best-effort, đã ghi log) |
| 261 | `lucnhan/core/config.py:379` | `LucNhanConfig.save` | C | re-raise | cleanup/rollback rồi re-raise — lỗi truyền về caller |
| 262 | `lucnhan/core/early_log.py:121` | `_resolve_paths` | C | nuốt, không log | nuốt lỗi, không log, không trả lỗi về caller |
| 263 | `lucnhan/core/host.py:48` | `<module>` | C | nuốt, không log | import probe `ida_kernwin.cvar.batch` — DO-NOT-TOUCH |
| 264 | `lucnhan/core/host.py:131` | `get_current_address` | C | return <default> | trả giá trị default về caller, không log |
| 265 | `lucnhan/core/host.py:154` | `resolve_symbol` | C | return <default> | import probe `ida_name`/`idc` — DO-NOT-TOUCH |
| 266 | `lucnhan/core/host.py:175` | `navigate_to` | C | return <default> | trả giá trị default về caller, không log |
| 267 | `lucnhan/core/host.py:186` | `get_user_config_base_dir` | C | return <default> | trả giá trị default về caller, không log |
| 268 | `lucnhan/core/host.py:202` | `get_database_path` | C | return <default> | trả giá trị default về caller, không log |
| 269 | `lucnhan/core/host.py:223` | `get_database_instance_id` | C | return <default> | trả giá trị default về caller, không log |
| 270 | `lucnhan/core/host.py:242` | `set_database_instance_id` | C | return <default> | trả giá trị default về caller, không log |
| 271 | `lucnhan/core/log_sinks.py:77` | `_read_configured_host_level` | C | return <default> | import probe `.config` — DO-NOT-TOUCH |
| 272 | `lucnhan/core/log_sinks.py:81` | `_read_configured_host_level` | C | return <default> | trả giá trị default về caller, không log |
| 273 | `lucnhan/core/log_sinks.py:145` | `_resolve_host_sink` | C | return <default> | import probe `.host` — DO-NOT-TOUCH |
| 274 | `lucnhan/core/startup_timing.py:67` | `_get_log_debug` | C | log-only | import probe `lucnhan.core.logging` — DO-NOT-TOUCH |
| 275 | `lucnhan/core/thread_safety.py:97` | `_thunk` | C | nuốt, không log | nuốt lỗi, không log, không trả lỗi về caller |
| 276 | `lucnhan/headless/runner.py:214` | `_auto_deny_approval` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) |
| 277 | `lucnhan/headless/runner.py:223` | `_auto_deny_approval` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) |
| 278 | `lucnhan/headless/runner.py:228` | `_auto_deny_approval` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) |
| 279 | `lucnhan/ida/dispatch.py:129` | `IdaUiDispatcher._call` | C | nuốt, không log | nuốt lỗi, không log, không trả lỗi về caller |
| 280 | `lucnhan/ida/headless_bootstrap.py:85` | `_clean_exit_ida` | C | nuốt, không log | import probe `idc` — DO-NOT-TOUCH |
| 281 | `lucnhan/ida/headless_controller.py:90` | `HeadlessSessionController.wait_auto_analysis` | C | return <default> | import probe `ida_auto` — DO-NOT-TOUCH |
| 282 | `lucnhan/ida/tools/annotations.py:76` | `rename_variable` | C | return <default> | trả giá trị default về caller, không log — **ĐÃ SỬA** (`93ea290`; mới: `annotations.py:76`, `rename_variable`; test: `TestRenameVariableDecompileFailure`).
| 283 | `lucnhan/ida/tools/decompiler.py:115` | `_resolve_ctree_ea` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) |
| 284 | `lucnhan/ida/tools/emulation.py:942` | `run_emulation` | C | raise ToolError | raise ToolError — lỗi truyền về caller dưới dạng ToolError |
| 285 | `lucnhan/ida/tools/emulation.py:967` | `run_emulation` | C | raise ToolError | raise ToolError — lỗi truyền về caller dưới dạng ToolError |
| 286 | `lucnhan/ida/tools/emulation.py:992` | `run_emulation` | C | raise ToolError | raise ToolError — lỗi truyền về caller dưới dạng ToolError |
| 287 | `lucnhan/ida/tools/emulation.py:1171` | `run_emulation` | C | raise ToolError | raise ToolError — lỗi truyền về caller dưới dạng ToolError |
| 288 | `lucnhan/ida/tools/emulation.py:1188` | `run_emulation` | C | nuốt, không log | nuốt lỗi, không log, không trả lỗi về caller |
| 289 | `lucnhan/ida/tools/emulation.py:1202` | `run_emulation` | C | nuốt, không log | nuốt lỗi, không log, không trả lỗi về caller |
| 290 | `lucnhan/ida/tools/emulation.py:1246` | `run_emulation` | C | nuốt, không log | nuốt lỗi, không log, không trả lỗi về caller |
| 291 | `lucnhan/ida/tools/emulation.py:1358` | `_discover_strings` | C | break | nuốt lỗi, không log, không trả lỗi về caller |
| 292 | `lucnhan/ida/tools/emulation_memory.py:369` | `_is_bss` | C | pass | nuốt lỗi, không log, không trả lỗi về caller |
| 293 | `lucnhan/ida/tools/emulation_memory.py:381` | `_segments` | C | raise ToolError | raise ToolError — lỗi truyền về caller dưới dạng ToolError |
| 294 | `lucnhan/ida/tools/emulation_memory.py:426` | `_read_page_bytes` | C | raise ToolError | raise ToolError — lỗi truyền về caller dưới dạng ToolError |
| 295 | `lucnhan/ida/tools/file_io.py:248` | `write_file` | C | pass + return <default> | trả giá trị default về caller, không log — **ĐÃ SỬA** (`7cb7ef0`; mới: `file_io.py:249-255`, `write_file`; test: `test_unexpected_write_error_propagates_as_tool_error`).
| 296 | `lucnhan/ida/tools/functions.py:85` | `_enumerate_all_functions` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) |
| 297 | `lucnhan/ida/tools/functions.py:155` | `get_function_info` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) |
| 298 | `lucnhan/ida/tools/microcode.py:296` | `nop_microcode` | C | return <default> | trả giá trị default về caller, không log |
| 299 | `lucnhan/ida/tools/microcode.py:381` | `install_microcode_optimizer` | C | return <default> | trả giá trị default về caller, không log |
| 300 | `lucnhan/ida/tools/registry.py:168` | `register_advanced_tools` | C | log-only | import probe `importlib.import_module` (module tool) — DO-NOT-TOUCH |
| 301 | `lucnhan/ida/tools/strings.py:125` | `_ensure_cache` | C | log-only | log_warning + nuốt lỗi (best-effort, đã ghi log) |
| 302 | `lucnhan/ida/tools/strings.py:140` | `_ensure_cache` | C | return <default> | log_warning + trả giá trị lỗi/default về caller |
| 303 | `lucnhan/ida/tools/strings.py:158` | `_ensure_cache` | C | return <default> | log_warning + trả giá trị lỗi/default về caller |
| 304 | `lucnhan/ida/tools/strings.py:275` | `get_string_at` | C | return <default> | trả giá trị default về caller, không log |
| 305 | `lucnhan/ida/tools/strings.py:283` | `get_string_at` | C | log-only | log_warning + nuốt lỗi (best-effort, đã ghi log) |
| 306 | `lucnhan/ida/tools/types_tools.py:199` | `_create_struct_ida9` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) — **ĐÃ SỬA** (`7cb7ef0`; mới: `types_tools.py:200-202`, `_create_struct_ida9`; test: `TestCreateStructFieldCommentFailure`).
| 307 | `lucnhan/ida/tools/types_tools.py:293` | `_modify_struct_ida9` | C | return <default> | trả giá trị default về caller, không log |
| 308 | `lucnhan/ida/tools/types_tools.py:724` | `apply_type_to_variable` | C | return <default> | trả giá trị default về caller, không log — **ĐÃ SỬA** (`93ea290`; mới: `types_tools.py:727`, `apply_type_to_variable`; test: `TestApplyTypeToVariableDecompileFailure`).
| 309 | `lucnhan/ida/tools/types_tools.py:748` | `apply_type_to_variable` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) |
| 310 | `lucnhan/ida/tools/types_tools.py:773` | `apply_type_to_variable` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) |
| 311 | `lucnhan/ida/tools/types_tools.py:828` | `suggest_struct_from_accesses` | C | return <default> | trả giá trị default về caller, không log |
| 312 | `lucnhan/ida/tools/types_tools.py:906` | `get_type_libraries` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) — **ĐÃ SỬA** (`7cb7ef0`; mới: `types_tools.py:909`, `get_type_libraries`; test: `test_main_til_fallback_propagates_unexpected_error`).
| 313 | `lucnhan/ida/ui/panel.py:267` | `_installed_font_family` | C | return <default> | import probe `qt_compat.QFontDatabase` — DO-NOT-TOUCH |
| 314 | `lucnhan/ida/ui/tools_form.py:85` | `_widget_alive` | C | return <default> | import probe `shiboken6.isValid` — DO-NOT-TOUCH |
| 315 | `lucnhan/mcp/client.py:198` | `MCPClient.call_tool` | C | raise | raise — lỗi truyền về caller |
| 316 | `lucnhan/mcp/manager.py:105` | `MCPManager.start_servers` | C | log-only | log_warning + nuốt lỗi (best-effort, đã ghi log) |
| 317 | `lucnhan/mcp/manager.py:186` | `MCPManager._start_one` | C | log-only | log_debug/error + nuốt lỗi (best-effort, đã ghi log) |
| 318 | `lucnhan/mcp/manager.py:191` | `MCPManager._start_one` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) |
| 319 | `lucnhan/mcp/manager.py:203` | `MCPManager.stop_all` | C | log-only | log_error + nuốt lỗi (best-effort, đã ghi log) |
| 320 | `lucnhan/memory/case_commands.py:118` | `dispatch_case_command` | C | return <default> | trả giá trị default về caller, không log |
| 321 | `lucnhan/memory/ingest.py:274` | `_write_exploration_to_sqlite` | C | log-only | log_error + nuốt lỗi (best-effort, đã ghi log) |
| 322 | `lucnhan/memory/ingest.py:457` | `ingest_exploration_finding` | C | log-only | log_error + nuốt lỗi (best-effort, đã ghi log) |
| 323 | `lucnhan/memory/ingest.py:521` | `_write_research_to_sqlite` | C | log-only | log_error + nuốt lỗi (best-effort, đã ghi log) |
| 324 | `lucnhan/memory/ingest.py:711` | `ingest_research_note` | C | log-only | log_error + nuốt lỗi (best-effort, đã ghi log) |
| 325 | `lucnhan/memory/ingest.py:810` | `ingest_report` | C | re-raise + return None | cleanup/rollback rồi re-raise — lỗi truyền về caller |
| 326 | `lucnhan/memory/jsonl_migration.py:177` | `write_envelopes_to_temp_bundle` | C | re-raise | cleanup/rollback rồi re-raise — lỗi truyền về caller |
| 327 | `lucnhan/memory/markdown.py:230` | `MemoryProjector._probe_portalocker` | C | return None | import probe `portalocker` — DO-NOT-TOUCH |
| 328 | `lucnhan/memory/raw_store.py:110` | `KnowledgeRawStore._write_jsonl_atomic` | C | re-raise + pass | cleanup/rollback rồi re-raise — lỗi truyền về caller |
| 329 | `lucnhan/memory/raw_store.py:321` | `KnowledgeRawStore.commit_hypothesis_verdicts` | C | re-raise | cleanup/rollback rồi re-raise — lỗi truyền về caller |
| 330 | `lucnhan/memory/raw_store.py:331` | `KnowledgeRawStore.commit_hypothesis_verdicts` | C | log-only | log_error + nuốt lỗi (best-effort, đã ghi log) |
| 331 | `lucnhan/memory/raw_store.py:336` | `KnowledgeRawStore.commit_hypothesis_verdicts` | C | log-only | log_error + nuốt lỗi (best-effort, đã ghi log) |
| 332 | `lucnhan/memory/registry.py:256` | `MemoryRegistry.bind_evidence` | C | re-raise | cleanup/rollback rồi re-raise — lỗi truyền về caller — **ĐÃ SỬA** (`b71a243`; mới: `registry.py:257-263`; test: `test_non_integrity_error_with_unique_text_is_not_misclassified`).
| 333 | `lucnhan/memory/report.py:585` | `write_report_file` | C | re-raise + pass | cleanup/rollback rồi re-raise — lỗi truyền về caller |
| 334 | `lucnhan/memory/report.py:887` | `save_report` | C | nuốt, không log | nuốt lỗi, không log, không trả lỗi về caller |
| 335 | `lucnhan/memory/service.py:205` | `BinaryMemoryService.save_fact` | C | return <default> | log_error + trả giá trị lỗi/default về caller |
| 336 | `lucnhan/memory/service.py:316` | `BinaryMemoryService.save_exploration_finding` | C | return <default> | log_error + trả giá trị lỗi/default về caller |
| 337 | `lucnhan/memory/workspace_store.py:586` | `WorkspaceStore.put_fact` | C | re-raise | cleanup/rollback rồi re-raise — lỗi truyền về caller |
| 338 | `lucnhan/memory/workspace_store.py:857` | `WorkspaceStore.put_entity` | C | re-raise | cleanup/rollback rồi re-raise — lỗi truyền về caller |
| 339 | `lucnhan/memory/workspace_store.py:971` | `WorkspaceStore.put_relation` | C | re-raise | cleanup/rollback rồi re-raise — lỗi truyền về caller |
| 340 | `lucnhan/memory/workspace_store.py:1027` | `WorkspaceStore.append_observation` | C | re-raise | cleanup/rollback rồi re-raise — lỗi truyền về caller |
| 341 | `lucnhan/memory/workspace_store.py:1067` | `WorkspaceStore.mark_projection_dirty` | C | re-raise | cleanup/rollback rồi re-raise — lỗi truyền về caller |
| 342 | `lucnhan/memory/workspace_store.py:1089` | `WorkspaceStore.mark_projection_clean` | C | re-raise | cleanup/rollback rồi re-raise — lỗi truyền về caller |
| 343 | `lucnhan/memory/workspace_store.py:1100` | `WorkspaceStore.mark_projection_conflict` | C | re-raise | cleanup/rollback rồi re-raise — lỗi truyền về caller |
| 344 | `lucnhan/memory/workspace_store.py:1127` | `WorkspaceStore.set_meta` | C | re-raise | cleanup/rollback rồi re-raise — lỗi truyền về caller |
| 345 | `lucnhan/providers/anthropic_provider.py:61` | `_read_oauth_from_keychain` | C | return <default> | trả giá trị default về caller, không log |
| 346 | `lucnhan/providers/anthropic_provider.py:815` | `AnthropicProvider._stream_chunks` | C | return None | log_debug/error + trả giá trị lỗi/default về caller |
| 347 | `lucnhan/providers/base.py:285` | `LLMProvider.chat` | C | raise | raise — lỗi truyền về caller |
| 348 | `lucnhan/providers/base.py:394` | `LLMProvider._watchdog` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) |
| 349 | `lucnhan/providers/base.py:412` | `LLMProvider.list_models` | C | return <default> | log_debug + trả giá trị lỗi/default về caller |
| 350 | `lucnhan/providers/base.py:466` | `LLMProvider.validate_key` | C | return <default> | log_debug + trả giá trị lỗi/default về caller |
| 351 | `lucnhan/providers/codex_provider.py:600` | `CodexProvider._stream_chunks` | C | return None | nuốt lỗi, không log, không trả lỗi về caller |
| 352 | `lucnhan/providers/codex_provider.py:618` | `CodexProvider._stream_chunks` | C | return None | log_debug + trả giá trị lỗi/default về caller |
| 353 | `lucnhan/providers/gemini_provider.py:441` | `GeminiProvider._stream_chunks` | C | return None | log_debug + trả giá trị lỗi/default về caller |
| 354 | `lucnhan/providers/minimax_provider.py:105` | `_unescape_value` | C | return <default> | trả giá trị default về caller, không log |
| 355 | `lucnhan/providers/minimax_provider.py:509` | `MiniMaxProvider._fetch_models_live` | C | return <default> | trả giá trị default về caller, không log |
| 356 | `lucnhan/providers/ollama_provider.py:73` | `OllamaProvider.list_models` | C | return <default> | log_debug + trả giá trị lỗi/default về caller |
| 357 | `lucnhan/providers/openai_compat.py:133` | `OpenAICompatProvider.list_models` | C | log-only | log_debug + nuốt lỗi (best-effort, đã ghi log) |
| 358 | `lucnhan/providers/openai_provider.py:504` | `OpenAIProvider._call_api` | C | re-raise + return <default> | cleanup/rollback rồi re-raise — lỗi truyền về caller |
| 359 | `lucnhan/providers/openai_provider.py:566` | `OpenAIProvider._stream_chunks` | C | return None | nuốt lỗi, không log, không trả lỗi về caller |
| 360 | `lucnhan/providers/openai_provider.py:573` | `OpenAIProvider._stream_chunks` | C | return None | nuốt lỗi, không log, không trả lỗi về caller |
| 361 | `lucnhan/providers/openai_provider.py:591` | `OpenAIProvider._stream_chunks` | C | re-raise + return None | cleanup/rollback rồi re-raise — lỗi truyền về caller |
| 362 | `lucnhan/providers/openai_provider.py:811` | `OpenAIProvider._iter_stream_chunks` | C | return None | nuốt lỗi, không log, không trả lỗi về caller |
| 363 | `lucnhan/state/history.py:379` | `SessionHistory._write_manifest` | C | re-raise | cleanup/rollback rồi re-raise — lỗi truyền về caller |
| 364 | `lucnhan/state/history.py:441` | `SessionHistory._update_manifest_entry` | C | log-only | log_warning + nuốt lỗi (best-effort, đã ghi log) |
| 365 | `lucnhan/state/history.py:634` | `SessionHistory.save_session` | C | re-raise | cleanup/rollback rồi re-raise — lỗi truyền về caller |
| 366 | `lucnhan/state/history.py:647` | `SessionHistory.save_session` | C | log-only | log_warning + nuốt lỗi (best-effort, đã ghi log) |
| 367 | `lucnhan/tools/base.py:310` | `wrapper` | C | raise ToolError | raise ToolError — lỗi truyền về caller dưới dạng ToolError |
| 368 | `lucnhan/tools/registry.py:411` | `ToolRegistry.execute` | C | raise ToolError | raise ToolError — lỗi truyền về caller dưới dạng ToolError |
| 369 | `lucnhan/tools/registry.py:499` | `ToolRegistry.execute_coerced` | C | raise ToolError | raise ToolError — lỗi truyền về caller dưới dạng ToolError |
| 370 | `lucnhan/tools/registry.py:571` | `ToolRegistry.execute_current_thread` | C | raise ToolError | raise ToolError — lỗi truyền về caller dưới dạng ToolError |
| 371 | `lucnhan/tools/script_guard.py:570` | `run_guarded_script` | C | nuốt, không log | nuốt lỗi, không log, không trả lỗi về caller — **ĐÃ SỬA** (`79f2123`; `except BaseException`; test: `test_base_exception_is_captured_not_propagated`).
| 372 | `lucnhan/tools/string_cache.py:177` | `_atomic_write_text` | C | re-raise + pass | cleanup/rollback rồi re-raise — lỗi truyền về caller |
| 373 | `lucnhan/tools/string_cache.py:196` | `_atomic_write_jsonl` | C | re-raise + pass | cleanup/rollback rồi re-raise — lỗi truyền về caller |
| 374 | `lucnhan/tools/string_cache.py:660` | `_Stager.stage_jsonl` | C | re-raise + pass | cleanup/rollback rồi re-raise — lỗi truyền về caller |
| 375 | `lucnhan/tools/string_cache.py:678` | `_Stager.stage` | C | re-raise + pass | cleanup/rollback rồi re-raise — lỗi truyền về caller |
| 376 | `lucnhan/tools/string_cache.py:873` | `_build_sync` | C | re-raise | cleanup/rollback rồi re-raise — lỗi truyền về caller |
| 377 | `lucnhan/tools/string_cache.py:1055` | `_work` | C | pass | log_warning + nuốt lỗi (best-effort, đã ghi log) |
| 378 | `lucnhan/tools/string_cache.py:1059` | `_work` | C | pass | nuốt lỗi, không log, không trả lỗi về caller |
| 379 | `lucnhan/tools/web.py:76` | `_get_minimax_auth` | C | return <default> | log_debug + trả giá trị lỗi/default về caller |
| 380 | `lucnhan/tools/web_fetch.py:214` | `_strip_html_tags` | C | return <default> | trả giá trị default về caller, không log |
| 381 | `lucnhan/tools/web_fetch.py:347` | `web_fetch` | C | raise ToolError | raise ToolError — lỗi truyền về caller dưới dạng ToolError |
| 382 | `lucnhan/ui/a2a_widget.py:790` | `A2ABridgeWidget._on_view_output_clicked` | C | log-only | import probe `.message_widgets` — DO-NOT-TOUCH |
| 383 | `lucnhan/ui/agent_tree.py:349` | `AgentTreeWidget._refresh_row_status_colors` | C | return None | import probe `.qt_compat.QColor` — DO-NOT-TOUCH |
| 384 | `lucnhan/ui/bulk_renamer.py:876` | `BulkRenamerWidget._refresh_row_status_colors` | C | return None | import probe `.qt_compat.QColor` — DO-NOT-TOUCH |
| 385 | `lucnhan/ui/context_bar.py:120` | `ContextBar.apply_theme` | C | log-only | import probe `.styles` / `.theme.widgets_common` — DO-NOT-TOUCH |
| 386 | `lucnhan/ui/highlight.py:89` | `_get_formatter` | C | nuốt, không log | import probe `pygments` — DO-NOT-TOUCH |
| 387 | `lucnhan/ui/highlight.py:198` | `<module>` | C | pass | import probe `.theme.manager` — DO-NOT-TOUCH |
| 388 | `lucnhan/ui/markdown.py:142` | `prewarm_markdown` | C | log-only | import probe `.markdown_renderer` — DO-NOT-TOUCH |
| 389 | `lucnhan/ui/markdown.py:553` | `<module>` | C | pass | import probe `.theme.manager` — DO-NOT-TOUCH |
| 390 | `lucnhan/ui/markdown_renderer.py:93` | `_highlight` | C | return <default> | import probe `.highlight` — DO-NOT-TOUCH |
| 391 | `lucnhan/ui/panel_core.py:553` | `LucNhanPanelCore._apply_initial_theme_from_config` | C | log-only | import probe `.theme.tokens` — DO-NOT-TOUCH |
| 392 | `lucnhan/ui/panel_core.py:569` | `LucNhanPanelCore._resolve_dependency_warnings` | C | log-only | import probe `..providers.registry` — DO-NOT-TOUCH |
| 393 | `lucnhan/ui/panel_core.py:2915` | `LucNhanPanelCore._on_settings` | C | log-only | import probe `.settings_dialog` — DO-NOT-TOUCH |
| 394 | `lucnhan/ui/session_controller_base.py:726` | `SessionControllerBase._create_provider` | C | log-only | import probe `..providers.auth_cache` — DO-NOT-TOUCH |
| 395 | `lucnhan/ui/session_controller_base.py:759` | `SessionControllerBase._sync_web_tool_config` | C | log-only | import probe `..tools.web` — DO-NOT-TOUCH |
| 396 | `lucnhan/ui/settings_dialog.py:2046` | `SettingsDialog._current_theme_state` | C | log-only | import probe `.styles` — DO-NOT-TOUCH |
| 397 | `lucnhan/ui/settings_dialog.py:2071` | `SettingsDialog._restore_theme_from_snapshot` | C | log-only | import probe `.styles` — DO-NOT-TOUCH |
| 398 | `lucnhan/ui/settings_service.py:77` | `SettingsService._discover_skills` | C | log-only | import probe `..skills.registry` — DO-NOT-TOUCH |
| 399 | `lucnhan/ui/settings_service.py:84` | `SettingsService._discover_skills` | C | log-only | import probe `core.external_sources` — DO-NOT-TOUCH |
| 400 | `lucnhan/ui/settings_service.py:100` | `SettingsService._discover_mcp` | C | log-only | import probe `core.external_sources` — DO-NOT-TOUCH |
| 401 | `lucnhan/ui/theme/applicator.py:175` | `bind_theme` | C | pass | import probe `.manager` — DO-NOT-TOUCH |
| 402 | `lucnhan/ui/theme/manager.py:374` | `ThemeManager._log_auto_derive_once` | C | log-only | import probe `..core.logging` — DO-NOT-TOUCH |
| 403 | `lucnhan/ui/theme/manager.py:474` | `ThemeManager._sync_legacy_theme_helpers` | C | return None | import probe `..styles` — DO-NOT-TOUCH |
| 404 | `lucnhan/ui/theme/manager.py:494` | `ThemeManager._sync_legacy_theme_helpers` | C | return <default> | import probe `..core.host` — DO-NOT-TOUCH |
| 405 | `lucnhan/ui/theme/manager.py:542` | `ThemeManager._compute_tokens` | C | return <default> | import probe `..core.host` — DO-NOT-TOUCH |
| 406 | `lucnhan/ui/theme/manager.py:549` | `ThemeManager._compute_tokens` | C | pass | import probe `..core.logging` — DO-NOT-TOUCH |
| 407 | `lucnhan/ui/theme/manager.py:568` | `ThemeManager._compute_tokens` | C | log-only | import probe `.palette_ida` — DO-NOT-TOUCH |
| 408 | `lucnhan/ui/theme/manager.py:584` | `ThemeManager._compute_tokens` | C | log-only | import probe `.palette_ida` — DO-NOT-TOUCH |
| 409 | `lucnhan/ui/theme/widgets_common.py:47` | `_font_size_override_points` | C | return <default> | import probe `.manager` — DO-NOT-TOUCH |

## Đã sửa trong lượt này

18 chỗ (11 Turn A + 7 Turn B). Test chạy bằng `.venv/Scripts/python.exe -m pytest …`; tất cả
deterministic, không cần IDA Pro (mock `tests/mocks/ida_mock.py`).

| # | Vị trí trước | Vị trí sau | Exception/hành vi mới | Test | Commit |
|---|---|---|---|---|---|
| 1 | `agent/loop.py:2156-2197` | `agent/loop.py:2156-2161` + `2198-2237` | Tách mutation bookkeeping khỏi `try` dispatch; bookkeeping lỗi → `log_error` + traceback, vẫn `is_error=False`, không append record giả | `test_agent_loop.py::TestMutationBookkeepingFailure` | `4c054a8` |
| 2 | `ida/tools/types_tools.py:977-981` | `:978-983` | `except (ida_hexrays.DecompilationFailure, RuntimeError)` + `log_debug`; vẫn trả `""` | `test_types_tools.py::TestGetVariableTypeDecompileFailure` | `93ea290` |
| 3 | `ida/tools/types_tools.py:721-725` | `:722-727` | cùng cặp exception; giữ `"Decompilation failed: {e}"` | `test_types_tools.py::TestApplyTypeToVariableDecompileFailure` | `93ea290` |
| 4 | `ida/tools/annotations.py:73-77` | `:74-77` | cùng cặp exception; giữ chuỗi lỗi cũ | `test_annotations.py::TestRenameVariableDecompileFailure` | `93ea290` |
| 5 | `tools/script_guard.py:567-571` | `:570` | `except BaseException` ở sink sandbox (SystemExit không thoát ra ngoài) | `test_script_guard.py::TestRunGuardedScript::test_base_exception_is_captured_not_propagated` | `79f2123` |
| 6 | `agent/loop.py:2414-2416` | `:2424-2427` | `(ValueError, RuntimeError, OSError, sqlite3.Error)`; message `"Error saving to central memory (<Type>)."`, `log_error` + traceback | `test_memory_cutover.py::TestSaveMemoryErrorPath` | `4c054a8` |
| 7 | `memory/ingest.py:104` | `:106-108` | `(OSError, ValueError)` (UnicodeError ⊂ ValueError) + `log_error` + traceback; vẫn `(None, None)` | `test_dual_write_ingest.py::test_make_store_logs_path_build_failure` | `b71a243` |
| 8 | `memory/ingest.py:168-170` | `:171-173` | giữ broad, `pass` → `log_error` (repr) | `test_dual_write_ingest.py::test_ingest_save_memory_logs_jsonl_failure` | `b71a243` |
| 9 | `memory/ingest.py:338-339, 382-383` (exploration) + `:557, 584, 613, 640, 662` (research) | `:341-342, 385-386` + `:561, 588, 617, 644, 666` | giữ broad, 7× `pass` → `log_error` nêu rõ write nào fail | 4 test `test_research_note_*`, `test_exploration_entity_write_failure_is_logged` | `b71a243` |
| 10 | `memory/retrieve.py:348` | `:349-351` | `(OSError, ValueError)` + `log_debug`; vẫn `parsed_notes=[]` | `test_retrieve_context.py::TestRetrieveNotesFailure` | `b71a243` |
| 11 | `memory/registry.py:256-260` | `:257-263` | broad catch + rollback giữ nguyên; chỉ dịch sang `EvidenceConflictError` khi `isinstance(exc, sqlite3.IntegrityError)` và message khớp | `test_registry.py::TestEvidenceUniqueness::test_non_integrity_error_with_unique_text_is_not_misclassified` | `b71a243` |
| 12 | `agent/loop.py:1006-1010` | `:1007-1011` | `(AttributeError, KeyError, TypeError, ValueError)` + `log_debug` | `test_agent_loop.py::TestKnowledgeMetadataLogging` | `75e02ec` |
| 13 | `ida/tools/file_io.py:248-253` | `:249-255` | `(OSError, ValueError)` + `log_error`; lỗi khác → `ToolError` | `test_file_io.py::TestWriteFile::test_unexpected_write_error_propagates_as_tool_error` | `7cb7ef0` |
| 14 | `ida/tools/types_tools.py:906` | `:909` | `(AttributeError, TypeError)`; giữ log + fallback string | `test_types_tools.py::TestGetTypeLibraries::test_main_til_fallback_propagates_unexpected_error` | `7cb7ef0` |
| 15 | `ida/tools/types_tools.py:199-200` | `:200-202` + return `:210-211` | `(AttributeError, RuntimeError)` + `log_warning`; thêm `(comments not set: …)` (giữ prefix `Created struct`) | `test_types_tools.py::TestCreateStructFieldCommentFailure` | `7cb7ef0` |
| 16 | `memory/report.py:440-441` | `:441-442` | giữ broad, `pass` → `log_debug` | `test_report_command_runner.py::TestBuildReportContextNotesFailure` | `7e09bdd` |
| 17 | `agent/loop.py:2308` + `:2481` | `:2318-2319` + `:2492-2493` | `log_debug` → `log_error` + `repr`; giữ broad | `test_agent_loop.py::TestKnowledgeIngestErrorLogging` (2 test) | `75e02ec` |
| 18 | `memory/context.py:242-243` | `:243-245` | `(OSError, ValueError, TypeError, KeyError)` + `log_warning`; vẫn `("", None)` | `test_retrieve_context.py::TestBuildRetrievedContextFailure` | `7e09bdd` |

## Backlog còn lại

Các mục **không sửa** trong đợt này (giữ broad hoặc chưa đủ evidence); line number là hiện tại.

| Mục | Trạng thái | Lý do 1 dòng |
|---|---|---|
| `agent/loop.py:861` `_build_system_prompt` | A-?(giữ) | `log_debug` khi `get_binary_info` lỗi — chỉ mất context, không ảnh hưởng undo/tool result |
| `agent/loop.py:866` `_build_system_prompt` | A-?(giữ) | cursor/function context lỗi — best-effort, chỉ ảnh hưởng prompt |
| `agent/loop.py:882` `_build_system_prompt` | A-?(giữ) | central-memory context lỗi — best-effort, đã log |
| `agent/loop.py:1009` | **đã sửa** | chuyển sang `## Đã sửa trong lượt này` #12 |
| `agent/loop.py:1013` `_build_retrieved_knowledge_section` | A-?(giữ) | outer catch best-effort, đã `log_debug` + trả `""`; narrow sẽ đổi lỗi thành crash turn |
| `ida/tools/emulation.py:1023` `_pc` | A-?(giữ) | fallback `entry_pc`; chưa verify hành vi register/PC trên IDA thật |
| `ida/tools/emulation.py:1237` `run_emulation` | A-?(giữ) | `continue` khi đọc register lỗi; chưa verify trên IDA thật |
| `ida/tools/microcode_optim.py:69` `DynamicInsnOptimizer.func` | A-?(giữ) | optimizer động no-op; chưa verify trên IDA thật |
| `ida/tools/microcode_optim.py:88` `DynamicBlockOptimizer.func` | A-?(giữ) | optimizer động no-op; chưa verify trên IDA thật |
| `ida/tools/strings.py:75` `_iter_ida_strings` | A-?(giữ) | entry rỗng lọt vào cache; chưa verify định dạng string của IDA 9.x |
| `memory/ingest.py:104` | **đã sửa** | chuyển sang `## Đã sửa trong lượt này` #7 |
| `memory/ingest.py:542` `_write_research_to_jsonl` (`parse_note`) | A-?(giữ) | `parsed=None` khi note hỏng — mất genre/tags/addresses nhưng vẫn ghi memory; cần verify format note trước khi narrow |
| `tools/skill_reference.py:68` `_reference_root` | A-?(giữ) | `pass` khi đọc config lỗi — user skill dir bị bỏ qua âm thầm; chưa đủ evidence |
| `ida/tools/types_tools.py:345` `_get_struct_info_ida9` | **P3-3 — KHÔNG sửa** | `except (AttributeError, Exception)`; chưa verify trên IDA 9.x nên giữ nguyên, chỉ ghi nhận |

## Ghi chú phương pháp

- Đếm bằng `rg` (410 dòng) rồi **xác nhận lại bằng AST** (`ast.ExceptHandler` với `type` là `Exception`
  hoặc `<module>.Exception`) → 409 handler thật. Chênh lệch 1 dòng là docstring `ui/panel_core.py:3457`.
- Trường *Nuốt lỗi?* suy ra từ AST: `pass` / `continue` / `break` / `return None` / `return <default>` /
  `raise` / `re-raise` / `raise ToolError`; `log-only` = có log nhưng không re-raise.
- Trường *Lý do* ưu tiên: mức ưu tiên A (mutation/undo & tool dispatch → `tools/` & `ida/tools/` →
  `memory/`) → B → C.
- `lucnhan/core/sanitize.py` **không có** handler `except Exception` nào (chỉ 2 `except re.error`) —
  đã kiểm tra, không phải ứng viên.
- `lucnhan/tools/script_guard.py` **có** 1 handler (`run_guarded_script`, dòng 570): exception của script
  do LLM viết được ghi vào `stderr_buf` và trả về cho người dùng dưới dạng output → **C** (đã surface
  lỗi cho caller, không phải A).
- `lucnhan/agent/loop.py:2197` (`_execute_single_tool`) dùng `except Exception` **sau** `except ToolError`
  → log_error + `traceback.format_exc()` + trả `is_error=True` cho LLM → **C**.

---

## Appendix — Giữ nguyên, lý do (KHÔNG thu hẹp)

> Chuyển nguyên văn từ `docs/except-audit-proposal.md` (file scratch đã xoá). Số dòng trong mục
> này là tại thời điểm viết proposal (trước Turn A/B); dùng `file:hàm` để định vị lại.

Đây là các chỗ quan trọng hơn nhiều candidate ở trên, nhưng thu hẹp sẽ **gây hại** hoặc phá contract
đã được test pin. Ghi lại để tránh một đợt "narrow cho đẹp" sau này.

1. **`lucnhan/agent/mutation.py:1138-1146` — `capture_pre_state` broad catch.**
   Docstring + 2 test pin hành vi best-effort: `tests/agent/test_mutation.py:695-699`
   (`test_capture_pre_state_swallows_executor_failures`) và `:596-608`
   (`test_write_file_capture_read_failure`) chủ động inject `RuntimeError` để yêu cầu **nuốt**.
   Ngoài ra `capture_pre_state(...)` được gọi **trước** `try` tool (loop.py:2149); nếu thu hẹp,
   một getter bug sẽ abort cả turn thay vì chỉ mất undo record. Giữ broad; **chỉ nên** đổi
   `log_debug` → `log_error` kèm `traceback.format_exc()` (hiện `{e}` không có traceback).
   Nếu muốn narrow: phải chấp nhận sửa 2 test trên + xác nhận turn-abort là hành vi mong muốn.

2. **`lucnhan/agent/loop.py:2822-2897` — `_verify_mutation` broad catch.**
   Docstring ghi rõ "This method never raises — all exceptions are caught and returned as a failed
   verification result" (2811-2812). Ba nhánh dự kiến (`ToolNotFoundError`, `ToolError`, JSON parse)
   đã được narrow trước đó (2891-2894, 2872-2875); nhánh `except Exception` còn lại là lưới an toàn
   để **không ghi undo record sai**. Thu hẹp sẽ khiến bug verification propagate vào khối tool →
   đổi một mutation thành công thành tool error (cùng bệnh với P1-1). Giữ broad; đổi `log_debug`
   → `log_error` + traceback. Lý do dùng `log_error`: verification fail làm mất record undo — cần
   thấy trong log, không phải debug.

3. **`lucnhan/tools/base.py:304-312` — `@tool` wrapper catch-all.** Đây là biên chuyển mọi
   exception handler thành `ToolError`; đã có `log_trace` + `_log_error` với `traceback.format_exc()`
   (311). Narrow không khả thi vì handler có thể ném bất kỳ type. Ghi chú (không phải yêu cầu):
   nếu tương lai có tool ném `CancellationError` (subclass `AgentError` ⊂ `Exception`), wrapper này
   sẽ wrap nó thành `ToolError` — cần thêm `except CancellationError: raise` **trước** catch-all.
   Hiện chưa có tool nào làm vậy (`grep CancellationError lucnhan/tools/` = 0), nên không sửa.

4. **`lucnhan/tools/registry.py:407-412, 495-500, 565-572` — boundary dispatch.**
   Thiết kế có chủ đích: registry là nơi duy nhất biến exception của tool thành `ToolError`
   (contract của `execute`/`execute_coerced`/`execute_current_thread`). Narrow không thể vì không
   liệt kê hết exception của 80+ tool. `except TypeError → ToolValidationError` chỉ còn là lưới
   phụ vì `@tool` wrapper đã tách `TypeError` của body (base.py:298-303) — nhưng **không xoá** vì
   registry còn phục vụ handler không qua decorator (test/MCP). Tương tự, thêm
   `except CancellationError: raise` được khuyến nghị khi có tool thật sự ném cancel; hiện chưa có.

5. **`lucnhan/ida/tools/registry.py:156-170`** — `register_advanced_tools`: catch-all quanh
   `importlib.import_module` + `register_module` cho module optional. Đây là pattern được CLAUDE.md
   bảo vệ (import `ida_*` phải `try/except ImportError`, và module advanced có thể thiếu dependency).
   Giữ broad (một module lỗi không được chặn module khác); chỉ nên thêm traceback vào
   `log_warning` (hiện chỉ `{e}`). **Không** đổi sang `except ImportError` — sẽ làm sập đăng ký tool
   khi module lỗi vì lý do khác.

6. **`lucnhan/ida/dispatch.py:126-136` (`IdaUiDispatcher.wrap`)** và
   **`lucnhan/core/thread_safety.py:92-107` (`idasync`)** — `except Exception` ở đây là
   **transport**, exception được lưu vào list và `raise` lại ở caller (dispatch.py:134-135,
   thread_safety.py:105-106). Không nuốt. Giữ nguyên. Ghi chú: cả hai dùng `Exception`, nên
   `SystemExit`/`KeyboardInterrupt` xảy ra trong thân hàm sẽ không được transport (xem P1-5 —
   sửa ở sink là đủ cho script; các tool IDA khác không nên raise BaseException).

7. **`lucnhan/memory/service.py:196-216, 307-327` — projection failure.** Broad catch có chủ đích:
   SQLite write đã commit, projection MEMORY.md lỗi thì trả `SaveMemoryResult(projection_dirty=True,
   warning=str(exc))` và `mark_projection_dirty()`; `log_error` đã có. Không thu hẹp: projector có
   thể ném `OSError`/`sqlite3.Error`/`ProjectionConflictError`/`ManagedRegionError` — tất cả đều
   phải degrade, không rollback DB. (`warning` không đi vào prompt của LLM, chỉ UI.)

8. **`lucnhan/memory/markdown.py:226-236` — probe `portalocker`.** Cố ý degrade khi dependency
   không có (`except Exception`, log debug). Không thu hẹp: môi trường đóng gói có thể làm import
   raise bất kỳ lỗi nào. Nếu muốn chặt hơn: nâng log lên `warning` để biết đang chạy chỉ với
   in-process lock (mất lock xuyên process) — **không** đổi sang `except ImportError` vì sẽ làm
   `/report`/`save_memory` văng lỗi trên môi trường hỏng.

9. **`lucnhan/memory/raw_store.py:110-117, 321-338`** — `except Exception` để cleanup temp /
   rollback rồi `raise` (110-117), hoặc log rollback failure rồi `raise` lỗi gốc (321-338).
   Đây là cleanup + re-raise, không nuốt. Giữ nguyên. (`raw_store.py:110` nên là `BaseException`
   nếu muốn dọn temp cả khi `KeyboardInterrupt`, nhưng đó là thay đổi ngoài phạm vi.)

10. **`lucnhan/memory/workspace_store.py:586, 804, 857, 971, 1027, 1067, 1089, 1100, 1127`** —
    `except Exception/BaseException: rollback(); raise`. Cleanup + re-raise, không nuốt. Giữ.

11. **`lucnhan/memory/sqlite_backend.py:147-150`** — `except BaseException: rollback/close; raise`
    quanh migration. Đúng (transaction không được để dở). Giữ.

12. **`lucnhan/core/sanitize.py:597, 686`** — cả hai `except re.error` cho custom IOC regex là
    **ví dụ mẫu** về narrow đúng: chỉ bắt lỗi biên dịch regex, log debug, bỏ rule. `sanitize.py`
    không có `except Exception` nào khác, nên **không có candidate** trong file này.

13. **`lucnhan/tools/script_guard.py:407` — `except SyntaxError` trong `_check_ast`.** Đúng type
    (AST parse). Giữ. (Khác với P1-5 ở sink `exec`.)

14. **`lucnhan/agent/loop.py:841-845` (`/case`), 1943-1951 (reference injection), 2740-2745
    (context enrichment), 2774-2777 (delegation), 3249-3251 (`run` top-level), 3491-3494
    (`BackgroundAgentRunner`)** — biên lệnh/thread/agent. Tất cả đã log (một số ở debug) và không
    được phép làm sập turn; narrow ở đây sẽ chỉ đổi lỗi thành crash. Giữ. Ghi chú: `2774` nên
    thêm `except CancellationError: raise` **nếu** A2A dispatcher tương lai ném cancel — hiện
    dispatcher chỉ poll `cancel_event` và yield event, không ném.

15. **`lucnhan/agent/subagent_manager.py:589-605`** — bắt `Exception` để set `FAILED` + emit event,
    có kiểm tra `cancel.is_set()` trước đó (590-592). Biên worker thread; giữ.

16. **`lucnhan/ida/tools/microcode.py:290-297` (`nop_microcode`)** — sau khi optimizer **đã install**
    (287-288), redecompile lỗi được báo bằng chuỗi "installed ... but redecompilation error". Broad
    catch ở đây bảo vệ thông điệp "đã install" (và do đó `/undo` vẫn nhận record, vì chuỗi không
    khớp `_FAILURE_PREFIXES`). Narrow sẽ biến bug thành `ToolError` sau khi đã mutate → mất record
    undo (đúng bệnh P1-1). Giữ broad; nếu muốn, thêm `log_error` + traceback. **Không** narrow.

17. **`lucnhan/ida/tools/types_tools.py:737-749, 752-774`** — hai fallback API IDA
    (`modify_user_lvar_info` rồi `modify_user_lvars`). Broad catch có chủ đích để thử API thay thế;
    failure cuối cùng vẫn trả chuỗi "Failed to retype ...". Giữ nguyên, không cần sửa.

18. **`lucnhan/ida/tools/file_io.py:136-167` (`read_file`)** — đã narrow đúng
    (`_FileToolError`, `StorageError`, `OSError`, `UnicodeDecodeError`). Đây là **mẫu** cho các tool
    khác. Không sửa.

---
