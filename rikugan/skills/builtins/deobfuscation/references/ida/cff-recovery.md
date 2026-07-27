# Control-Flow Flattening (CFF) recovery (emulation-based)

> **Source**: Trích từ `unicorn-ida-skill` (external). Methodology preserved verbatim. Author attribution in the "Source material" section below.
>
> **Complementary to `algorithm-reference.md` (section "CFF — Control Flow Flattening Removal")** — that doc covers the **microcode block-optimizer** approach (rewire `m_goto`s, NOP state assignments). This doc covers the **emulation / pattern-matching** approach from the Geri Revay VB2023 paper, for cases where static microcode rewriting is insufficient (combined CFF + opaque predicates, indirect-goto dispatchers, or when you want a quick coverage trace instead of full CFG reconstruction).

## How to apply in Luc Nhan

This reference describes standalone / IDAPython workflows. In Luc Nhan:

- **Static pattern matching** (OBB/DBB heuristics, jump-table parsing) → use `decompile_function`, `read_function_disassembly`, or `get_microcode` at `MMAT_PREOPTIMIZED` to identify the state variable and dispatcher blocks. The `NN_cmp` / `NN_jump` IDA-internal constants referenced in the snippets below are for `execute_python` only.
- **BB-level emulation trace** → `emulate_code` does **not** expose a custom `UC_HOOK_BLOCK` callback and does not return a per-BB trace. It returns `instruction_count`, `final_registers`, `writes`, `captures`, and a `status` field (`completed` / `range_exit` / `instruction_limit` / ...). Use it for end-to-end "does this function range reach `stop_address` with these args" questions. For per-BB coverage traces (the emulation approach in the VB2023 paper), fall back to `execute_python` with a manual Unicorn instance and `UC_HOOK_BLOCK`.
- **IDA 9.x caveat**: `idaapi.NN_cmp` / `NN_jump` / `NN_j*` constants were **removed** in IDA 9.x (the `ida_allins` instruction-type namespace was reorganized). Prefer `idc.print_insn_mnem(ea) == "cmp"` / `"jmp"` / `startswith("j")` rather than the numeric `itype` values when writing new `execute_python` snippets — string comparisons are stable across versions.

---

## Source material

