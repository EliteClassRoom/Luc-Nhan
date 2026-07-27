# Self-restoring binary deobfuscation

> **Source**: Trích từ `unicorn-ida-skill` (external). Methodology preserved. Author attribution in the "Source material" section below.
>
> **Companion to `algorithm-reference.md`** — that doc covers IL-level deobfuscation patterns (CFF, opaque predicates, MBA, instruction substitution). This doc covers a class of obfuscation that **cannot be reversed with IL-level tools**: code that stays encrypted at rest and only exists in plaintext for microseconds during execution.

## How to apply in Luc Nhan

This reference describes a standalone Python workflow using `unicorn` + `capstone`. The core technique — **`uc.reg_write(UC_X86_REG_RIP, address + size)` inside a `UC_HOOK_CODE` callback to capture bytes without executing them** — is **not supported by `emulate_code`** (no custom hook API exposed). In Luc Nhan:

- **Pattern recognition (detect `pushfq/popfq` brackets, identify XOR-decrypt windows)** → use `decompile_function` and `read_function_disassembly` to identify the obfuscation shape. The `pushfq` / `popfq` markers and the XOR-decrypt pair are visible at the assembly level.
- **Decoding the protected bytes** → **requires `execute_python`** with a manual Unicorn + Capstone driver. User approval is mandatory. This is one of the few deobfuscation workflows that cannot be expressed via `emulate_code` — the technique fundamentally depends on intercepting each instruction before it executes.
- **PLT detection** (Step 3 of the algorithm) → use `list_segments` and `list_imports` to find the PLT range, rather than `readelf -S` (which is Linux-only and not available inside Luc Nhan).
- **Writing out the deobfuscated binary** → `execute_python` writes the patched `bytearray` to disk. The user then reloads it into IDA for clean decompilation.
- **IDA 9.x caveat**: `idc.PatchByte` / `idc.PatchDword` are still present in IDA 9.x (the byte-patch API was not migrated). The mutation API that changed is the enum / struct / type system — not relevant here.

---

## Source material

Kashiwaba Yuki, *Self-Restoring Binary Deobfuscation with Unicorn and Capstone* — writeup of the *Singlestep* challenge from Cyber Apocalypse CTF 2025 (HTB, by Malakar). The article covers deobfuscating a binary that **XOR-decrypts code blocks at runtime, executes the decrypted code, then re-encrypts them**, leaving the disk image devoid of plaintext executable code at rest.

This is a different class of obfuscation from the one-shot packers and the VM-protected binaries. It's also different from the `INT3`-VEH pattern — both use marker pairs to bracket an obfuscation window, but `INT3`-VEH contains *jump redirections* and the goal is to emulate correctly, while self-restoring contains *real code* and the goal is to capture the real code without running it.

## What a self-restoring binary is

A **one-shot packer** decrypts the entire `.text` once at entry, then the decrypted code stays in memory forever. Static analysis of the *unpacked* in-memory image recovers the original code.

A **self-restoring binary** keeps `.text` encrypted on disk *and* in memory. At runtime, around each protected code block:

```
pushfq                      # save flags
xor [rip+offset], key       # decrypt the next N bytes
...                         # executed plaintext code
xor [rip+offset], key       # re-encrypt the next N bytes
popfq                       # restore flags
```

The `xor` keys at the two ends are typically identical — the second `xor` restores the ciphertext. Between `pushfq` and `popfq`, the code runs in plaintext for a few microseconds; outside that window, the disk image and post-`popfq` memory are both ciphertext.

Static analysis sees only ciphertext; the executed code is invisible. Dynamic analysis (a debugger or emulator) sees the plaintext while it's being executed but loses it as soon as the block exits.

The Unicorn + Capstone approach captures the plaintext into a new file at the moment it exists, then writes that file out as the *deobfuscated binary*. The decompiler can then analyze the deobfuscated binary normally.

## The `pushfq`/`popfq` marker pair pattern

Most self-restoring code uses `pushfq` to save RFLAGS before the decryption `xor`, and `popfq` to restore RFLAGS after the re-encryption `xor`. These instructions:

