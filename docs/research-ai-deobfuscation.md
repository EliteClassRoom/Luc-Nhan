# Nghiên cứu: AI Skills & Công cụ Deobfuscate Binary (Control-Flow Flattening, Mutation)

> Báo cáo nghiên cứu tổng hợp: các skill/kỹ thuật và công cụ (truyền thống lẫn AI/LLM) để
> deobfuscate binary — trọng tâm **control-flow flattening (CFF)** và **mutation obfuscation**
> (opaque predicates / bogus control flow / MBA / instruction substitution).
> Mục tiêu: làm cơ sở nâng cấp skill `deobfuscation` của Luc Nhan.

- Ngày soạn: 2026-10-23
- Phương pháp: đọc tài liệu nội bộ repo (skill `deobfuscation` + 7 file reference) + thu thập tài liệu công khai (arxiv API, GitHub, USENIX/NDSS/ACM, blog kỹ thuật) — mọi nguồn có link xác minh.
- Ghi chú: trong phiên nghiên cứu này, `web_search` của môi trường đang lỗi cấu hình (API key không hợp lệ), nên việc tìm kiếm được thực hiện qua fetch trực tiếp (arxiv API + DuckDuckGo HTML) — kết quả tương đương, các URL dưới đây đều được nạp và xác minh HTTP 200.

---

## 1. Tóm tắt điều hành (Executive Summary)