Geri Revay (Fortinet), *"Don't Flatten Yourself: Restoring Malware with Control-Flow Flattening Obfuscation"*, Virus Bulletin 2023 London. The paper walks through CFF theory, an 8-step generic recovery process, and three implementation options (pattern matching, emulation, symbolic execution) against a synthetic ransomware (`noobware`) flattened with [Tigress](https://tigress.wtf/).

This is a different workflow from the string-decryption work in `string-decryption.md`. Those workflows extract *data* (decrypted strings) from obfuscated functions. CFF recovery reconstructs *structure* — the original control-flow graph of a flattened function.

## What CFF is and why it matters

**Control-flow flattening** transforms a function's original loops and conditionals into a switch-case state machine. Every original basic block (OBB) ends by writing a state value to a state variable; the dispatcher block reads the state variable, indexes into a jump table, and jumps to the next OBB. A simple `encodeAndSaveFiles()` function went from 170 lines to 453 lines when flattened with `tigress --Transform=Flatten --FlattenDispatch=switch`.

This matters because:

- **It's increasingly common in malware.** Real-world families using CFF include [DoubleZero](https://www.virusbulletin.com/uploads/pdf/conference/vb2022/papers/VB2022-Combating-control-flow-flattening-in-NET-malware.pdf) (Kucherin 2022), HawkEye (ConfuserEx), Emotet ([OALABS](https://research.openanalysis.net/emotet/malware/angr/symbolic%20execution/deobfuscation/research/2022/04/06/emotet_deobfuscation.html)), and Pandora Ransomware (SHA256 `5b56c5d86347e164c6e571c86dbf5b1535eae6b979fede6ed66b01e79ea33b7b`).
- **Static decompilation is degraded.** Every flattened function looks the same — a flat switch statement. The original branching structure is invisible without reconstruction.
- **It is asymmetric.** Cheap for the malware author (off-the-shelf obfuscators, often open-source). Expensive for the analyst. Without preparation, full analysis becomes unfeasible in time-sensitive cases like incident response.

## The 8-step generic recovery process

The paper's framework — apply the steps that the relevant technique supports, in any order; only step 8 (reconstruct the CFG) strictly depends on all the others:

1. **Identify original basic blocks (OBBs).** They contain the original logic.
2. **Identify decision basic blocks (DBBs).** Artificially created when an OBB used to end in a conditional jump.
3. **Identify dispatcher basic blocks.** Implement the state machine — can usually be skipped in the script.
4. **Identify the state variable.** Where the next-state value lives (typically `[rbp-N]`).
5. **Map state values to OBBs.** Build the lookup table (jump table, comparisons, or dynamic execution).
6. **Recover next-state values for each OBB.** Each OBB has 1 (direct jump) or 2 (decision branch) successors.
7. **Find the initial state.** Set in the first BB of the function.
8. **Reconstruct the original CFG.** OBBs are nodes; next-state relationships are edges.

The three implementation options differ in *which* of these steps they automate vs require manual reverse engineering.

## Three implementation options

| Technique | Speed | Coverage | Setup cost | When to use |
|---|---|---|---|---|
| **Pattern matching** (static) | Fast (seconds) | Full *if* heuristics hold | Just IDA + `idaapi.FlowChart` | First try. Works on clean CFF (Tigress, ConfuserEx). Falls apart when CFF combines with opaque predicates, MBA, or indirect-jump dispatchers. |
| **Emulation** (flare-emu / Unicorn / `emulate_code`) | Medium (seconds–minutes) | Partial (~44–51% on noobware, but the partial coverage *is* the main path) | `emulate_code` (built-in) or `execute_python` with Unicorn | **Recommended for time-sensitive IR.** Even 44% OBB coverage reveals what the function actually does (file open → read → encrypt, in the noobware case). Full coverage is the gold standard but not required. |
| **Symbolic execution** (angr) | Slow (minutes–hours) | Full when it terminates | angr + IDA Python integration | Only when pattern matching fails AND you have time to learn the toolchain. **NOT recommended during time-sensitive IR** — the paper explicitly warns: "Symbolic execution ... is more like dark magic than engineering, and its complexity can be as time-consuming as the obfuscation technique we are trying to use it against." |

The paper's most quotable insight for time-sensitive work:

> "If we only reverse engineer these basic blocks [0x1553: Starting the function; 0x1647: Opening a file; 0x16f7: Reading the content; 0x16b2: Encrypting the content], we will have a very good understanding of what this function does. So even though this solution provides only partial results, it can easily be the most time-efficient way to get reasonably good results."

## OBB / DBB pattern-matching heuristics

Distilled from the paper's listings 3 and 4 (Appendix B). Iterate every basic block in `idaapi.FlowChart(func)`, track the last two instructions, and apply these checks. **These snippets run via `execute_python`** — they need the IDA-internal `idaapi` / `idautils` APIs:

```python
# OBB: large BB, ends in fixed jump, second-to-last instruction is `mov imm`.
if instr_count >= 3 and is_mov_imm(second_last_instr) and is_jump_fixed(last_instr):
    block = {
        'type': 'obb',
        'next_state': second_last_instr.Op2.value,   # the immediate being stored
        'bb': bb,
    }
# DBB: small BB (2–3 insns), last is conditional jump, second-to-last is `cmp`.
elif instr_count in [2, 3] and second_last_instr.itype == idaapi.NN_cmp \
        and is_conditional_jump(last_instr):
    succs = bb.succs()
    true_value  = extract_state_var_value(next(succs))   # the imm in the *true* BB's `mov [state_var], <imm>`
    false_value = extract_state_var_value(next(succs))
    block = {
        'type': 'dbb',
        'next_state': [true_value, false_value],
        'bb': bb,
    }
```

`is_jump_fixed` checks for `NN_jump` with operand type `o_far` or `o_near` (unconditional, fixed target). `is_conditional_jump` checks the `NN_j*` family (`jo`, `jno`, `jb`, `jnb`, `jz`, `jnz`, `jbe`, `jnbe`, `js`, `jns`, `jp`, `jnp`, `jl`, `jnl`, `jle`, `jnle`, `jcxz`, `jecz`, `jrcxz`, `jge`).

`extract_state_var_value(bb)` walks `idautils.Heads(bb.start_ea, bb.end_ea)`, looking for a `mov [memory], <immediate>` — the assignment to the state variable in the *successor* BB.

> **IDA 9.x note**: The `NN_cmp` / `NN_jump` / `NN_j*` constants were **removed** in IDA 9.x. Prefer comparing against `idc.print_insn_mnem(ea) == "cmp"` / `"jmp"` / `startswith("j")` rather than the numeric `itype` values, which are more stable across versions.

## Jump-table state → OBB mapping

If the dispatcher uses a jump table, state values index into an array of signed 32-bit offsets:

```python
def get_state_address(jpt_name, state_val):
    jpt_address = idaapi.get_name_ea(idaapi.BADADDR, jpt_name)
    if jpt_address == idaapi.BADADDR:
        return None
    # IMPORTANT: signed offset, not unsigned get_dword.
    jpt_offset = idaapi.as_signed(idaapi.get_dword(jpt_address + (state_val * 4)), 32)
    return jpt_address + jpt_offset
```

The jump table is named `jpt_<dispatcher_addr>` in IDA (e.g., `jpt_140E` in the noobware case). Each entry is a 32-bit signed offset from `jpt_address` to the corresponding OBB. `state_val` is the index; `OBB_addr = jpt_address + signed_offset`.

Two families of dispatcher shapes you'll encounter:

- **Switch dispatcher** (Tigress default, ConfuserEx): clean jump table indexed by state value. The paper's `get_state_address` works directly.
- **Indirect-goto dispatcher** (some Tigress modes, custom CFF): state value is used to compute an indirect jump through runtime-computed addresses. Harder to recover statically — fall back to emulation or symbolic execution.

## BB-level tracing — the key novel pattern

The core technique for the *emulation* approach: record which basic blocks were executed, then derive the run's CFG from the trace.

### Via `emulate_code` (built-in tool, preferred for end-to-end runs)

`emulate_code` does **not** expose per-BB tracing, but it tells you whether the function range reached `stop_address` with given arguments — the first question to ask before investing in a richer trace. Run it with:

- `start_address` = function entry
- `stop_address` = function end (exclusive)
- `registers` = reconstructed function arguments (from calling convention)
- `memory_ranges` = any globals / lookup tables the function reads
- `capture_ranges` = any output buffers you want to read back

Inspect `result.status` (was `stop_address` reached?), `result.instruction_count` (how deep did it go?), and `result.final_registers` / `result.captures`. If the run completes successfully, you know the function's main path; if it exits with `range_exit`, the function branches outside the range and you need the richer trace below. This is the simplest first probe — no custom hook code, no `execute_python` approval round-trip.

### Via `execute_python` + `UC_HOOK_BLOCK` (richer tracing)

For richer tracing (custom per-BB logic, conditional `emu_stop`, register capture at BB boundaries), use `execute_python` to drive a Unicorn instance directly. The engine fires `UC_HOOK_BLOCK` natively at translation-block boundaries — no per-instruction overhead, no FlowChart dependency:

```python
import unicorn

executed = []
uc = unicorn.Uc(unicorn.UC_ARCH_X86, unicorn.UC_MODE_64)
# ... map_segments(uc), set up stack, write args ...

# One-line BB trace.
uc.hook_add(unicorn.UC_HOOK_BLOCK,
            lambda uc, address, size, ud: ud.append(address),
            executed)

uc.emu_start(func_ea, 0, timeout=10 * 60 * 1_000_000)
print(f"Executed {len(executed)} BBs:", [hex(a) for a in executed])
```

### Legacy per-instruction variant (flare-emu paper listing)

The VB2023 paper predated (or simply didn't use) `UC_HOOK_BLOCK`. Its listing 11 uses `UC_HOOK_CODE` (per-instruction) with manual `idaapi.FlowChart` lookup. This works but adds one Python callback per instruction — fine for 10 000-insn budgets, slow for longer. **Inside Luc Nhan, prefer `emulate_code` or `UC_HOOK_BLOCK` over this variant.**

```python
def get_bb_start_ea(address, flow_chart):
    """Return the start_ea of the FlowChart block containing `address`."""
    for block in flow_chart:
        if block.start_ea <= address < block.end_ea:
            return block.start_ea

def instruction_hook(uc, address, size, user_data):
    user_data["inst_ctr"] += 1
    bb_start = get_bb_start_ea(address, user_data['flow_chart'])
    if bb_start != user_data['current_bb']:
        user_data['executed_blocks'].append(bb_start)
        user_data['current_bb'] = bb_start
    if user_data["inst_ctr"] >= 10_000:    # safety cap
        uc.emu_stop()
```

Then `eh.emulateRange(func_ea, instructionHook=instruction_hook, registers=func_args, hookData=userData)` runs the function. After it returns, `userData['executed_blocks']` holds the BB-level trace in execution order.

**Tradeoff**: `UC_HOOK_BLOCK` boundaries match IDA's `FlowChart` only when the code isn't optimized. For compiler-optimized output, expect mismatches and verify a few transitions by hand. When you're already in IDA, prefer the FlowChart approach (more accurate); when you're standalone, prefer `UC_HOOK_BLOCK` (simpler).

## Coverage metrics

After the trace, compute two ratios to gauge how much of the function the emulation covered:

```python
num_covered_bb = sum(1 for bb in flow_chart if bb.start_ea in executed)
num_covered_obb = sum(1 for obb_addr in obbs if obb_addr in executed)
print(f"BB coverage:    {100 * num_covered_bb / len(flow_chart):.1f}%")
print(f"OBB coverage:   {100 * num_covered_obb / len(obbs):.1f}%")
```

The paper's noobware run produced `BB coverage: 51.7%`, `OBB coverage: 44.4%`. That's plenty — the four OBBs executed were the function's actual logic (start → open → read → encrypt). The unexecuted OBBs were the error-handling states (file open fails, fwrite fails), which are usually irrelevant for the main analysis.

**Interpretation rules of thumb**:

- **>80% coverage** → either the sample has minimal error paths or the function args were unrealistic. Verify by sampling a few covered BBs in IDA.
- **30–60% coverage** → typical. The covered portion is the main path. Analyze those BBs and stop unless error paths are specifically relevant.
- **<20% coverage** → args were wrong or the function exited early. Check that the input string/file pointer is valid for the function's purpose.

## Common gotchas

- **The state variable lives at different offsets across families.** noobware is `[rbp-0x138]`; Pandora has a series of short BBs implementing the dispatcher. Recover it via static analysis first; don't hardcode.
- **Jump-table offsets are signed.** Use `idaapi.as_signed(get_dword(...), 32)`, not `get_dword` directly. Negative offsets mean the entry lives before the table.
- **`UC_HOOK_BLOCK` boundaries ≠ IDA FlowChart for optimized code.** Compiler reordering (inlining, jump threading) can produce one Unicorn translation block that spans what IDA sees as multiple FlowChart BBs. Verify a handful of transitions by hand before trusting the trace.
- **The paper's `instruction_hook` runs `print(...)` on every instruction.** That's ~10 000 prints per 10 000-insn budget — the per-instruction overhead alone will change your trace. Disable prints in production; record to a list and print after `emu_start` returns.
- **Symbolic execution is not for time-sensitive IR.** Even with concolic concretization, angr's path-exploration is fiddly when CFF is combined with opaque predicates. The paper's explicit warning: "instead of trying it during a time-sensitive investigation, it is better first to invest the time and get to know the technique and the tools." Point users at [OpenSecurityTraining2 RE3201](https://p.ost2.fyi/courses/course-v1:OpenSecurityTraining2+RE3201_symexec+2021_V1/course/) for angr training.
- **The OBB heuristic conflates the function's exit BB with a real OBB.** noobware's CFG in Figure 11 of the paper is missing node `0x17E8` (the `leave; retn` block) because it doesn't match the heuristic. That's intentional — `0x17E8` contains no interesting logic, only the function return.
- **Manual debugging is still a viable fallback.** The paper's "Honorary mention: manual debugging" section notes that for heavily combined obfuscation, single-stepping through the function may be faster than writing a symbolic-execution script. Slow but reliable.

## Sample binaries

| SHA256 | Sample | Notes |
|---|---|---|
| `5b56c5d86347e164c6e571c86dbf5b1535eae6b979fede6ed66b01e79ea33b7b` | Pandora ransomware | Real-world CFF + opaque predicates case study from the paper's Figure 4 |
| (no hash — synthetic) | `noobware` | Built from Appendix A source via `tigress --Transform=Flatten --FlattenDispatch=switch --Functions=encodeAndSaveFiles`; 170 lines → 453 lines |

The paper's `pattern_matching.py`, `emulation.py`, and `symbolic_exec.py` (Appendices B–D) are IDA Python scripts targeting the noobware test case. They are IDA-specific (`idaapi.FlowChart`, `flare_emu`, `angr`) and paper-specific; this reference captures the reusable patterns. Users who want the full scripts can copy them from the paper's appendices.

## Reference links

- Geri Revay, *Don't Flatten Yourself: Restoring Malware with Control-Flow Flattening Obfuscation*, Virus Bulletin 2023 London — the source paper.
- [Tigress obfuscator](https://tigress.wtf/) — the academic C/C++ source-level obfuscator used to build the noobware test case. Supports `--Transform=Flatten` with `--FlattenDispatch=switch|goto|indirect-goto|call`.
- [Mandiant flare-emu](https://github.com/mandiant/flare-emu) — the IDA-side Unicorn wrapper the paper's `emulation.py` uses.
- [Kucherin, *Combating control flow flattening in .NET malware*](https://www.virusbulletin.com/uploads/pdf/conference/vb2022/papers/VB2022-Combating-control-flow-flattening-in-NET-malware.pdf) — VB2022 paper on DoubleZero; .NET-specific but the OBB/DBB concept transfers.
- [OALABS Emotet deobfuscation](https://research.openanalysis.net/emotet/malware/angr/symbolic%20execution/deobfuscation/research/2022/04/06/emotet_deobfuscation.html) and [Emotet deobfuscation generic](https://research.openanalysis.net/angr/symbolic%20execution/deobfuscation/research/emotet/2022/04/20/emotet_deobfuscation_generic.html) — real-world CFF + angr workflows.
- [OpenSecurityTraining2 RE3201: Symbolic Analysis](https://p.ost2.fyi/courses/course-v1:OpenSecurityTraining2+RE3201_symexec+2021_V1/course/) — recommended angr training before attempting symbolic execution on CFF.
- `algorithm-reference.md` (sibling file) — the microcode block-optimizer approach to CFF removal. Use that approach when the dispatcher is clean enough for static CFG rewiring; use this file's emulation approach when static heuristics fail.
- `string-decryption.md` (sibling file) — sibling workflow (string decryption). Different problem domain but the same basic-block hook structure applies to both.