- Have no architectural side effects beyond RFLAGS.
- Don't modify any general-purpose registers, memory, or stack.
- Preserve the program state invariantly across the obfuscation window.

Other markers exist — `push rax / pop rax`, `nop` sleds, `int3` + a magic byte — but `pushfq/popfq` is by far the most common in CTF and malware samples. The technique generalizes to any marker pair; treat the hook as a state machine over whatever pair the binary uses.

## The 5-step algorithm

Distilled from the article's solver (lines 257-374 of the source):

### Step 1: hook `UC_HOOK_CODE` with a flag state machine

```python
from unicorn import Uc, UC_ARCH_X86, UC_MODE_64, UC_HOOK_CODE
from unicorn.x86_const import UC_X86_REG_RIP
from capstone import Cs, CS_ARCH_X86, CS_MODE_64

uc  = Uc(UC_ARCH_X86, UC_MODE_64)
md  = Cs(CS_ARCH_X86, CS_MODE_64)
md.detail = True

# out_buf mirrors the input binary; we patch it as we go.
out_buf     = bytearray(input_bytes)
popfq_flag  = [False]      # mutable container so the closure can write
captured    = []           # addresses we've already deobfuscated

def hook(uc, address, size, _ud):
    raw = bytes(uc.mem_read(address, size))
    insn = next(md.disasm(raw, address))   # one instruction per call

    if insn.mnemonic == "pushfq":
        popfq_flag[0] = True
        out_buf[address:address+size] = b"\x90" * size    # NOP the marker

    elif insn.mnemonic == "popfq":
        popfq_flag[0] = False
        out_buf[address:address+size] = b"\x90" * size

    elif popfq_flag[0] and address not in captured:
        # INSIDE the obfuscation window: this is plaintext code.
        # Capture its bytes, but skip execution (see Step 2).
        out_buf[address:address+size] = raw
        captured.append(address)
        uc.reg_write(UC_X86_REG_RIP, address + size)      # <-- the key trick

        if insn.mnemonic == "call":
            target = int(insn.op_str, 16)
            if target < 0x1260:
                # PLT entry — let the runtime resolve it on its own pass.
                return
            call_addrs.append(target)

    else:
        # OUTSIDE the window: this is obfuscation glue. NOP it.
        out_buf[address:address+size] = b"\x90" * size
```

### Step 2: capture in-window, skip execution

Inside the obfuscation window, the deobfuscated code may depend on inputs (branching, input validation, library calls) that would crash emulation or run forever. For *deobfuscation* purposes you don't need to *run* the plaintext code — you only need its bytes.

The trick: `uc.reg_write(UC_X86_REG_RIP, address + size)` inside the hook. Unicorn's `UC_HOOK_CODE` callback fires *before* the instruction executes, and any `reg_write` takes effect for the *next* instruction fetch. So this single line:

1. Lets the hook capture `uc.mem_read(address, size)` (the plaintext bytes).
2. Lets the hook decide what to do with them (write to `out_buf`).
3. Advances RIP past the instruction so the CPU never executes it.

Three ways to "skip" an instruction and when each applies:

| Technique | What it does | When to use |
|---|---|---|
| `uc.emu_stop()` | Ends emulation entirely | `ret` — we've finished this call site |
| NOP the bytes in-place + emulate normally | Lets the CPU run a `0x90` | When the in-window code *must* run for downstream instructions to work (rare in self-restoring) |
| `uc.reg_write(RIP, address + size)` | Captures bytes, advances RIP, no execution | The default for self-restoring deobfuscation |

### Step 3: skip PLT calls, queue non-PLT calls

Library functions (PLT entries) live at low addresses in the ELF and require a separate resolution pass. The article uses a binary-specific threshold (`< 0x1260`); for a different ELF, find the PLT range via `readelf -S binary | grep plt` or by checking the GOT/PLT entries in IDA.

> **In Luc Nhan**: use `list_segments` to find the `.plt` section's address range, then use that as the threshold instead of hardcoding `< 0x1260`. Or use `list_imports` to enumerate import thunks directly.

Non-PLT calls (calls to other obfuscated functions in the binary) get pushed to `call_addrs` for recursive emulation.

