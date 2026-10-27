# Deobfuscator của Luc Nhan — nghiên cứu kiến trúc & đo lường thực tế

> **Phạm vi**: toàn bộ đường deobfuscation trong repo Luc Nhan (IDA Pro plugin): skill playbook, primitive
> microcode/emulation/scripting, guardrail & undo, hành vi UI/agent.
> **Phương pháp**: đọc mã nguồn (trích `file:line` cho mọi khẳng định) + **chạy thí nghiệm thật** trên
> chính `lucnhan.skills.loader` / `lucnhan.core.sanitize` / `lucnhan.skills.registry` bằng interpreter
> `$IDAPYTHON_VENV_EXECUTABLE` (Python 3.13.14, `ida_domain/unicorn/z3` đều có mặt) — không cần mở IDA.
> **Mốc repo**: `d58810d` (master). Tài liệu liên quan: [research-ai-deobfuscation.md](research-ai-deobfuscation.md)
> (khảo sát kỹ thuật/công cụ bên ngoài); tài liệu này tập trung vào **thứ đang thực sự chạy trong repo**.

---

## 1. TL;DR

1. **Không tồn tại một "deobfuscator" như module biến đổi.** Deobfuscator ở đây = *agent loop* + 5 nhóm
   primitive (đọc microcode, ghi microcode, emulate, annotate, `execute_python`) + *một playbook bằng
   tiếng Anh* + *sổ ghi mutation để undo*. Chính skill nói thẳng: **"You are the deobfuscator"**
   (`lucnhan/skills/builtins/deobfuscation/SKILL.md:119`). Không có pass tự động nào chạy CFF/MBA thay người dùng.
2. **Playbook bị cắt 65% ngay tại thời điểm inject.** Payload thật khi host = IDA là **141.574 ký tự**;
   `sanitize_skill_body()` cắt còn **50.054 ký tự** vì `SKILL_MAX_CHARS = 50_000`
   (`lucnhan/core/sanitize.py:343,420-421`). Chỉ 35,4% playbook tới được model (~12,5k token thay vì ~35,4k).
3. **Phần bị cắt đúng là phần quan trọng nhất**: `tools.md` (danh mục tool), `guide.md` (bảng *Critical
   Rules*), `microcode-guide.md`, `cff-recovery.md`, `string-decryption.md`, `self-restoring-binary.md`.
   `api-hashing.md` bị cắt giữa bảng LummaC2. **Rule chống crash `mba.maturity >= 6` chỉ tồn tại trong 2
   file bị cắt** (`guide.md:89,150`, `microcode-guide.md:28,30`) → model không bao giờ thấy, trong khi
   `SKILL.md:262` chỉ tay "check the host-specific reference files" (đúng những file đã bị cắt).
4. **Trigger là substring, và skill bật plan mode**: 27 trigger (`deobfuscation/SKILL.md:29-56`) so khớp
   bằng `t in text_lower` (`lucnhan/skills/registry.py:135`), thắng theo số hit; `mode: plan` âm thầm
   chuyển chat sang plan mode (`lucnhan/agent/loop.py:1043-1050`, `3146-3150`). Thí nghiệm: **9/9 câu vô
   hại** ("continuous integration", "samba configuration", "flatten a nested json list"…) đều route sang
   `/deobfuscation`.
5. **Primitive mạnh nhất bị khoá khỏi subagent**: `install_microcode_optimizer` có `requires_approval=True`
   (`lucnhan/ida/tools/microcode.py:311`); loop unattended ẩn mọi tool cần approval
   (`lucnhan/agent/loop.py:2970-2979`) ⇒ subagent chỉ recon/emulate, mutation cấu trúc buộc phải ở main chat.
6. **Tầng emulation là phần chắc tay nhất**: Unicorn per-call, read-only IDB, từ chối syscall/int **trước
   khi thực thi**, snapshot memory trung thực, budget 100k (cap 1M) instruction, 5s (cap 20s), 16 MiB mapped,
   4096 B × 16 capture, không zero-fill bừa (`lucnhan/ida/tools/emulation.py:1-43,90-141,1601-1833`).
7. **Undo không đối xứng**: `nop_microcode` reversible (`lucnhan/agent/mutation.py:541-588`);
   `install_microcode_optimizer` / `remove_microcode_optimizer` **không** reversible
   (`lucnhan/agent/mutation.py:964-1001`).

---

## 2. Kiến trúc thực tế

```
UI (IDA context menu / chat)
  └─ action "lucnhan:deobfuscate"            lucnhan/ida/ui/actions.py:179-224
       └─ handle_deobfuscate(ctx, term="microcode")   lucnhan/ui/action_handlers.py:47-54
            │  (chỉ SINH prompt; không gọi tool nào)
            ▼
AgentLoop.run()                              lucnhan/agent/loop.py:3146-3165
  ├─ _resolve_skill(): /slug hoặc match_triggers → active_skill      loop.py:1017-1053
  ├─ active_skill.mode == "plan" → use_plan_mode = True              loop.py:3149-3150
  ├─ run_plan_mode(): inject skill body qua sanitize_skill_body()    lucnhan/agent/modes/plan.py:138-157
  └─ tools_schema: bỏ tool approval-gated khi unattended; lọc allowed_tools
                                                                     loop.py:2968-2984
            ▼
ToolRegistry.execute() → @idasync marshalling về main thread (IDA API)
  ├─ đọc: get_microcode / get_microcode_block / decompile_function / read_function_disassembly
  ├─ ghi: nop_microcode (NopOptimizer) / install_microcode_optimizer (optinsn_t|optblock_t)
  ├─ chạy: emulate_code / resolve_emulated_string (Unicorn, main_thread=False)
  └─ thoát hiểm: execute_python (approval từng script)
            ▼
MutationRecord (pre-state + reverse op) → /undo        lucnhan/agent/mutation.py
            ▼
TurnEvent stream → queue.Queue → QTimer poll → Qt UI
```