1. **LLM "tự sửa pseudocode" thiếu cấu trúc là không đủ.** Đánh giá hệ thống trên 7 LLM thương mại
   ([Deconstructing Obfuscation, 2025](https://arxiv.org/abs/2505.19887)) cho thấy 5 loại lỗi lặp lại
   (hiểu sai predicate, sai ánh xạ cấu trúc, hiểu sai control flow, sai biến đổi số học, sai lan truyền hằng số)
   và **thất bại toàn diện khi obfuscation kết hợp nhiều kỹ thuật**. Nghiên cứu độc lập
   ([BinDeObfBench, 2026](https://arxiv.org/abs/2604.08083)) xác nhận: khả năng deobfuscate phụ thuộc
   *lý luận và domain expertise* hơn là kích thước model; fine-tune theo tác vụ thắng pre-train rộng.

2. **Chain-of-Thought + phân rã công việc + tool-use là hướng được kiểm chứng.** Với CFF/opaque predicates,
   prompting CoT cải thiện trung bình **+16% dựng lại CFG và +20.5% bảo toàn ngữ nghĩa** so với zero-shot
   ([arXiv 2604.15390](https://arxiv.org/abs/2604.15390)). Fine-tune kèm artifact symbolic execution
   (KLEE: ràng buộc SMT, path statistics, test cases) cải thiện thêm độ bảo toàn ngữ nghĩa
   ([arXiv 2511.19130](https://arxiv.org/abs/2511.19130)).

3. **Công cụ truyền thống vẫn là xương sống** và có thể "vay" làm agent tool: microcode rewriting
   (HexRaysDeob, D-810/d810-ng), lifting + LLVM + SMT (SATURN), symbolic execution + LLVM (Titan, NoVmp),
   Miasm/angr deflattener (ollvm-unflattener, MODeflattener). Skill Luc Nhan đã có sẵn nền tảng
   (microcode optimizer + emulation) tương đương phần lõi của nhóm công cụ này.

4. **Skill `deobfuscation` hiện tại của Luc Nhan đã rất khớp best practice** (đọc IL trước khi sửa,
   verify sau mỗi bước, maturity guard, emulation-first). Khoảng trống chính: (a) tên công cụ devirtualization
   cụ thể ở bước "VM Boundary", (b) Z3/symbolic-execution như *oracle* xác minh, (c) checklist CFF chuẩn hoá,
   (d) benchmark để đo chất lượng, (e) tận dụng MCP client (Hex-Rays đã có IDA MCP server chính thức).

5. **Hệ sinh thái skill/agent khác** (Anthropic Agent Skills, AGENTS.md, MCP RE servers, AIxCC/ATLANTIS)
   xác nhận thiết kế hiện tại của Luc Nhan (SKILL.md + frontmatter + references progressive disclosure
   + allowed_tools) là chuẩn công nghiệp.

---

## 2. Hiện trạng skill `deobfuscation` của Luc Nhan

Skill hiện có tại [`lucnhan/skills/builtins/deobfuscation/SKILL.md`](../lucnhan/skills/builtins/deobfuscation/SKILL.md)
cùng 7 file reference trong `references/ida/`:

| Reference | Nội dung |
|---|---|
| `tools.md` | Danh sách tool: đọc (`decompile_function`, `get_microcode`), emulate (`emulate_code`, `resolve_emulated_string`), ghi microcode (`nop_microcode`, `install_microcode_optimizer`), annotation, `execute_python` |
| `guide.md` | Hai đường chính: (A) built-in microcode optimizer, (B) ida-domain `DecompilerHooks`; luật kỹ thuật từng loại obfuscation |
| `algorithm-reference.md` | Nhận diện + phương pháp: CFF (state variable, dispatcher, handler, rewiring), opaque predicates (+Z3), MBA (constant fold + identity table), instruction substitution, dead code, BCF, anti-disassembly, VM boundary |
| `microcode-guide.md` | Cách đọc/ghi microcode Hex-Rays |
| `cff-recovery.md` | Phương pháp emulation/pattern-matching (VB2023, Geri Revay): OBB/DBB heuristics, jump-table mapping, `UC_HOOK_BLOCK` tracing, coverage metrics |
| `string-decryption.md`, `api-hashing.md`, `self-restoring-binary.md` | String decryption (Conti, ADVobfuscator), API hashing (MurmurHash2, LummaC2, Guloader), self-restoring binaries |

**Điểm mạnh (khớp với bằng chứng nghiên cứu ở §4–§6):**
- CFF đọc microcode ở nhiều mức maturity, giả định **nhiều dispatcher**, state variable = tần suất so sánh
  cao nhất (không lọc entropy), rewiring handler + NOP state assignment — đúng kỹ thuật D-810/HexRaysDeob.
- Opaque predicates: Z3 để chứng minh + optimizer ép nhánh — cùng cách tiếp cận với bộ xác minh rule của D-810.
- Emulation-first (an toàn, read-only IDB) — hiếm có trong các plugin LLM RE khác.
- Vòng lặp "read → identify → fix → verify → iterate" + luật cứng chống hỏng microcode (maturity guard,
  `equal_mops(EQ_IGNSIZE)`, mark dirty, redecompile sau mỗi lần đổi) — đúng khuyến nghị CoT/self-correction.

**Khoảng trống được xác định qua nghiên cứu (đối chiếu ở §7):**
1. VM boundary chỉ dừng ở "document + escalate", chưa kể tên công cụ devirtualization cụ thể để agent/human dùng.
2. Chưa có bước symbolic-execution chính thức khi pattern-matching thất bại (bằng chứng ở §5.2 cho thấy
   artifact symexec cải thiện LLM đáng kể).
3. Chưa có checklist CFF dạng "liệt kê OBB/DBB → dựng CFG trung gian → verify số edge" (CoT-style).
4. Chưa có benchmark định lượng cho skill (gợi ý ở §7.5).

---

## 3. Cơ sở lý thuyết: CFF và Mutation Obfuscation

### 3.1 Control Flow Flattening (CFF)

CFF biến cấu trúc rẽ nhánh/lặp gốc thành máy trạng thái switch-case:

- Mỗi **original basic block (OBB)** kết thúc bằng lệnh gán **state variable** một hằng số mới.
- **Dispatcher block** đọc state variable, so sánh với hằng số (hoặc index jump table) rồi nhảy tới handler.
- **Decision basic block (DBB)** tạo nhân tạo khi OBB gốc kết thúc bằng nhánh có điều kiện.
- Hệ quả: hàm trông như một switch phẳng; cấu trúc gốc mất hút. Ví dụ: `encodeAndSaveFiles()` 170 → 453 dòng
  sau `tigress --Transform=Flatten` (Geri Revay, [VB2023](https://www.virusbulletin.com/conference/vb2023/abstracts/dont-flatten-yourself-restoring-malware-with-control-flow-flattening-obfuscation/)).

Quy trình khôi phục 8 bước (Revay VB2023): nhận diện OBB → DBB → dispatcher → state variable →
map state→OBB → tìm next-state từng OBB → tìm state khởi tạo → dựng lại CFG.
Ba cách hiện thực: **pattern matching** (nhanh, vỡ khi kết hợp opaque/MBA/indirect-goto),
**emulation** (nhanh vừa, coverage một phần nhưng đủ luồng chính), **symbolic execution** (đầy đủ nhưng chậm).
Hướng dẫn cộng đồng cùng hệ kỹ thuật: [OLLVM obfuscation từ A-Z (tyrusrc)](https://tyrusrc.github.io/learnfromscratch/learn/topics/mobile/ollvm-obfuscation/),
[cơ chế unflatten trong D-810 (eybisi.run)](https://eybisi.run/Control-Flow-Unflattening/),
[phân tích OLLVM (CFF+BCF+substitution) của liaogang](https://liaogang.github.io/tech/2019/11/03/Deobfuscation-recovering-an-OLLVM-protected-program.html).

Nền tảng học thuật của các nhánh kỹ thuật: *selective symbolic execution* — Sharif et al., *A Generic Approach to Automatic Deobfuscation of Executable Code*, IEEE S&P 2009 ([IEEE](https://ieeexplore.ieee.org/document/5207648)); Yadegari et al. (cùng tựa đề, IEEE S&P 2015 — bit-level taint + slicing, [IEEE](https://ieeexplore.ieee.org/document/7163054)) và *Symbolic Execution of Obfuscated Code*, CCS 2015 ([ACM](https://dl.acm.org/doi/10.1145/2810103.2813663)); Rolles, *Hex-Rays Microcode API vs. Obfuscating Compiler* (2018) — nền tảng của HexRaysDeob ([Hex-Rays blog](https://hex-rays.com/blog/hex-rays-microcode-api-vs-obfuscating-compiler/)). Detector opaque predicate học thuật tiêu biểu: LOOP, CCS 2015 ([github.com/s3team/loop](https://github.com/s3team/loop)).

### 3.2 Mutation Obfuscation (OLLVM-style) — làm rõ thuật ngữ

"Mutation" trong hệ sinh thái obfuscator thường chỉ nhóm biến đổi *tương đương* ở mức biểu thức/lệnh:

- **Opaque predicates** (OLLVM `-bcf`, Tigress): điều kiện luôn true/false như `x*(x-1) % 2 == 0`.
- **Bogus control flow (BCF)**: opaque predicate + block rác/clone block thật.
- **MBA** (mixed boolean-arithmetic, Tigress): thay `x+y` bằng `(x^y) + 2*(x&y)`.
- **Instruction substitution** (OLLVM `-sub`, Hikari `-enable-mutation`, ADVobfuscator): thay phép toán
  đơn giản bằng chuỗi lệnh tương đương phức tạp.
- So với CFF: không phá cấu trúc CFG mà phá *biểu thức* → deobfuscation cần đại số
  (pattern + trình chứng minh SMT/Z3) thay vì graph rewriting.

**Trọng điểm từ nghiên cứu:** khi CFF + mutation kết hợp, cả LLM lẫn công cụ pattern-match truyền thống
đều suy giảm mạnh — đây là giới hạn chung ngành (xem §5.2, §5.3).

---

## 4. Công cụ & kỹ thuật deobfuscation truyền thống (non-AI)

> Nguồn: báo cáo nghiên cứu chuyên sâu (subagent, 10/2026) dựa trên GitHub/GitLab API, arxiv API, README và hex-rays.com; số sao/dấu thời gian sẽ trôi theo thời gian. ⚠ = chưa kiểm chứng trực tiếp.

### 4.1 Nhóm Hex-Rays microcode (gần nhất với kiến trúc Luc Nhan)

| Công cụ | Kỹ thuật | Trạng thái / license | URL |
|---|---|---|---|
| **HexRaysDeob** (RolfRolles) | Pattern rewrite microcode: `FlatteningDeobfuscator` (CFF), `MBADeobfuscator` (MBA), opaque patterns; bài *Hex-Rays Microcode API vs. Obfuscating Compiler* (2018) là tutorial chuẩn | C++, cần Hex-Rays SDK; ngừng bảo trì 2021; GPL-3.0 | [github.com/RolfRolles/HexRaysDeob](https://github.com/RolfRolles/HexRaysDeob) · [hex-rays.com/blog/hex-rays-microcode-api-vs-obfuscating-compiler/](https://hex-rays.com/blog/hex-rays-microcode-api-vs-obfuscating-compiler/) |
| **D-810** (m417z → eShard) | Rule engine trên microcode qua **IDAPython**: constant folding, opaque (setz/setnz), MBA rules, **Z3**, block merging, CFF unflatteners; JSON config + GUI | LGPL-3.0; GitLab active tới 2025-05 | [gitlab.com/eshard/d810](https://gitlab.com/eshard/d810) · mirror [github.com/zhkl0228/d810](https://github.com/zhkl0228/d810) |
| **d810-ng** (w00tzenheimer/eShard) | Kế thừa D-810 cho IDA 9.x: unflattener cho **OLLVM, Tigress switch/indirect, Hodur (PlugX), Approov**, fake jumps; MBA DSL verify bằng Z3; back-end **CoBRA (symbolic exec)**; test suite headless (idalib); phát hành PyPI | Active 2025–2026 — thành viên "sống nhất" họ D-810; LGPL-3.0/AGPL ⚠ (kiểm tra trước khi nhúng) | [github.com/w00tzenheimer/d810-ng](https://github.com/w00tzenheimer/d810-ng) · [pypi.org/project/d810-ng](https://pypi.org/project/d810-ng) |
| **MicroSMT** (eversinc33) | Lift microcode → **Z3**: giải opaque predicates / identity số học | Mới 2026, pure Python | [github.com/eversinc33/MicroSMT](https://github.com/eversinc33/MicroSMT) |
| **obpo-plugin** (obpo-project) | CFF deobfuscator *cloud*: đánh dấu dispatcher → server Go chạy core đóng (dataflow + slicing + concolic) → trả patch; x86/x64/ARM(64)/PPC/MIPS | Core đóng, archived 2023; ⚠ gửi code ra server thứ ba (quyền riêng tư khi phân tích malware) | [github.com/obpo-project/obpo-plugin](https://github.com/obpo-project/obpo-plugin) · [obpo-server](https://github.com/obpo-project/obpo-server) |
| **xdeobf** (teapotd), **jgajek/cff-deobfuscator** | CFF: tìm dispatcher/state var → dựng lại CFG | C++ (2022) / IDAPython (2026) — tham khảo | [github.com/teapotd/xdeobf](https://github.com/teapotd/xdeobf) · [github.com/jgajek/cff-deobfuscator](https://github.com/jgajek/cff-deobfuscator) |
| Helper: **lucid** (gaasedelen), **genmc** (patois), **HexRaysPyTools** (igogo-x86) | Explorer/dump microcode, hỗ trợ Hex-Rays workflow | Không phải deobfuscator nhưng giúp agent "nhìn" microcode | [github.com/gaasedelen/lucid](https://github.com/gaasedelen/lucid) · [github.com/patois/genmc](https://github.com/patois/genmc) · [github.com/igogo-x86/HexRaysPyTools](https://github.com/igogo-x86/HexRaysPyTools) |

### 4.2 Bộ OLLVM deobfuscation & CFF scripts

| Công cụ | Kỹ thuật | URL |
|---|---|---|
| **RPISEC/llvm-deobfuscator** | Đảo pass CFF OLLVM bằng Binary Ninja SSA (tìm def state var, dựng lại edge); wiki kèm xử lý bogus CF thủ công | [github.com/RPISEC/llvm-deobfuscator](https://github.com/RPISEC/llvm-deobfuscator) |
| **cq674350529/deflat** | Script **angr** kinh điển: symexec lấy state mapping, patch nhị phân; headless-scriptable | [github.com/cq674350529/deflat](https://github.com/cq674350529/deflat) |
| **MODeflattener** (mrT4ntr4) / **ollvm-unflattener** (cdong1012) | Miasm (symbolic exec) phục hồi CFG OLLVM; MODeflattener dùng heuristic Blazytko để *phát hiện* hàm flattened | [MODeflattener](https://github.com/mrT4ntr4/MODeflattener) · [cdong1012](https://github.com/cdong1012/ollvm-unflattener) · [blog](https://mrt4ntr4.github.io/MODeflattener/) |
| **maskelihileci/IDA_Ollvm-unflattener** | Plugin IDA + Miasm (2025) | [github.com/maskelihileci/IDA_Ollvm-unflattener](https://github.com/maskelihileci/IDA_Ollvm-unflattener) |
| **eset/stadeo** | ESET: IDA + Miasm (dataflow/symexec + **reassemble**) xử lý CFF + string obfuscation Stantinko/Emotet — ví dụ hoàn chỉnh pipeline "unflatten → patch" | [github.com/eset/stadeo](https://github.com/eset/stadeo) |
| **OLLVM_Deobfuscation** (453483289 / neil-wu) | angr + Z3, x86/Linux OLLVM | [github.com/neil-wu/OLLVM_Deobfuscation](https://github.com/neil-wu/OLLVM_Deobfuscation) |
| Biến thể BN: amimo/ollvm-breaker, MikuCffHelper (active 2024+), zerotistic "Breaking CFF" (BN + Z3); IDA: GeT1t/DeOllvm | CFF removal | [amimo](https://github.com/amimo/ollvm-breaker) · [MikuCffHelper](https://github.com/ltlly/MikuCffHelper) · [zerotistic blog](https://zerotistic.blog/posts/cff-remover/) |

### 4.3 VMProtect & devirtualization

| Công cụ | Kỹ thuật | URL |
|---|---|---|
| **NoVmp** (can1357) | Static devirt VMProtect x64 3.0–3.5: tìm VMENTER → lift **VTIL** → optimize → (tùy chọn) compile lại x64 vào section `.novmp`; CLI → script được qua subprocess, reload PE vào IDA | [github.com/can1357/NoVmp](https://github.com/can1357/NoVmp) |
| **VTIL-Core** (vtil-project) | IR + compiler framework cho devirtualization (nền của NoVmp) | [github.com/vtil-project/VTIL-Core](https://github.com/vtil-project/VTIL-Core) |
| **Titan** (archercreat) — *"Tirant" trong câu hỏi hầu như chắc chắn là tool này* | Devirt VMP (<3.8) bằng **Triton** (emulation + symexec + AST pattern handler) → **LLVM IR** + custom passes (no-alias, memory coalescing); output bitcode "educational" | [github.com/archercreat/titan](https://github.com/archercreat/titan) |
| **VMProtect-devirtualization** (JonathanSalwan) | Động: symexec Triton + LLVM khôi phục *hàm thuần*; gốc từ chiến thắng Tigress virtualize (DIMVA 2018) | [github.com/JonathanSalwan/VMProtect-devirtualization](https://github.com/JonathanSalwan/VMProtect-devirtualization) |
| **VMUnprotect**; **TicklingVMProtect** (gavz, 2026 ⚠) | Hướng động; pipeline mới đáng theo dõi | [gist assarbad (tổng hợp)](https://gist.github.com/assarbad/83e7def48a986727b12fcc644da1aa57) · [TicklingVMProtect](https://github.com/gavz/TicklingVMProtect) |
| Giáo dục: hackyboiz (LLVM-based VMP devirt, 2025), nac-l (lifting Themida/VMP, 2025) | Pipeline devirt từng bước | [hackyboiz](https://hackyboiz.github.io/2025/09/11/banda/LLVM_based_VMP/en/) · [nac-l](https://nac-l.github.io/2025/01/25/lifting_0.html) |

### 4.4 SMT & program synthesis

| Công cụ | Kỹ thuật | URL |
|---|---|---|
| **SATURN** (Zimperium) — *không phải USENIX 2021: SPRO 2019; mã nguồn chưa từng công bố* | Binary → LLVM IR (kiểu McSema/Remill) → dựng lại CFG lặp → **LLVM opt + Souper (SMT superoptimizer)** xóa constant unfolding / MBA / opaque / BCF → biên dịch lại | [arxiv.org/abs/1909.01752](https://arxiv.org/abs/1909.01752) · [zimperium.com/blog/saturn-software-deobfuscation-framework-based-on-llvm](https://zimperium.com/blog/saturn-software-deobfuscation-framework-based-on-llvm) |
| **msynth** (mrphrazer / Tim Blazytko) | **MBA simplification**: AST rewriting dựa oracle + *stochastic program synthesis* học biểu thức ngắn tương đương; CLI + Python; active 2026 | [github.com/mrphrazer/msynth](https://github.com/mrphrazer/msynth) · [synthesis.to](https://synthesis.to) |
| **MBA-Blast** (NirIzr → softsec-unh) | IDA plugin MBA simplification: pattern + synthesis/bruteforce oracle | [github.com/softsec-unh/MBA-Blast](https://github.com/softsec-unh/MBA-Blast) |
| **LOOP** (s3team, CCS 2015) | Detector opaque predicate học thuật hay trích dẫn nhất | [github.com/s3team/loop](https://github.com/s3team/loop) |
| DROP-IDA-plugin (Keysight), OpaquePredicatePatcher (BN, active 2026), native-predicate-solver (BN) | Detection/cleanup opaque predicates | [DROP](https://github.com/Keysight/DROP-IDA-plugin) · [OpaquePredicatePatcher](https://github.com/Vector35/OpaquePredicatePatcher) |

### 4.5 Framework & decompiler độc lập

| Công cụ | Dùng cho | URL |
|---|---|---|
| **angr** | Symexec/concolic (deflat...); Claripy/Z3 | [github.com/angr/angr](https://github.com/angr/angr) |
| **Triton** | DSE + taint + AST; nền của Titan/Ponce/X-Tunnel | [github.com/JonathanSalwan/Triton](https://github.com/JonathanSalwan/Triton) |
| **Miasm** | Disasm/IR/symexec; nền deflattener, stadeo | [github.com/cea-sec/miasm](https://github.com/cea-sec/miasm) |
| **RetDec** | Decompiler LLVM-based headless; "ý kiến thứ hai" + xử lý SATURN-style | [github.com/avast/retdec](https://github.com/avast/retdec) |
| **Ponce** (IDA + Triton), **X-Tunnel-Opaque-Predicates**, TritonDSE | Symexec/taint ngay trong Hex-Rays; ví dụ DSE để lộ opaque predicates | [Ponce](https://github.com/illera88/Ponce) · [X-Tunnel](https://github.com/JonathanSalwan/X-Tunnel-Opaque-Predicates) · [TritonDSE](https://github.com/quarkslab/tritondse) |
| **Pharos** (CMU SEI) | Không phải deobfuscator; hạ tầng CFG/dataflow/hashing cho detection & triage hàng loạt | [github.com/cmu-sei/pharos](https://github.com/cmu-sei/pharos) |

### 4.6 Ánh xạ kỹ thuật → agent hóa trong Luc Nhan

| Kỹ thuật | Công cụ tiêu biểu | Cách Luc Nhan tái hiện/vay mượn |
|---|---|---|
| Rewriting microcode | HexRaysDeob, D-810, **d810-ng**, MicroSMT | Đã có sẵn `install_microcode_optimizer`/`nop_microcode` — d810-ng dùng đúng API IDAPython microcode (IDA ≥ 7.5); port rule MBA + Z3-verify (d810-ng / MicroSMT) là khả thi nhất |
| Emulation trace | flare-emu, Unicorn | Đã có: `emulate_code`, `UC_HOOK_BLOCK` qua `execute_python` (`cff-recovery.md`) |
| Pattern matching CFG | ollvm-unflattener, MODeflattener, RPISEC | `get_microcode` MMAT_LOCOPT/PREOPTIMIZED + heuristic OBB/DBB (đã có) |
| SMT/Z3 | SATURN (Souper), D-810 rules, MicroSMT | `execute_python` + z3 (đã có trong `algorithm-reference.md`) |
| MBA synthesis | msynth, MBA-Blast | `execute_python` gọi msynth (oracle AST) qua CLI/import — bổ sung "oracle xác minh" (§7.2) |
| Symbolic execution | angr, Triton, Miasm | `execute_python` + angr/Triton — thiếu bước "fallback chuẩn" (§7.4) |
| Devirtualization VM | NoVmp/VTIL, Titan, Salwan, VMUnprotect | Ngoài phạm vi IL; chạy ngoài tiến trình → reload (`NoVmp` sinh `.novmp`) (§7.1) |

### 4.7 Xếp hạng khả năng scriptable cho agent

1. **d810-ng** — Python, import được (`import d810`), config-driven, đang active → khuyến nghị vay rule số 1.
2. **Toolset microcode của Luc Nhan** (get_microcode / nop_microcode / install_microcode_optimizer / redecompile) — đã có, đúng tầng mà HexRaysDeob/D-810 sửa.
3. **MicroSMT** (IDA + Z3, Python) — opaque predicates.
4. **MBA-Blast / msynth** (Python; msynth thêm CLI out-of-process) — MBA.
5. Helper: lucid, genmc, HexRaysPyTools.
6. Ngoài tiến trình (headless subprocess): deflat (angr), MODeflattener/cdong (Miasm), stadeo, RetDec, NoVmp/VTIL (chỉ VMP), Titan.

### 4.8 Các hiệu chỉnh phát hiện trong quá trình nghiên cứu

- **SATURN** = SPRO 2019 (workshop tại IEEE S&P), *không phải* USENIX 2021; mã nguồn chưa công bố.
- **"Tirant"** → **Titan** (archercreat) hoặc thí nghiệm Salwan; **"SPRING"** không tồn tại (có thể nhầm với d810-ng "next-gen" hoặc MicroSMT).
- **"ollvm-cff-deobfuscator"** → **obpo-plugin**; **"ida-plumber"** không tồn tại.
- nccgroup/**DeObfuscator** và quarkslab/**ida-triton** đã bị gỡ khỏi GitHub; can1357/**VMPattack** 404.
- Cờ `-mllvm -mutation` của OLLVM chưa kiểm chứng được (pass chính thức: `-fla`, `-bcf`, `-sub`, `-split`; Hikari fork bổ sung MBA/string-enc). "Mutation" trong câu hỏi khớp nhất với OLLVM `-sub` + MBA (Tigress `EncodeArithmetic`).

---

## 5. Nghiên cứu AI/LLM cho deobfuscation & decompilation

### 5.1 LLM decompiler (học máy đưa ra mã nguồn cấp cao)

| Paper | Năm / Venue | Ý tưởng & kết quả chính | URL |
|---|---|---|---|
| **LLM4Decompile** (Tan et al.) | EMNLP 2024 | Loạt model 1.3B–33B decompile trực tiếp; **End models vượt GPT-4o và Ghidra >100% về tỷ lệ chạy lại được** (re-executability) trên HumanEval/ExeBench; **Ref models** tinh chỉnh output Ghidra +16.2%. Code/dataset mở | [arxiv.org/abs/2403.05286](https://arxiv.org/abs/2403.05286) · [github.com/albertan017/LLM4Decompile](https://github.com/albertan017/LLM4Decompile) |
| **DeGPT** (Hu et al.) | NDSS 2024 | Framework end-to-end tối ưu output decompiler bằng LLM (hiểu → sửa lặp): cải thiện readability/simplicity | [ndss-symposium.org/ndss-paper/degpt-optimizing-decompiler-output-with-llm/](https://www.ndss-symposium.org/ndss-paper/degpt-optimizing-decompiler-output-with-llm/) · [github.com/PeiweiHu/DeGPT](https://github.com/PeiweiHu/DeGPT) |
| **FidelityGPT** (Zhou et al.) | NDSS 2026 | Phát hiện/sửa "distortion" của decompiler: prompt nhận biết distortion + RAG; **FR 94% vs DeGPT 83%, CFR 64% vs 37%** | [arxiv.org/abs/2510.19615](https://arxiv.org/abs/2510.19615) |
| **ALT4Decompile** (Wang et al.) | 2025 | Dựng **Abstract Loop Tree** từ jump pattern để "C-aligned" hóa assembly trước khi LLM decompile; **70.4% pass Decompile-Eval, vẫn tốt dưới 4 kỹ thuật obfuscation thường dùng** | [arxiv.org/abs/2509.14646](https://arxiv.org/abs/2509.14646) · [github.com/wang-yongpan/ALT4Decompile](https://github.com/wang-yongpan/ALT4Decompile) |
| **Idioms** (Dramko et al.) | 2025 | Neural decompilation kèm **sinh luôn định nghĩa type**; ExeBench 54.4% vs LLM4Decompile 46.3%, Nova 37.5% | [arxiv.org/abs/2502.04536](https://arxiv.org/abs/2502.04536) |
| **MCGD multi-agent** (Zhang et al.) | 2026 | Pipeline 3 tầng ràng buộc (cú pháp → compilable → tương đương hành vi bằng test LLM sinh); agent con sửa theo feedback lỗi; **84–97% re-executability trên 1.641 binary ExeBench** ($0.03–0.05/binary) | [arxiv.org/abs/2604.23940](https://arxiv.org/abs/2604.23940) |
| **How Far We've Gone…** (Liu et al.) | USENIX Security 2024 | Đánh giá LLM trên các tác vụ hiểu binary code: trên random nhưng chưa đạt "hiểu thực sự" | [github.com/SysZhang/HowFarWeveGone](https://github.com/SysZhang/HowFarWeveGone) |

### 5.2 LLM + deobfuscation (CFF, opaque predicates, mutation) — bằng chứng trực tiếp

| Paper | Năm | Phát hiện chính | URL |
|---|---|---|---|
| **Deconstructing Obfuscation** (Tkachenko et al.) | 2025 | Đánh giá 7 LLM thương mại trên 4 kịch bản (BCF, instruction substitution, CFF, kết hợp): mô hình **3 bậc kháng cự — BCF thấp, CFF trung bình, substitution/kết hợp cao**; 5 lỗi hệ thống (predicate, structural mapping, control-flow, arithmetic transformation, constant propagation); **thất bại toàn diện khi kết hợp kỹ thuật**; đề xuất cộng tác người–AI | [arxiv.org/abs/2505.19887](https://arxiv.org/abs/2505.19887) |
| **CoT cho control-flow deobfuscation** (Mohseni et al.) | 2026 | CoT prompting trên CFF + opaque predicates (+ combo), 5 model SOTA: **+16% dựng lại CFG, +20.5% bảo toàn ngữ nghĩa** vs zero-shot; GPT-5 mạnh nhất; độ khó phụ thuộc cả vào cấu trúc CFG gốc | [arxiv.org/abs/2604.15390](https://arxiv.org/abs/2604.15390) |
| **Can LLMs Recover Program Semantics?** (Feng & Saha) | 2025 | Fine-tune với **artifact KLEE** (SMT constraints, path statistics, test cases) trên CFF/opaque/arithmetic/branch encoding (TUM Obfuscation Benchmarks…): **cải thiện ổn định semantic fidelity + tỷ lệ compile**; GPT-4.1-mini mạnh nhất trong 3 model | [arxiv.org/abs/2511.19130](https://arxiv.org/abs/2511.19130) |
| **BinDeObfBench** (Hu et al.) | 2026 | Benchmark đầu tiên LLM deobfuscate binary (biến đổi pre/compile/post-compilation): **khả năng lý luận > kích thước model**; fine-tune theo tác vụ > pre-train rộng; reasoning models bền vững qua ISA/mức tối ưu; ICL chỉ giúp model thường | [arxiv.org/abs/2604.08083](https://arxiv.org/abs/2604.08083) |
| **ReSource** (Zhou et al.) | 2026 | Framework đa pha "transformation-agnostic": tách lexical/syntactic/semantic; **CFF được xử lý bằng predictor nhẹ dựng "structural skeleton" trước khi LLM sinh mã**; 83% Top-5 trên 80k cặp, 4 kỹ thuật obfuscation | [arxiv.org/abs/2608.27889](https://arxiv.org/abs/2608.27889) |
| **Robustness of Code LLMs on Protected Code** (Wen et al., ISSRE 2026) | 2026 | Trên 7 model, 5 phương pháp bảo vệ: **inference trực tiếp trên code bị che thường ≥ inference trên bản đã deobfuscate** (model mạnh giữ ~90% Pass@1); deobfuscation như bước tiền xử lý không phải lúc nào cũng cần | [arxiv.org/abs/2609.04220](https://arxiv.org/abs/2609.04220) |
| **Emotet payload deobfuscation** (Patsakis et al.) | 2024 | 4 LLM trên payload Emotet thật: một số LLM deobfuscate hiệu quả; fine-tune là hướng khả thi cho pipeline threat-intel | [arxiv.org/abs/2404.19715](https://arxiv.org/abs/2404.19715) |
| **JsDeObsBench** (Chen et al., CCS 2025) | 2025 | Benchmark JS deobfuscation (họ kỹ thuật rộng): LLM mạnh về simplification, yếu về syntax/execution reliability — bài học chuyển được sang binary | [arxiv.org/abs/2506.20170](https://arxiv.org/abs/2506.20170) |

### 5.3 Dữ liệu & benchmark để đánh giá

- **Decompile-Bench**: 2 triệu cặp binary–source (450 GB) từ GitHub permissive + bộ đánh giá chống data-leak; fine-tune bằng nó +20% re-executability | [arxiv.org/abs/2505.12668](https://arxiv.org/abs/2505.12668)
- **ExeBench** (dùng trong LLM4Decompile/Idioms/MCGD): hàm thật từ binary thế giới thực
- **TUM Obfuscation Benchmarks** (dùng trong [2511.19130](https://arxiv.org/abs/2511.19130))

### 5.4 Kết luận cho thiết kế skill

1. LLM không nên "sửa code" trực tiếp kiểu tự do; nên **sinh optimizer/script có kiểm tra** hoặc **liệt kê ánh xạ trước khi sửa** (kết quả CoT + ReSource + DeGPT).
2. **Obfuscation kết hợp là giới hạn công bố** — skill cần phát hiện "combo" và bàn giao người dùng (§7.6).
3. **Verify bằng thực thi/semantic** (emulate trước–sau, so output) mạnh hơn verify bằng mắt — khớp với `emulate_code` sẵn có.
4. Model có lý luận (reasoning) + fine-tune tác vụ hiện là tốt nhất — khi triển khai provider mới, ưu tiên reasoning model (Luc Nhan đã hỗ trợ nhiều provider).

---

## 6. Hệ sinh thái AI agent "skill" cho Reverse Engineering

### 6.1 Chuẩn skill & conventions của các nền tảng

| Nền tảng | Định dạng skill/tool | Ghi chú |
|---|---|---|
| **Anthropic Agent Skills** | Thư mục skill chứa `SKILL.md` (YAML frontmatter: name, description + thân markdown) + thư mục `references/` nạp theo nhu cầu; mô tả dùng để router tự chọn skill | [platform.claude.com/docs/en/agents-skills/overview](https://platform.claude.com/docs/en/agents-skills/overview) |
| **OpenAI Codex** | Convention `AGENTS.md` đặt hướng dẫn repo cho agent | [github.com/openai/codex](https://github.com/openai/codex) |
| **MCP (Model Context Protocol)** | Server tools chuẩn hóa cho agent (cách Luc Nhan bridge `mcp_<server>_<tool>`) | [modelcontextprotocol.io](https://modelcontextprotocol.io/) |

**Thiết kế skill Luc Nhan (SKILL.md + frontmatter + references + allowed_tools) khớp chuẩn Anthropic Agent Skills**
— không cần đổi kiến trúc; chỉ cần cải tiến nội dung.

### 6.2 MCP servers cho RE (so sánh với tool surface Luc Nhan)

| Server | Bề mặt tool | URL |
|---|---|---|
| **IDA MCP server chính thức (Hex-Rays)** | Miễn phí, open source, model-agnostic; "Code Mode" tiết kiệm ~20% token; kết nối IDA với agent | [hex-rays.com/blog/hex-rays-ida-mcp-server](https://hex-rays.com/blog/hex-rays-ida-mcp-server) |
| **GhidraMCP** (LaurieWired) | Decompile, disassemble, rename/comment… cho LLM điều khiển Ghidra | [github.com/LaurieWired/GhidraMCP](https://github.com/LaurieWired/GhidraMCP) |
| **ghidra-mcp** (bethington) | 209 tools: p-code emulation, debugger live, binary diffing… | [github.com/bethington/ghidra-mcp](https://github.com/bethington/ghidra-mcp) |
| **Binary Ninja MCP** | MCP server chính thức của Binary Ninja | [docs.binary.ninja/guide/mcp.html](https://docs.binary.ninja/guide/mcp.html) |
| **ida-mcp → re-mcp** (jtsylve) | v3.0: chung interface tool cho cả IDA Pro và Ghidra | [jtsylve.blog/post/2026/05/04/ida-mcp-becomes-re-mcp](https://jtsylve.blog/post/2026/05/04/ida-mcp-becomes-re-mcp) |
| So sánh tổng quan | REA vs IDA MCP vs GhidraMCP vs Binary Ninja MCP (10/2026) | [dev.to/leemie8848/ai-reverse-engineering-tools-compared-rea-ida-mcp-ghidramcp-and-friends-oct-2026-3hfl](https://dev.to/leemie8848/ai-reverse-engineering-tools-compared-rea-ida-mcp-ghidramcp-and-friends-oct-2026-3hfl) |

→ Luc Nhan **đã có MCP client** — có thể tiêu thụ các server này để cross-check (ví dụ xem P-code của Ghidra khi Hex-Rays bó tay) mà không cần tự viết plugin.

### 6.3 Plugin/agent LLM cho RE

- **Rikugan / Luc Nhan** (repo này): agent-loop riêng, 80+ tool IDA, subagent song song, skills, MCP client, headless — là một trong những hệ agent RE đầy đủ nhất; so sánh thêm ở bài dev.to ở trên.
- **Gepetto** (Bernd Herzog): plugin IDA phổ biến gọi LLM để *đặt tên & chú thích* hàm — bề mặt tool hẹp hơn nhiều so với Luc Nhan | [github.com/bernd-herzog/gepetto](https://github.com/bernd-herzog/gepetto)
- **Agentic Malware Analysis** (mrphrazer): framework modular cho Claude/Codex + MCP (Binary Ninja, Ghidra): gọi tool phân tích, chạy mẫu/emulator, ghi nhớ có cấu trúc | [github.com/mrphrazer/agentic_malware_analysis](https://github.com/mrphrazer/agentic_malware_analysis)
- **reverse-skill** (zhaoxuya520): ví dụ repo "skill" dạy RE, có hẳn phần tích hợp idalib-mcp/ghidra-mcp — tham khảo cấu trúc tài liệu | [deepwiki.com/zhaoxuya520/reverse-skill](https://deepwiki.com/zhaoxuya520/reverse-skill/7.2-ida-pro-ghidra-and-binary-analysis-mcp)

### 6.4 AIxCC và hệ agent bảo mật (bài học thiết kế)

- **AIxCC finals (DEF CON 33, 08/2025)**: Team Atlanta (Georgia Tech + Samsung Research + KAIST + POSTECH) vô địch với **ATLANTIS** ($4M); Trail of Bits **Buttercup** hạng nhì ($3M); Theori hạng ba ($1.5M) — [DARPA announcement](https://www.darpa.mil/news/2025/aixcc-results) · [aicyberchallenge.com](https://aicyberchallenge.com/Finals-winners-announcement/)
- **ATLANTIS**: cyber reasoning system dùng LLM điều phối công cụ (fuzzing, sanitizer feedback, patch) tìm/patch lỗi thật — [arxiv.org/abs/2509.14589](https://arxiv.org/abs/2509.14589) · [team blog](https://team-atlanta.github.io/blog/post-afc/)
- Bài học: thành công đến từ **harness + tool-use + vòng phản hồi thực thi** (sanitizer) chứ không phải LLM "nhìn" code; tương tự, deobfuscation agent nên có **oracle thực thi** (emulate/symexec) thay vì chỉ đọc-giả-định.

### 6.5 Recipes cộng đồng "LLM/skill deobfuscate"

- OLLVM deobfuscation A-Z: nhận diện CFF/BCF/substitution; pattern deflatten, Triton/angr symexec, scripted IDA/Ghidra passes — [tyrusrc.github.io](https://tyrusrc.github.io/learnfromscratch/learn/topics/mobile/ollvm-obfuscation/)
- Khôi phục chương trình OLLVM: phân tích từng lớp bảo vệ và ghép công cụ — [liaogang.github.io](https://liaogang.github.io/tech/2019/11/03/Deobfuscation-recovering-an-OLLVM-protected-program.html)
- Unflatten với D-810: giải thích "dispatcher father" và state variable — [eybisi.run](https://eybisi.run/Control-Flow-Unflattening/)
- Deflattener OLLVM bằng Miasm + heuristic Blazytko — [MODeflattener](https://mrt4ntr4.github.io/MODeflattener/)

### 6.6 Best-practice viết skill cho tác vụ phân tích dài

1. **Progressive disclosure**: SKILL.md ngắn gọn, chi tiết để trong `references/` nạp theo bước (Luc Nhan đã làm).
2. **Checklist bắt buộc** cho tác vụ nhiều bước (agent tự chấm điểm sau mỗi bước).
3. **Vòng verify** gắn với thực thi (redecompile, emulate, so sánh output trước/sau) — mạnh hơn tự đánh giá mắt thường.
4. **Danh sách "không được làm"** (luật cứng) — giảm hallucination (Luc Nhan đã có maturity guard…).
5. **Failure modes công bố** cần đưa vào skill để agent tự nhận diện: predicate misinterpretation, structural mapping errors, control flow misinterpretation, arithmetic transformation errors, constant propagation errors ([2505.19887](https://arxiv.org/abs/2505.19887)).
6. **Bàn giao (handoff)** khi vượt năng lực công bố: combined obfuscation, VM-protected, self-restoring.

---

## 7. Đối chiếu & khuyến nghị nâng cấp skill Luc Nhan

Ánh xạ từng gợi ý vào file hiện có để dễ triển khai:

### 7.1 VM Boundary → nêu tên công cụ cụ thể (sửa `SKILL.md` §3 + `algorithm-reference.md` §VM)
Hiện chỉ ghi "Miasm, Triton" chung chung. Thay bằng ma trận:
- **VMProtect 3.x**: Titan (Triton+LLVM) khi cần devirt hàm cụ thể; NoVmp (VTIL) khi muốn lift + tối ưu toàn cục; VMUnprotect khi cần hướng động.
- **Themida/WinLicense**: ghi chú sang hướng động (giới hạn công bố); không hứa hẹn devirt tĩnh.
- **Tigress-virtualized**: tham chiếu hướng tiếp cận Triton+LLVM của [JonathanSalwan](https://github.com/JonathanSalwan/VMProtect-devirtualization) và paper DIMVA 2018.
- Quy tắc: nếu mẫu là `pushfq/popfq` → self-restoring workflow (đã có), không nhầm thành VM.
- Khi có điều kiện chạy ngoài tiến trình: vòng lặp **devirt ngoài → reload** — NoVmp sinh PE `.novmp` (mở lại vào IDA), Titan xuất LLVM bitcode; agent mô tả luồng này như "external tool loop" (chạy CLI → nạp lại file → phân tích tiếp), tương thích undo/patch tracking của Luc Nhan.

### 7.2 Z3 như *oracle* xác minh trước khi sửa (bổ sung `algorithm-reference.md`)
D-810 đã chứng minh pattern rule + Z3-verify là bền. Chuẩn hóa thành idiom trong skill:
- MBA: trước khi thay thế, `prove(expr == simplified)` với z3 BitVec (đã có sketch, nâng thành bắt buộc).
- Opaque predicate: `solver.check(Not(pred)) == unsat` → mới ép nhánh.
- Instruction substitution: sau khi sinh dãy thay thế, verify tương đương với bản gốc.
- Hiện thực tham chiếu để vay rule: **d810-ng** (MBA DSL, rule Z3-verified — import được trong IDAPython) và **MicroSMT** (lift microcode → Z3); MBA nặng thì dùng msynth/MBA-Blast qua `execute_python` ([d810-ng](https://github.com/w00tzenheimer/d810-ng) · [MicroSMT](https://github.com/eversinc33/MicroSMT) · [msynth](https://github.com/mrphrazer/msynth)).

### 7.3 Checklist CFF CoT-style (bổ sung `SKILL.md` §2 hoặc `cff-recovery.md`)
Thêm bước bắt buộc trước khi rewiring (kết quả CoT paper + ReSource):
1. Liệt kê **tất cả** OBB/DBB/dispatcher từ microcode (2 mức maturity).
2. Dựng bảng **state → handler** (kể cả nhánh true/false của DBB).
3. Trình bày CFG trung gian trước khi sửa (agent "nói ra" ánh xạ — giảm structural mapping errors).
4. Rewire → NOP state assignment → **đếm lại edge** và so với bảng ở bước 2 (verify số lượng, không chỉ cảm nhận).
5. **(Nghiên cứu công cụ)** Workflow chuẩn cho một hàm CFF: (1) `get_microcode` ở nhiều mức MMAT để xác nhận dispatcher/state var; (2) thử rule d810-ng hoặc tự cài `optblock_t`/`optinsn_t` unflattener; (3) `redecompile_function` + diff; (4) opaque/MBA còn sót → rule Z3 (MicroSMT/D-810) hoặc msynth/MBA-Blast; (5) ca cứng đầu → công cụ headless ngoài (deflat/angr, MODeflattener, stadeo); (6) VMP ≥ 3 → rời Hex-Rays, dùng NoVmp/VTIL/Titan rồi nạp lại file.

### 7.4 Fallback symbolic execution chính thức (bổ sung `tools.md` + `guide.md`)
- Khi pattern + emulation thất bại (indirect-goto dispatcher, combo opaque+CFF): dùng angr/Triton qua `execute_python`.
- Bằng chứng [2511.19130](https://arxiv.org/abs/2511.19130): artifact symexec (SMT constraints, path stats, test cases) cải thiện semantic fidelity — agent nên *đưa ràng buộc SMT thu được vào prompt* khi giải thích cho LLM.
- Cảnh báo chi phí (Revay VB2023): không dùng trong IR gấp; thêm vào bảng "Choosing the Right Approach".

### 7.5 Benchmark đánh giá skill (mới: `tests/deobfuscation/` hoặc docs)
- Sinh mẫu: biên dịch các hàm C đơn giản với **OLLVM** (`-fla`, `-bcf`, `-sub`, tổ hợp) và **Tigress** (`Flatten`, `OpaquePredicates`, `MBA`).
- Tiêu chí: (a) cấu trúc — F1 trên CFG phục hồi vs bản gốc; (b) ngữ nghĩa — `emulate_code` output trước/sau có tương đương không; (c) readability — diff mã sau redecompile + đếm state-assignment còn sót.
- Tham chiếu dataset công khai: Decompile-Bench, ExeBench, TUM Obfuscation Benchmarks.

### 7.6 Giới hạn công bố → bàn giao sớm (bổ sung `SKILL.md` Critical Rules)
- **Combined obfuscation** (≥2 kỹ thuật): cả LLM lẫn công cụ đều suy giảm mạnh ([2505.19887](https://arxiv.org/abs/2505.19887), [2609.04220](https://arxiv.org/abs/2609.04220)) → agent phát hiện combo thì chuyển sang chế độ "lập hồ sơ + chia nhỏ + gợi ý human", không cố deobfuscate toàn phần.
- **Không cần deobfuscate mù quáng**: [2609.04220](https://arxiv.org/abs/2609.04220) cho thấy model mạnh thường hiểu được code bị che trực tiếp → thử `decompile_function` + phân tích trước, chỉ deobfuscate khi thực sự cần.

### 7.7 Tận dụng MCP client (kiến trúc, không sửa skill)
- Luc Nhan đã có MCP client: bridge [Hex-Rays IDA MCP server](https://hex-rays.com/blog/hex-rays-ida-mcp-server) hoặc [GhidraMCP](https://github.com/LaurieWired/GhidraMCP) để cross-check P-code khi Hex-Rays thất bại — ví dụ "decompile bằng Ghidra + LLM4Decompile-style" khi nghi ngờ decompiler sai.

### 7.8 Từ khóa trigger & mô tả skill
- Bổ sung trigger: `deflatten`, `devirtualize`, `VMProtect`, `Titan`, `NoVmp`, `SATURN`, `OLLVM-CFF`… và ghi rõ phạm vi devirtualization (= phát hiện + chuyển tiếp, không tự devirt).

---

## 8. Danh mục tài liệu tham khảo

**Nội bộ repo**
- [SKILL.md](../lucnhan/skills/builtins/deobfuscation/SKILL.md), [tools.md](../lucnhan/skills/builtins/deobfuscation/references/ida/tools.md), [guide.md](../lucnhan/skills/builtins/deobfuscation/references/ida/guide.md), [algorithm-reference.md](../lucnhan/skills/builtins/deobfuscation/references/ida/algorithm-reference.md), [cff-recovery.md](../lucnhan/skills/builtins/deobfuscation/references/ida/cff-recovery.md), [microcode-guide.md](../lucnhan/skills/builtins/deobfuscation/references/ida/microcode-guide.md)

**Công cụ truyền thống**
- https://github.com/RolfRolles/HexRaysDeob · https://github.com/IDAPluginProject/d810 · https://github.com/w00tzenheimer/d810-ng · https://pypi.org/project/d810-ng
- https://github.com/archercreat/titan · https://github.com/can1357/NoVmp · https://github.com/JonathanSalwan/VMProtect-devirtualization · https://gist.github.com/assarbad/83e7def48a986727b12fcc644da1aa57
- https://github.com/cdong1012/ollvm-unflattener · https://github.com/mrT4ntr4/MODeflattener · https://mrt4ntr4.github.io/MODeflattener/ · https://github.com/neil-wu/OLLVM_Deobfuscation
- https://arxiv.org/abs/1909.01752 (SATURN) · https://dl.acm.org/doi/10.1145/3338503.3357721
- https://tigress.wtf/ · https://github.com/obfuscator-llvm/obfuscator · https://plugins.hex-rays.com/category/deobfuscation
- https://gitlab.com/eshard/d810 · https://github.com/zhkl0228/d810 · https://github.com/w00tzenheimer/d810-ng · https://github.com/eversinc33/MicroSMT · https://github.com/obpo-project/obpo-plugin · https://github.com/teapotd/xdeobf · https://github.com/gaasedelen/lucid · https://github.com/patois/genmc · https://github.com/igogo-x86/HexRaysPyTools
- https://github.com/RPISEC/llvm-deobfuscator · https://github.com/cq674350529/deflat · https://github.com/eset/stadeo · https://github.com/maskelihileci/IDA_Ollvm-unflattener · https://github.com/amimo/ollvm-breaker · https://github.com/ltlly/MikuCffHelper · https://zerotistic.blog/posts/cff-remover/
- https://github.com/vtil-project/VTIL-Core · https://github.com/gavz/TicklingVMProtect · https://hackyboiz.github.io/2025/09/11/banda/LLVM_based_VMP/en/ · https://nac-l.github.io/2025/01/25/lifting_0.html
- https://github.com/mrphrazer/msynth · https://synthesis.to · https://github.com/softsec-unh/MBA-Blast · https://github.com/s3team/loop · https://github.com/Keysight/DROP-IDA-plugin · https://github.com/Vector35/OpaquePredicatePatcher
- https://github.com/illera88/Ponce · https://github.com/JonathanSalwan/X-Tunnel-Opaque-Predicates · https://github.com/quarkslab/tritondse · https://github.com/avast/retdec · https://github.com/cmu-sei/pharos · https://github.com/cea-sec/miasm · https://github.com/angr/angr · https://github.com/JonathanSalwan/Triton
- https://hex-rays.com/blog/hex-rays-microcode-api-vs-obfuscating-compiler/ · https://ieeexplore.ieee.org/document/5207648 (S&P 2009) · https://ieeexplore.ieee.org/document/7163054 (S&P 2015) · https://dl.acm.org/doi/10.1145/2810103.2813663 (CCS 2015) · https://zimperium.com/blog/saturn-software-deobfuscation-framework-based-on-llvm
- https://eybisi.run/Control-Flow-Unflattening/ · https://liaogang.github.io/tech/2019/11/03/Deobfuscation-recovering-an-OLLVM-protected-program.html · https://tyrusrc.github.io/learnfromscratch/learn/topics/mobile/ollvm-obfuscation/

**Nghiên cứu LLM**
- https://arxiv.org/abs/2403.05286 (LLM4Decompile) · https://www.ndss-symposium.org/ndss-paper/degpt-optimizing-decompiler-output-with-llm/ (DeGPT) · https://github.com/PeiweiHu/DeGPT
- https://arxiv.org/abs/2510.19615 (FidelityGPT) · https://arxiv.org/abs/2608.27889 (ReSource) · https://arxiv.org/abs/2509.14646 (ALT4Decompile) · https://arxiv.org/abs/2502.04536 (Idioms) · https://arxiv.org/abs/2604.23940 (MCGD) · https://github.com/SysZhang/HowFarWeveGone
- https://arxiv.org/abs/2505.19887 (Deconstructing Obfuscation) · https://arxiv.org/abs/2604.15390 (CoT CFF deobf) · https://arxiv.org/abs/2511.19130 (LLM + KLEE) · https://arxiv.org/abs/2604.08083 (BinDeObfBench) · https://arxiv.org/abs/2609.04220 (Robustness on protected code) · https://arxiv.org/abs/2404.19715 (Emotet) · https://arxiv.org/abs/2506.20170 (JsDeObsBench)
- https://arxiv.org/abs/2505.12668 (Decompile-Bench)

**Hệ sinh thái skill/agent/MCP**
- https://platform.claude.com/docs/en/agents-skills/overview · https://github.com/openai/codex · https://modelcontextprotocol.io/
- https://hex-rays.com/blog/hex-rays-ida-mcp-server · https://github.com/LaurieWired/GhidraMCP · https://github.com/bethington/ghidra-mcp · https://docs.binary.ninja/guide/mcp.html · https://jtsylve.blog/post/2026/05/04/ida-mcp-becomes-re-mcp · https://dev.to/leemie8848/ai-reverse-engineering-tools-compared-rea-ida-mcp-ghidramcp-and-friends-oct-2026-3hfl
- https://github.com/bernd-herzog/gepetto · https://github.com/mrphrazer/agentic_malware_analysis · https://deepwiki.com/zhaoxuya520/reverse-skill/7.2-ida-pro-ghidra-and-binary-analysis-mcp
- https://www.darpa.mil/news/2025/aixcc-results · https://aicyberchallenge.com/Finals-winners-announcement/ · https://arxiv.org/abs/2509.14589 (ATLANTIS) · https://team-atlanta.github.io/blog/post-afc/