### Step 4: drive each call site separately

The main loop dispatches each non-PLT call as a separate `emu_start` call, accumulating captures across all of them:

```python
call_addrs = [0x43E0]                              # entry into the first obfuscated block

while call_addrs:
    target = call_addrs.pop()
    if target in captured:
        continue                                    # idempotency guard
    try:
        uc.emu_start(target, 0x900D)                # bounded by the binary's exit point
    except Exception as e:
        print(f"Error at {hex(uc.reg_read(UC_X86_REG_RIP))}: {e}")
        break

with open("deobfuscated", "wb") as f:
    f.write(out_buf)
```

The `0x900D` upper bound is the binary's exit trampoline — once RIP reaches it, the current `emu_start` returns. The `try/except` catches unmapped-memory faults (e.g. when a call target is library code that wasn't loaded); the `not in captured` guard prevents infinite recursion if A calls B calls A.

### Step 5: write out the deobfuscated binary

The bytearray has been patched throughout the run:

- Every `pushfq`/`popfq` is `\x90 * size`.
- Every obfuscation-glue instruction (the `xor` decrypt/re-encrypt, intermediate setup) is `\x90 * size`.
- Every in-window instruction is the original plaintext code.

Writing `out_buf` to disk produces an ELF/PE that opens cleanly in IDA — the decompiler sees linear, non-obfuscated code, no `xor` loops, no `pushfq`/`popfq` markers.

## Recursive `call_addrs` queue — when and why

One-shot `emu_start(entry, 0)` (the simple packer pattern) only covers the function at `entry`. For self-restoring binaries, **each obfuscated block can end in a call to another obfuscated function**, and you can't traverse those calls without dispatching a separate `emu_start` for each one. The `call_addrs` queue is how you do that.

The pattern is similar to a worklist algorithm:

1. Start with the entry point in `call_addrs`.
2. `emu_start` runs until `ret` or the binary's exit trampoline.
3. Any non-PLT `call` encountered during emulation pushes a new target onto the queue.
4. Pop the next target and emulate it.
5. Repeat until the queue is empty.

The `not in captured` guard before `emu_start` is essential: A → B → A would otherwise loop forever. Each address is processed at most once.

## Reassembling the deobfuscated binary

The output binary is the original file with patched instruction bytes. Headers, sections, entry point, and segment layout are preserved; only the contents of the `.text`-adjacent obfuscation windows change.