| Thành phần | File | Vai trò trong deobfuscation |
|---|---|---|
| Playbook | `skills/builtins/deobfuscation/SKILL.md` + `references/ida/*.md` | Quy trình, nhận diện, identity MBA, guard kỹ thuật |
| Đọc microcode | `ida/tools/microcode.py:140-241` | `get_microcode` (8 mức maturity), `get_microcode_block` (operand chi tiết) |
| Ghi microcode | `ida/tools/microcode.py:249-475`, `microcode_optim.py` | Optimizer instruction/block, NOP theo địa chỉ, redecompile |
| Emulation | `ida/tools/emulation.py` (+`emulation_memory/output/types`) | Giải mã string, chứng minh opaque predicate, dựng lại 1 vòng dispatcher |
| Annotation | `ida/tools/annotations.py`, `types_tools.py` | `set_comment`, `rename_function`, `set_type` sau khi làm sạch |
| Thoát hiểm | `ida/tools/scripting.py`, `tools/script_guard.py` | `execute_python`: z3, `ida-domain` hooks, sửa byte anti-disassembly |
| Guard | `core/sanitize.py`, `agent/mutation.py`, `agent/loop.py` | Chống prompt-injection từ binary, undo, approval |

---

## 3. Tầng playbook — nơi deobfuscator "suy nghĩ"

### 3.1 Frontmatter & cách nạp

`SKILL.md` khai báo `name`, `description` (block scalar `>-`), `tags`, `mode: plan`, **27 `triggers`**,
không có `allowed_tools` (`deobfuscation/SKILL.md:1-57`). Loader chỉ parse các field này
(`lucnhan/skills/loader.py:281-307,414-427`); **body + toàn bộ reference được nối thành một chuỗi** khi
truy cập `SkillDefinition.body` (`loader.py:302-326`), và `_load_references()` nạp cả thư mục
`references/<host>/` theo host đang chạy (`loader.py:329-370`).

> Điểm cần nói rõ: frontmatter/loader docstring nói "lazy load on first access" — đúng ở mức **skill**,
> nhưng trong một skill thì **không có cơ chế nạp từng reference theo nhu cầu**. Tài liệu nội bộ
> [research-ai-deobfuscation.md](research-ai-deobfuscation.md) §2 liệt kê "references progressive
> disclosure" như một điểm mạnh; đo đạc dưới đây cho thấy điều đó không đúng với implementation hiện tại.

### 3.2 Kích thước payload (đo trên file thật)

| File | Ký tự | Dòng |
|---|---:|---:|
| `SKILL.md` (raw) | 12.464 | 263 |
| └ body sau frontmatter | 10.959 | — |
| `references/ida/algorithm-reference.md` | 12.965 | 259 |
| `references/ida/api-hashing.md` | 34.764 | 715 |
| `references/ida/cff-recovery.md` | 19.341 | 224 |
| `references/ida/guide.md` | 10.825 | 233 |
| `references/ida/microcode-guide.md` | 6.584 | 234 |
| `references/ida/self-restoring-binary.md` | 17.378 | 243 |
| `references/ida/string-decryption.md` | 23.622 | 441 |
| `references/ida/tools.md` | 7.313 | 92 |
| **Blob reference (nối)** | **130.613** | — |
| **Tổng payload inject** | **141.574** | — |
| **Thực inject sau sanitize** | **50.054** | — |

### 3.3 Bằng chứng truncation (chạy thật, host = IDA)

```
skill.body (with refs)   : 141,574 chars
sanitize_skill_body()    : 50,054 chars  cap=50,000
truncated?               : True
kept share               : 35.4%         (~12.5k token giữ lại / ~35.4k token)
```

Thứ tự nối là `sorted(os.listdir(...))` của `references/ida` (`loader.py:346-368`) → thứ tự alphabet quyết
định ai sống ai chết:

| Reference | Offset trong payload | Trạng thái sau cap 50k |
|---|---|---|
| `algorithm-reference.md` | 10.961 – 23.704 | **FULL** |
| `api-hashing.md` | 23.706 – 57.784 | **PARTIAL** — cắt giữa bảng LummaC2 (dòng cuối còn đọc được: `… Targets / Crypto wallets (Binance, Electrum, Ethereu`) |
| `cff-recovery.md` | 57.786 – 76.933 | DROPPED |
| `guide.md` | 76.935 – 87.550 | DROPPED |
| `microcode-guide.md` | 87.552 – 93.935 | DROPPED |
| `self-restoring-binary.md` | 93.937 – 111.111 | DROPPED |
| `string-decryption.md` | 111.113 – 134.329 | DROPPED |
| `tools.md` | 134.331 – 141.574 | DROPPED |

### 3.4 Hệ quả cụ thể

| Mất gì | Ở đâu | Tác động |
|---|---|---|
| `mba.maturity >= 6` guard | `guide.md:89,150`, `microcode-guide.md:28,30` | Model có thể sửa microcode ở `MMAT_LVARS` → internal error/assert của Hex-Rays; `guide.md:219` ("Crash / internal error after microcode edit") cũng bị cắt |
| `equal_mops(b, EQ_IGNSIZE)` | `guide.md:151` | **Vẫn sống** nhờ `algorithm-reference.md:26,38` (may mắn) |
| `mark_lists_dirty` / `mark_chains_dirty` | `guide.md:94,153` | **Vẫn sống** nhờ `algorithm-reference.md:38` |
| Danh mục tool + bảng "Choosing the Right Approach" | `tools.md:1-92` | Model phải đoán tool nào tồn tại; dễ gọi `execute_python` (chậm, phải approval) thay vì optimizer |
| `remove_microcode_optimizer` như bước dọn dẹp | `guide.md:18`, `tools.md:60` | Không ai nhắc model dọn optimizer sau khi xong → optimizer "rác" tồn tại tới hết phiên IDA |
| Toàn bộ quy trình string decryption inline-stack (Conti/ADVobfuscator), self-restoring, CFF bằng emulation | `string-decryption.md`, `self-restoring-binary.md`, `cff-recovery.md` | 3 lớp obfuscation phổ biến mất playbook; `SKILL.md:99-115,220-232` vẫn viện dẫn chúng ("See references/…") |

Nói cách khác: model nhận được **SKILL.md + algorithm-reference + 3/4 api-hashing**, tức là phần "động
não" của CFF/opaque/MBA ở mức khái niệm, nhưng mất toàn bộ **quy tắc an toàn khi ghi**, **catalogue tool**,
và **3 case study chuyên biệt**.

---

## 4. Tầng primitive

### 4.1 Bảng tool liên quan deobfuscation

| Tool | Flags | Giới hạn / hợp đồng | Nguồn |
|---|---|---|---|
| `get_microcode(address, maturity="MMAT_LVARS")` | `requires_decompiler` | 8 mức: GENERATED(0)…LVARS(7); nhận tên hoặc số | `microcode.py:140-177` |
| `get_microcode_block(address, block_index, maturity)` | `requires_decompiler` | dump operand L/R/D chi tiết + preds/succs | `microcode.py:180-241` |
| `nop_microcode(func_address, instruction_addresses, optimizer_name="")` | `mutating` — **không approval** | cài `NopOptimizer` **vĩnh viễn** cho mọi lần decompile; redecompile ngay | `microcode.py:249-303` |
| `install_microcode_optimizer(name, description, optimizer_type, python_code)` | `mutating` + **`requires_approval`** | `instruction` (`optimize(blk, ins)`) hoặc `block` (`optimize(blk)`); code qua `script_guard` AST + `safe_builtins()` | `microcode.py:311-396`, `microcode_optim.py:121-143` |
| `remove_microcode_optimizer(name)` / `list_microcode_optimizers()` | `mutating` / — | registry là dict module-global | `microcode.py:404-436`, `microcode_optim.py:28` |
| `redecompile_function(address)` | — | `mark_cfunc_dirty()` + `decompile()` để xem hiệu quả | `microcode.py:444-475` |
| `emulate_code(…)` | `main_thread=False`, timeout 30s | 13 tham số; xem §4.3 | `emulation.py:1601-1708` |
| `resolve_emulated_string(…)` | `main_thread=False` | wrapper capture 1 buffer; đúng 1 trong `output_address`/`output_stack_offset`; 1..4096 B | `emulation.py:1711-1833` |
| `execute_python` | `requires_approval` (từng script, trừ khi user bấm "Allow for this chat") | namespace hạn chế + blocklist AST | `tools/script_guard.py`, `agent/loop.py:1891-1916` |

### 4.2 Ghi microcode — cơ chế

- `NopOptimizer(ida_hexrays.optinsn_t)`: `func()` đặt `ins.opcode = m_nop` nếu `ins.ea in target_eas`
  (`microcode_optim.py:36-52`).
- `DynamicInsnOptimizer(optinsn_t)` / `DynamicBlockOptimizer(optblock_t)`: gọi code người dùng, mọi exception
  bị nuốt và trả 0 (`microcode_optim.py:55-90`) — an toàn cho decompiler nhưng **im lặng khi rule sai**,
  model chỉ thấy "applied 0 times".
- Namespace cấp cho optimizer: `safe_builtins()` + `ida_hexrays` + mọi hằng `m_*`, `mop_*`, `MMAT_*`, `BLT_*`
  (`microcode_optim.py:108-118`). Đủ để viết rule; `__import__` bị thay bằng `_guarded_import`.
- Vòng đời do model điều khiển: install → `redecompile_function` → kiểm tra → remove. Không có cơ chế
  "chạy thử trên bản sao rồi rollback" tự động.

### 4.3 Emulation — phần được thiết kế kỹ nhất

Từ docstring + hằng số (`emulation.py:1-43,90-141,1601-1833`):