For ELF targets: the deobfuscated binary should open directly in IDA. The entry point is unchanged (the binary's first instruction is rarely inside a self-restoring window), and the section sizes haven't shifted (we replaced bytes in place).

For PE targets: verify the OEP. If the entry stub itself was inside a self-restoring window, the OEP may have shifted; check with `pefile` and adjust `AddressOfEntryPoint` if needed.

After reassembly, the decompiler produces clean output — the Singlestep challenge's matrix-inverse code (4×4 array initialization, input validation, identity-matrix check) becomes readable in the deobfuscated binary even though it was completely absent from the original.

## The `reg_write(RIP, ...)` technique is general

This isn't only for self-restoring binaries. Any task where you want to *observe* instruction bytes without *executing* them benefits:

- **Self-restoring deobfuscation** — primary use case (this article).
- **`INT3`-VEH emulation** — register a `UC_HOOK_INTR` callback, decode the `INT3 <byte>` encoding, `reg_write(EIP, new_eip)` to redirect. Different hook type, same RIP-rewrite idea.
- **Anti-debug checks** — capture the check (`IsDebuggerPresent`, `rdtsc` timing, etc.) into a buffer for analysis, then `reg_write(RIP, address + size)` to skip it and continue.
- **Custom instruction decoder** — when the binary uses an undocumented or obfuscated opcode, capture it for inspection, then `reg_write(RIP, ...)` to dispatch your own implementation via an `emu_start` on a helper function.
- **Trace-only mode** — capture every instruction's bytes for offline static analysis, never letting the side effects (memory writes, syscalls, register modifications) actually happen.

Tradeoff vs `uc.emu_stop()`:

- `emu_stop()` ends emulation entirely. Use when you want to bail out of this call site (e.g. on `ret`).
- `reg_write(RIP, address + size)` continues to the next instruction. Use when you want to observe one instruction but keep running.

## Common gotchas

- **`pushfq/popfq` may not be the exact marker.** Other families use `push rax/pop rax`, `nop` sleds, or no marker at all (the XOR key is implicit in the surrounding context). When the marker isn't `pushfq/popfq`, find it via static analysis first: look for the XOR instruction that touches code memory (`xor [rip+offset], imm` with the destination address inside `.text`).
- **The `reg_write(RIP, ...)` trick does NOT prevent the CPU from advancing** *past this single instruction*. The next instruction still runs. If you want to skip a whole block, you need to call `reg_write` from every instruction in the block — or use `emu_stop()` and dispatch a new `emu_start` from the post-block address.
- **`capstone`'s `disasm` may produce zero instructions for some byte patterns.** When the iterator is empty, `next(...)` raises `StopIteration`. The article's `for i, instruction in enumerate(md.disasm(...))` handles this implicitly (the `for` loop just doesn't iterate), but the rest of the hook assumes the iterator yielded at least one instruction. Defend against this:
  ```python
  insns = list(md.disasm(raw, address))
  if not insns:
      return
  insn = insns[0]
  ```
- **PLT threshold is binary-specific.** The source uses `< 0x1260` for this particular ELF. For a different binary, find the PLT range via `list_segments` (preferred) or by checking the GOT/PLT entries in IDA. Hardcoding the threshold silently misclassifies calls.
- **Recursive calls can cycle.** A calls B calls A. The `not in captured` guard prevents infinite loops, but verify by checking the final `captured` list makes sense — every entry should be inside a `pushfq/popfq` window and correspond to a unique instruction.
- **The output binary may need entry-point patching** for PE. If the entry stub itself was inside a self-restoring window, the OEP may have shifted; check with `pefile` and adjust `AddressOfEntryPoint` if needed.
- **Library calls inside the obfuscation window must be skipped via the `if target < PLT_THRESHOLD: return` check.** If you let a PLT call run, emulation crashes immediately (the PLT isn't mapped as executable code). The check is *inside* the in-window branch, not before it.
- **The `call_addrs` queue can grow large.** For a deeply nested call graph it may reach thousands of entries. Each `emu_start` is fast (~microseconds for a tiny block), but verify the loop terminates by checking the queue is empty at the end.

## Sample binary

| Sample | Source | Notes |
|---|---|---|
| `singlestep` | Cyber Apocalypse CTF 2025 (HTB, by Malakar) | Real CTF challenge. No SHA published, but reproducible from the HTB archive. Self-restoring XOR blocks implement a 4×4 matrix inverse for input validation; the plaintext code is completely absent from the on-disk binary. |

The article's two code listings (lines 63-138 and lines 257-374 of the source) are IDA-independent Python scripts — copy them verbatim, point `open('singlestep', 'rb')` at the binary, and they will produce a working `deobfuscated` binary.

## Reference links

- Kashiwaba Yuki, *Self-Restoring Binary Deobfuscation with Unicorn and Capstone* — the source article.
- HTB Cyber Apocalypse CTF 2025 archive — the *Singlestep* challenge. Search for "Malakar" or "singlestep" in the HTB archive.
- [Unicorn hook type reference](https://www.unicorn-engine.org/docs/api.html) — `UC_HOOK_CODE` callback signature `(uc, address, size, user_data)`.

## Sibling references

- `algorithm-reference.md` — high-level deobfuscation methodology. Self-restoring binaries are mentioned there as a "VM boundary" case where IL-level tools cannot reverse the obfuscation; this file is the concrete workflow for that case.
- `cff-recovery.md` — sibling workflow for control-flow flattening recovery. Different obfuscation class, but both rely on emulation to recover structure that static analysis cannot see.
- `string-decryption.md` — sibling workflow for inline string decryptors. Same emulation shape, different problem domain.
- `api-hashing.md` — sibling workflow for API-hash resolution.
- `tools.md` — reference for `execute_python`, `list_segments`, `list_imports`, `decompile_function` parameter semantics.