- **Hai chế độ**: `range` (mặc định; `[start, stop)`, cần `registers` khác rỗng) và `function`
  (`calling_convention` ∈ cdecl/stdcall/fastcall (x86), win64/sysv64 (x64); `arguments` theo thứ tự khai
  báo; `registers={}` được phép; `stop_address` là sentinel cho `ret`).
- **Ngân sách**: `instruction_limit` mặc định 100.000, cap 1.000.000; `timeout_seconds` mặc định 5,0, cap
  20,0 (tính cả thời gian snapshot); aggregate mapped ≤ 16 MiB (gồm 1 MiB stack tổng hợp).
- **Capture**: `capture_ranges` ≤ 16 cửa sổ, mỗi cửa sổ ≤ 4096 B, có thể theo `stack_offset` (signed, so với
  SP sau ABI); `collect_strings=True` quét **byte đã bị thay đổi**, cap 64 candidate, 256 KiB decode.
- **An toàn**: mọi `syscall`/`sysenter`/`int imm8`/`int3`/`int1`/`into`/`ud2` bị **chặn trước khi chạy**,
  kể cả khi nấp sau prefix (`emulation.py:122-141`); không có API stub; permission giữ theo segment (chỉ
  thêm X cho code range, **không bao giờ thêm W**); đọc/ghi vào padding bị từ chối thay vì zero-fill.
- **Trạng thái dừng có tên**: `completed`, `range_exit`, `instruction_limit`, `unmapped_memory`,
  `permission_error`, `unsupported_instruction`, `timeout`, `cancelled`, `emulator_error` — timeout/cancel
  không bao giờ báo "thành công", nhưng vẫn trả state một phần.
- **Luồng thread**: `main_thread=False` → snapshot (arch + memory) trên host thread rồi chạy Unicorn trên
  worker; không giữ handle IDA nào sang pha CPU (`emulation.py:19-23`).

### 4.4 Kỹ thuật → primitive (thực tế, không phải như playbook tuyên bố)

| Kỹ thuật | Primitive dùng được | Thiếu gì |
|---|---|---|
| String decryption inline/stack | `emulate_code` + `resolve_emulated_string` (+ `collect_strings`) | Playbook case study (file bị cắt); decode cần API/state ngoài → `execute_python` |
| API hashing (MurmurHash2…) | `emulate_code` đọc EAX; xref walker | Phần "binary rewriting" của LummaC2 cần patch byte → `execute_python` |
| CFF | block optimizer (rewire trong 1 block) **hoặc** `execute_python` + `ida-domain` hooks cho map state→handler đa block | `algorithm-reference.md:43` tự thừa nhận cần hook để rewire toàn cục; hook = approval |
| Opaque predicate | instruction optimizer ép `m_goto`/`m_nop`; z3 qua `execute_python` để chứng minh | Không có solver tích hợp |
| MBA | instruction optimizer (fold hằng, identity) | Không có pass chuẩn hoá tự động |
| Instruction substitution | instruction optimizer (pattern) | Không có rule table dựng sẵn |
| Junk/dead store | `nop_microcode` hoặc optimizer | — |
| Anti-disassembly | `execute_python` + `ida_bytes.patch_byte` | Không có tool patch byte chuyên dụng |
| Self-restoring (pushfq/popfq) | **chỉ** `execute_python` (Unicorn hook `UC_HOOK_CODE` + `reg_write(RIP, …)`) | `emulate_code` không expose custom hook — đúng như `SKILL.md:229-232` nói |
| VM boundary | không có | Chỉ "document + escalate" (Miasm/Triton) |

---

## 5. Guardrail, approval & undo

### 5.1 Ma trận quyền

| Hành động | Approval? | Undo? | Ghi chú |
|---|---|---|---|
| `get_microcode`, `decompile_function`, `read_*` | Không | — | Chỉ đọc |
| `emulate_code`, `resolve_emulated_string` | Không | — | Read-only với IDB, chạy Unicorn |
| `nop_microcode` | **Không** | **Có** → `remove_microcode_optimizer` (`mutation.py:541-588`) | Nhưng tạo optimizer toàn cục, tồn tại tới hết phiên |
| `install_microcode_optimizer` | **Có** (`microcode.py:311`) | **Không** (`mutation.py:964-973`) | Code do LLM viết, exec trong namespace hạn chế |
| `remove_microcode_optimizer` | Không (mutating) | **Không** (`mutation.py:991-1001`) | Xoá là mất `python_code` gốc |
| `execute_python` | **Có**, từng script (`loop.py:1891-1916`) | Không | "Allow for this chat" chỉ do user bấm |
| Rename/comment/type | Không | Có (typed reverse ops) | Dùng để annotate sau khi làm sạch |

### 5.2 Chống prompt-injection từ binary

- Mọi tool result đi qua `sanitize_tool_result()` trước khi vào history; skill body qua
  `sanitize_skill_body()` (strip marker + neutralise closing tag + **truncate 50k**) — `core/sanitize.py:410-423`.
- Tên/chuỗi lấy từ binary khi vào prompt đều qua `strip_injection_markers()`. Với deobfuscation, đây là
  đường tấn công thật: **plaintext giải mã ra là dữ liệu không tin cậy** (C2, đường dẫn, khoá registry) và
  được `set_comment` ghi thẳng vào IDB — hợp lý, nhưng nội dung đó lại quay vào prompt ở lượt sau.

### 5.3 Bất đối xứng vận hành

- Registry optimizer là **dict module-global** (`microcode_optim.py:28`): reload plugin/khởi động lại IDA là
  mất, trong khi `/undo` chỉ replay lời gọi tool → `remove_microcode_optimizer` sẽ trả "No optimizer named…".
  Với `nop_microcode`, đây là mất khả năng undo thật sau restart (dù IDB không bị sửa).
- Không có UI hiển thị optimizer đang hoạt động ngoài tool `list_microcode_optimizers` → người dùng khó biết
  vì sao pseudocode "sạch" khác thường.

---

## 6. Hành vi đo được (không suy đoán)

### 6.1 Trigger auto-activation

Chạy `SkillRegistry.match_triggers()` với registry thật (13 skill builtin):

| Câu người dùng | Skill thắng | Ghi chú |
|---|---|---|
| "analyze this obfuscated binary please" | `deobfuscation` | đúng ý |
| "help me flatten a nested json list" | `deobfuscation` | sai — "flatten" |
| "why is the continuous integration failing" | `deobfuscation` | sai — "conti" ⊂ "continuous" |
| "review this samba configuration file" | `deobfuscation` | sai — "mba" ⊂ "samba" |
| "what does this goldengate mba module do" | `deobfuscation` | sai |
| "explain the junk code in this function" | `deobfuscation` | biên |
| "unpack this vmprotect sample" | `deobfuscation` | đúng ý |
| "sum up the risk of the conti leak" | `deobfuscation` | đúng ngữ nghĩa malware, nhưng vẫn là trùng chuỗi |

Cơ chế: `hits = sum(1 for t in skill.triggers if t in text_lower)` (`registry.py:135`), hoà thì skill đầu
tiên theo thứ tự dict thắng (`>` chứ không `>=`, `registry.py:136-138`). Vì `mode: plan`, mỗi lần khớp
**không** rewrite message mà bật plan mode (`loop.py:1046-1047` → `3149-3150`), rồi plan runner inject body
một lần (`plan.py:147-157`).

### 6.2 Maturity mặc định

`get_microcode(address)` mặc định `MMAT_LVARS` = mức 7 (`microcode.py:143`) — đúng mức **đã mất cấu trúc
dispatcher** và là mức bị cấm sửa. Model gọi thiếu tham số sẽ nhìn thấy bản "đã tối ưu", tức bằng chứng
obfuscation đã bị Hex-Rays dọn mất. `SKILL.md`/`algorithm-reference.md` khuyên đọc `PREOPTIMIZED`/`LOCOPT`
nhưng mặc định của tool thì ngược lại.

---

## 7. Đánh giá

### 7.1 Điểm mạnh (có bằng chứng)

1. **Emulation layer đúng chuẩn an toàn**: chặn syscall trước khi chạy, không mutate IDB, snapshot trung
   thực, budget rõ ràng, trạng thái dừng có tên — hiếm plugin LLM-RE nào làm tới mức này
   (`emulation.py:1-43,122-141`).
2. **`algorithm-reference.md` chất lượng cao**: state variable theo *tần suất so sánh hằng*, giả định
   nhiều dispatcher, xử lý `mop_r` vs `mop_S`, `make_blkref` cho target `mop_v/mop_a`
   (`algorithm-reference.md:13-70`) — đúng kỹ thuật D-810/HexRaysDeob.
3. **Vòng lặp read → hiểu → sửa → redecompile → verify** được cài vào playbook như luật cứng
   (`SKILL.md:254-262`).
4. **Hạ tầng mutation/undo** nghiêm túc: mỗi tool ghi đều có pre-state + reverse op hoặc lý do không thể
   undo được ghi rõ (`mutation.py:936-1001`).
5. **Phân quyền theo ngữ cảnh**: unattended subagent không thấy tool approval-gated (`loop.py:2970-2979`) —
   thiết kế tốt, chỉ cần biết để kiến trúc hoá công việc.

### 7.2 Điểm yếu, xếp hạng

**P0-1 — Truncation 50k làm mất 65% playbook.** *(đã áp dụng một phần — xem §9)*
Bằng chứng: §3.3. Đây là lỗi *im lặng*: model không biết mình thiếu gì (văn bản kết thúc bằng `… [truncated]`
giữa bảng LummaC2).
Đề xuất (chọn 1 hoặc kết hợp):
- (a) **Nạp reference theo yêu cầu**: thêm tool `read_skill_reference(slug, file)` + chỉ inject `SKILL.md`
  và một `INDEX.md` ngắn; sửa `loader.py:321-324` để không nối refs mặc định.
- (b) **Đặt tên theo thứ tự ưu tiên** (`00-tools.md`, `01-guide.md`, …) và/hoặc sắp theo
  `references/INDEX.md` thay vì `sorted(listdir)` (`loader.py:346-368`) — sửa 1 dòng, giữ nguyên kiến trúc.
- (c) Nâng `SKILL_MAX_CHARS` (50k → 200k) — rẻ nhưng đẩy ~35k token vào **mọi** lượt của chat đó; chỉ nên
  làm cùng (a)/(b).
Ước lượng: (b) một mình đã trả lại `guide.md` + `tools.md` (≈18k ký tự) — đúng 2 file chứa rule an toàn và
catalogue tool.

**P0-2 — Rule chống crash không bao giờ tới model.** *(đã áp dụng — xem §9)*
`mba.maturity >= 6`, `EQ_IGNSIZE`, `mark_lists_dirty/mark_chains_dirty`, "never instantiate `minsn_t`"
(`guide.md:146-159`). Đề xuất: copy 4 dòng này vào khối **Critical Rules** của `SKILL.md:254-262` (file này
luôn sống sót) — sửa 4 dòng markdown, rủi ro bằng 0.

**P1-1 — Trigger substring + plan mode tự động.**
`registry.py:135` so khớp substring; 27 trigger gồm các chuỗi ngắn dễ trùng (`conti`, `mba`, `cff`,
`flatten`, `obfuscated`). Đề xuất: khớp theo biên từ (`re.search(rf"\b{re.escape(t)}\b", text)`), và **không**
bật plan mode từ trigger (chỉ bật khi user gõ `/deobfuscation`), hoặc yêu cầu ≥2 hit.

**P1-2 — Mặc định maturity ngược khuyến nghị.**
`microcode.py:143` mặc định `MMAT_LVARS`; đổi mặc định sang `MMAT_LOCOPT` (hoặc thêm câu "default LVARS is
the worst level for spotting obfuscation" vào description) để model không tự bịt mắt.

**P1-3 — Không có test hồi quy cho payload.**
`tests/tools/test_skills.py` chỉ test parse frontmatter (ví dụ `test_folded_scalar_strip_indicator`,
`test_skills.py:85-103`); không test nào khẳng định reference được inject đủ, hay payload < cap. Đề xuất test:

```python
def test_deobfuscation_references_survive_sanitize(self):
    skill = registry.get("deobfuscation")
    injected = sanitize_skill_body(skill.body, skill.slug)
    for ref in ("algorithm-reference", "guide", "tools", "microcode-guide"):
        self.assertIn(f"## Reference: {ref}.md", injected)
    self.assertNotIn("[truncated]", injected)
```

**P2-1 — Optimizer toàn cục không hiển thị.**
Registry dict module-global (`microcode_optim.py:28`) + không có UI ⇒ khó debug "vì sao pseudocode khác
thường", và mất khả năng undo sau restart. Đề xuất: badge "N optimizers active" trong panel + persist
metadata vào IDB (`ida_netnode`/`idb_event`) nếu muốn undo qua restart.

**P2-2 — Tài liệu nội bộ lệch implementation.**
`docs/research-ai-deobfuscation.md` §2 mô tả "references progressive disclosure" như điểm mạnh — cần sửa
thành "eager concat + hard cap 50k" kèm số đo, nếu không người đọc sẽ tin sai về năng lực thực tế.

---

## 8. Roadmap đề xuất (theo thứ tự chi phí/lợi ích)

| Bước | Việc | File | Ước lượng | Tiêu chí xong |
|---|---|---|---|---|
| 1 ✅ | Đưa 4 rule chống crash vào `SKILL.md` | `deobfuscation/SKILL.md:254-262` | 10 phút | Rule xuất hiện trong payload sau sanitize |
| 2 ✅ | Đổi thứ tự nạp reference + test hồi quy | `skills/loader.py:346-368`, `tests/tools/test_skills.py` | 1 giờ | `guide.md` + `tools.md` nằm trong 50k; test mới fail trước/succeed sau |
| 3 ✅ | `read_skill_reference` (on-demand per-file) | `tools/skill_reference.py` | nửa ngày | Model truy được ref bất kỳ, kể cả phần ngoài 50k; tool đọc theo cửa sổ ≤ 7k |
| 4 | Trigger theo biên từ, bỏ auto plan-mode | `skills/registry.py:122-142` | 30 phút | 9/9 câu vô hại ở §6.1 không còn route |
| 5 | Benchmark định lượng | `docs/EVALUATION_WORKFLOW.md` + sample OLLVM/Tigress | 1–2 ngày | Đo: % hàm decompile sạch sau deobf, crash rate, token/lượt, số vòng tool |

Benchmark tối thiểu nên đo 4 chỉ số: (i) tỉ lệ hàm hết flatten sau N vòng, (ii) số lần decompiler crash,
(iii) token tiêu thụ mỗi lượt (bắt được cả truncation lẫn lan man), (iv) số lần phải nhảy sang
`execute_python` (mỗi lần là một approval round-trip → chỉ số UX).

---

## 9. Patch đã áp dụng (2026-10-27)

Hai patch P0 của §7.2, cộng bước 3 của §8 (đọc reference on-demand), đã được áp dụng kèm test hồi quy.

### 9.1 Rule chống crash vào thẳng `SKILL.md`

`lucnhan/skills/builtins/deobfuscation/SKILL.md` có thêm mục **Microcode write guards** (4 rule: maturity
≥ 6, `EQ_IGNSIZE`, mutate in place, mark dirty) và bullet cuối của *Critical Rules* giờ trỏ
`tools.md`/`guide.md` cho **catalogue tool + recipe**, thay vì trỏ tới nơi chứa rule an toàn. Vì `SKILL.md`
luôn nằm trong 50k đầu tiên, 4 rule này không thể bị cắt nữa.

### 9.2 Thứ tự reference do chính skill khai báo

- `lucnhan/skills/loader.py`: thêm `_read_reference_order()` + `_ordered_reference_names()`. Nếu thư mục
  reference có `INDEX.md` thì thứ tự nạp theo file đó; file không khai báo xếp sau, alphabetical.
  `INDEX.md` **không bao giờ** bị inject. Không có `INDEX.md` → giữ nguyên hành vi cũ, nên 12 skill còn lại
  không đổi hành vi.
- Thêm `lucnhan/skills/builtins/deobfuscation/references/ida/INDEX.md`:
  `tools.md` → `guide.md` → `microcode-guide.md` → `algorithm-reference.md` → `cff-recovery.md` →
  `self-restoring-binary.md` → `string-decryption.md` → `api-hashing.md`.

### 9.3 Đo lại sau patch (cùng phương pháp §3.3)

| Chỉ số | Trước patch | Sau patch |
|---|---:|---:|
| Payload assembled | 141.574 | 142.866 (thêm rule + ghi chú on-demand) |
| Inject sau `SKILL_MAX_CHARS` | 50.054 | 50.054 |
| Reference **nguyên vẹn** trong prompt | 1 (`algorithm-reference`) | **4** (`tools`, `guide`, `microcode-guide`, `algorithm-reference`) |
| `mba.maturity >= 6` trong prompt | Không | **Có** |
| `EQ_IGNSIZE` / `mark_*_dirty` trong prompt | Có (nhờ `algorithm-reference`) | Có |
| `tools.md` (catalogue tool) | Bị cắt | **Nguyên vẹn** |

Thứ tự inject thật sau patch: `tools.md` (offset 12.282) → `guide.md` (19.527) → `microcode-guide.md`
(30.144) → `algorithm-reference.md` (36.529) → `cff-recovery.md` (49.274, chỉ ~760 ký tự đầu) và cắt tại 50.034.

Còn ngoài ngân sách: phần lớn `cff-recovery`, toàn bộ `string-decryption`, `self-restoring-binary`,
`api-hashing`. Đây là giới hạn của cap 50k, không phải lỗi thứ tự — `SKILL.md` giờ chỉ rõ cách lấy chúng
bằng `read_skill_reference` (§9.7).

### 9.4 Test hồi quy

`tests/tools/test_skills.py` thêm 9 test:

- `TestReferenceIndexOrder` (4): `INDEX.md` đổi thứ tự nạp; entry lạ bị bỏ; không có `INDEX.md` → alphabetical;
  `INDEX.md` không bị inject.
- `TestDeobfuscationPayloadBudget` (3): payload > 2× cap (tài liệu hoá *vì sao* cần thứ tự); 4 reference ưu
  tiên **nguyên vẹn** trong prompt (so khớp **toàn văn file**, nên mọi chỉnh sửa làm tràn ngân sách sẽ fail);
  4 rule chống crash có mặt.
- `TestIdaScriptingPayloadBudget` (2): `api-reference.md` nguyên vẹn; payload vượt cap (porting-guide phải
  đọc on-demand).

Chạy: `pytest tests/tools/test_skills.py -q -o addopts=""` → **48 passed, 16 subtests**.
(`-o addopts=""` cần vì venv IDAPython chưa có plugin `pytest-timeout` mà `pyproject.toml` bật sẵn.)

### 9.5 Toàn bộ skill builtin — và `ida-scripting`

Đo payload của cả 13 skill (`discover_skills` + `sanitize_skill_body`, host = IDA):

| Skill | Payload | Bị cắt? |
|---|---:|---|
| `deobfuscation` | 142.866 | có (thứ tự đã xử lý) |
| `ida-scripting` | 110.559 | có (thứ tự đã xử lý, phần dư đọc on-demand) |
| `vuln-audit` | 16.729 | không |
| `naming-convention` | 11.594 | không |
| `emulator` | 11.481 | không |
| 8 skill còn lại | ≤ 6.712 | không |

`ida-scripting` có 2 reference: `api-reference.md` (14.811 B) và `idapython-porting-guide-ida-9.md`
(84.075 B). Đã thêm `references/INDEX.md` (thứ tự `api-reference.md` → porting-guide) nên
`api-reference.md` **nguyên vẹn** ở offset 11.476; porting-guide vào từ offset 28.391 và chỉ ~21,6k/84k
nằm trong prompt — phần còn lại đọc bằng `read_skill_reference`.

### 9.7 `read_skill_reference` — đọc reference ngoài ngân sách (bước 3 của §8)

`lucnhan/tools/skill_reference.py`, đăng ký trong `lucnhan/ida/tools/registry.py:61`
(`_ADVANCED_MODULE_NAMES` → có mặt ở cả panel UI và headless, vì cả hai đều gọi
`register_advanced_tools`).

Hợp đồng:

```
read_skill_reference(skill="deobfuscation")                                  # manifest: file + size
read_skill_reference(skill="deobfuscation", file="ida/string-decryption.md")  # cửa sổ 6.000 ký tự
read_skill_reference(skill="deobfuscation", file="ida/api-hashing.md", offset=6000)
```

- Trả về `=== <slug>/references/<path> — chars A-B of N ===` + `<skill_reference>…</skill_reference>`
  + `... [next offset=B]`; `limit` mặc định 6.000, chặn trên 7.000 để không bị
  `TOOL_RESULT_TRUNCATE_LEN` (8.000) cắt mất phần đuôi.
- Nội dung đi qua `strip_injection_markers()` và được bọc delimiter (skill trên đĩa là *semi-trusted*,
  xem `CLAUDE.md` §3).
- Ranh giới đường dẫn: slug khớp `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`, file phải `resolve()` nằm trong
  `<skill>/references/`, chỉ `.md`, `INDEX.md` bị loại; mọi trường hợp khác trả `ToolError` kèm danh sách
  file hợp lệ.
- Thứ tự tìm skill giống `SkillRegistry.discover()`: built-in trước, user skill (`LucNhanConfig().skills_dir`)
  ghi đè; config hỏng thì fallback về built-in.
- `main_thread=False`, `mutating=False`, không cần approval (chỉ đọc file).

Cả hai `SKILL.md` lớn giờ có ghi chú on-demand kèm ví dụ copy-paste được
(`deobfuscation/SKILL.md` sau danh sách reference, `ida-scripting/SKILL.md` mục *When to fetch more*), nên
model biết chính xác file nào đang thiếu và lấy bằng lệnh nào.

Test: `tests/tools/test_skill_reference.py` — 19 test (metadata tool + đăng ký registry, manifest, cửa sổ/offset,
cap limit, đọc trọn file 23k và 84k bằng nhiều cửa sổ, chặn traversal/non-md/INDEX/slug sai/offset quá cuối,
user skill ghi đè built-in + lọc marker, config hỏng fallback).

### 9.6 Kiểm chứng không hồi quy

Chạy toàn bộ `pytest tests` (venv IDAPython, `-o addopts=""`) trên cây làm việc và trên một
`git worktree` sạch ở `HEAD`:

| | Baseline (`HEAD`) | Sau patch |
|---|---:|---:|
| passed | 3.760 | **3.789** (+28 test mới, +1 flake) |
| failed | 383 | 382 |
| errors | 24 | 24 |
| collected | 4.310 | 4.338 (+28 test mới) |

Đối chiếu **tập id test fail**: không có test nào mới fail; duy nhất
`tests/a2a/test_a2a_http_e2e.py::TestSendTask::test_send_task_returns_task` fail ở baseline và pass ở lượt
sau (test mạng, flaky). 382 fail + 24 error còn lại đều do môi trường: venv IDAPython không có `PySide6`
(`lucnhan/ui/qt_compat.py:30` → `ModuleNotFoundError`), tức toàn bộ test Qt/UI, không liên quan tới patch.

## 10. Phụ lục — lệnh tái lập

```powershell
# 1) Đo payload thật (host = IDA) và xem reference nào sống sót
$env:IDAPYTHON_VENV_EXECUTABLE -c @"
import sys; sys.path.insert(0, r'D:\Program Files\IDAdata\IDAUSR\plugins\Luc-Nhan')
import lucnhan.core.host as h; h.host_kind = lambda: h.HOST_IDA   # giả lập host IDA
from lucnhan.core.sanitize import sanitize_skill_body, SKILL_MAX_CHARS
from lucnhan.skills.loader import discover_skills
sk = {s.slug: s for s in discover_skills(r'D:\Program Files\IDAdata\IDAUSR\plugins\Luc-Nhan\lucnhan\skills\builtins')}['deobfuscation']
body = sk.body; san = sanitize_skill_body(body, sk.slug)
print(len(body), len(san), SKILL_MAX_CHARS, '[truncated]' in san)
for m in ('algorithm-reference','api-hashing','cff-recovery','guide','microcode-guide','self-restoring-binary','string-decryption','tools'):
    print(m, f'## Reference: {m}.md' in san)
"@

# 2) Kiểm tra trigger false positive
$env:IDAPYTHON_VENV_EXECUTABLE -c @"
import sys; sys.path.insert(0, r'D:\Program Files\IDAdata\IDAUSR\plugins\Luc-Nhan')
from lucnhan.skills.registry import SkillRegistry
r = SkillRegistry(r'D:\Program Files\IDAdata\IDAUSR\plugins\Luc-Nhan\lucnhan\skills\builtins'); r.discover()
for p in ['why is the continuous integration failing','review this samba configuration file']:
    print(p, '->', r.match_triggers(p).slug)
"@
```

**Chỉ mục `file:line` quan trọng**: `sanitize.py:343,410-423` (cap + truncate) · `loader.py:302-326,329-370`
(nối reference) · `registry.py:122-142` (trigger) · `loop.py:1017-1053,2970-2979,3146-3165` (resolution,
unattended, plan flip) · `plan.py:138-157` (inject trong plan mode) · `microcode.py:140-177,249-303,311-396`
(đọc/ghi) · `microcode_optim.py:28,36-90,121-143` (optimizer) · `emulation.py:1-43,90-141,1601-1833`
(emulation) · `mutation.py:541-588,964-1001` (undo) · `actions.py:179-224` + `action_handlers.py:47-54`
(entry point).
