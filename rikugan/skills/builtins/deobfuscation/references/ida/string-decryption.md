# Stack-string decryption (Conti-style + ADVobfuscator)

> **Source**: Trích từ `unicorn-ida-skill` (external). Methodology preserved. Author attributions in each section below.
>
> **Two sibling decryption patterns gộp vào một file**: cả hai đều inline-decryptor patterns mà malware xây encrypted bytes trên stack rồi decrypt in-place. Regex signature khác nhau, algorithm khác nhau, nhưng workflow 4 bước (regex discovery → prologue slice → emulate → read) giống nhau.

## How to apply in Luc Nhan

This reference describes standalone Python workflows using `unicorn` + `capstone` + `pefile`. In Luc Nhan:

- **Pattern discovery (regex signature)** → use `list_strings` / `search_strings` to scan for known byte patterns, or use `execute_python` with `re` + `ida_bytes.get_bytes` over `.text`. The regex eggs themselves are pure byte patterns and apply identically.
- **Emulation** → `emulate_code` and `resolve_emulated_string` cover the common case when the decryptor is self-contained (no API calls, no branches leaving the routine). Pass the routine's `start_address` and the address immediately after its last instruction as the exclusive `stop_address`; set `registers` for the calling convention; add `memory_ranges` for the encrypted input, key bytes, or lookup tables that live outside the routine. `resolve_emulated_string` is the shortcut when you know the output buffer address — it returns raw bytes plus ASCII / UTF-8 / UTF-16LE candidates.
- **Sliced-block emulation with custom hooks** (the stack-snapshot diff technique in the ADV section, the `unicorn_block()` helper in the Conti section) → **not supported by `emulate_code`** (no custom `UC_HOOK_CODE` callback, no `reg_write(RIP, ...)`). For these, fall back to `execute_python` (requires user approval) and drive a Unicorn instance manually. This is the more powerful path but adds approval friction.
- **Bulk extraction** (run the decryptor against every regex hit) → `execute_python` only. Built-in tools are designed for one-shot per-call analysis.
- **IDA 9.x caveat**: this file's snippets use `capstone` (third-party, version-stable) and `idc.print_insn_mnem` (unchanged across IDA versions). No IDA 9.x migration is needed for the snippets in this file. The legacy enum-annotation pattern (`idc.add_enum` / `add_enum_member`) — which **is** removed in IDA 9.x — is covered separately in `api-hashing.md`.

---

# Part A — Conti-style stack-string decryption

## Source material

0ffset Training Solutions, *Resolving Stack Strings with Capstone Disassembler & Unicorn in Python*. Worked example: Conti ransomware SHA256 `565ff723884f77bf7e744527b0eb736373183ce1cc6c6df0fdee4b2929f685c2` (Abuse.ch). The original article extracts ~97 plaintext strings from the sample.

## The Conti-style pattern

The malware uses a sequence of `mov [ebp+offset], imm8` instructions to write the encrypted bytes onto the stack, then runs a small loop to decrypt in place:

```asm
; Build the encrypted string on the stack:
mov   [ebp+var_53], 0FCh        ; these are 1 byte each, ordered low->high
mov   [ebp+var_52], 85h
mov   [ebp+var_51], 33h
...
mov   byte ptr [ebp+var_53+1Ah-1], 0EBh   ; 26 bytes total (0x1A)

; Decrypt loop:
mov   al, [esp+esi+68h+var_53]   ; encrypted byte
mov   ecx, 31h                    ; constant 0x31 (varies per instance)
movzx eax, al
sub   ecx, eax
imul  eax, ecx, 17h               ; constant 0x17 (also varies)
cdq
idiv  edi                         ; edi = 0x7F (the modulus)
lea   eax, [edx+7Fh]
cdq
idiv  edi                         ; second idiv against 0x7F
mov   [esp+esi+68h+var_53], dl    ; write decrypted byte back
inc   esi
cmp   esi, 1Ah                    ; string length (0x1A = 26)
jb    short loc_41DB70
```

The decompiled form is:

```c
for (i = 0; i < 0x1A; ++i)
    v43[i] = (23 * (49 - (unsigned __int8)v43[i]) % 127 + 127) % 127;
```

Two things make this annoying to handle statically:

1. **The constants vary per instance.** Some routines use 24/78 instead of 23/49; some use `lea ecx, [ecx+ecx*2]; shl eax, 3` instead of `imul eax, ecx, 0x17` (because IDA simplifies both to `imul 24`).
2. **The modulus (0x7F) is in a register, not an immediate.** The first `idiv` blows up unless `edi` (or whatever register is used) is pre-initialized to 0x7F.

That's why emulation beats reimplementing the algorithm in Python.

## The four-step pipeline

### Step 1: regex signature discovery

Every Conti-style decryptor loop opens with one of two instruction encodings for `mov al, [ebp+esi+disp]`:

- `8A 44 35 CD` — `mov al, [ebp+esi-0x33]` (disp8 negative)
- `8A 84 3D F1 FE FF FF` — `mov al, [ebp+esi-0x10F]` (disp32)

Find every match in the raw `.text` blob with a regex that uses byte-level wildcards:

```python
import re
rule = re.compile(rb"\x8A\x44.{2,3}|\x8A\x84.{2,6}")
matches = list(rule.finditer(data))
# Expect ~100 matches in a Conti sample; expect ~10-20% false positives.
```

The match gives you the position of the *decryptor loop*. The encrypted bytes are *built before* the loop, somewhere in the preceding few hundred bytes.

### Step 2: reverse-disassemble to find the prologue

Take ~500 bytes before the match (heuristic — increase for long strings) and reverse-disassemble it. Walk the reversed instruction list and find the first `mov [mem], imm8` whose stack offset matches the decryptor's working offset. That instruction is where the encrypted byte sequence starts:

```python
import capstone
md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
md.detail = True
md.skipdata = True

data_block = data[m.start() - 500 : m.end()]
disasm_list = list(md.disasm(data_block, 0, len(data_block)))

offset = disasm_list_reversed_first_insn.operands[1].value.mem.disp

for insn in reversed(disasm_list):
    if (insn.mnemonic == "mov"
            and insn.operands[0].type == capstone.x86.X86_OP_MEM
            and insn.operands[0].value.mem.disp == offset):
        prologue_addr = insn.address
        break
```

If `disasm_list` is empty or has fewer than 10 instructions, the 500-byte window started mid-instruction. Slide forward by 1 byte and retry.

### Step 3: forward-disassemble to the loop end

The block runs from the prologue through the loop's `jb` (jump-if-below) that closes the decryption. Find it by walking forward:

```python
smaller_block = data[prologue_in_data : m.end() + 75]
for insn in md.disasm(smaller_block, 0, len(smaller_block)):
    if insn.mnemonic == "jb":
        loop_end = insn.address + insn.size
        break
final_block = smaller_block[:loop_end]
```

### Step 4: emulate the block

**In Luc Nhan**, prefer `emulate_code`:

```
emulate_code(
  start_address = prologue_addr,
  stop_address  = loop_end,                 # exclusive
  registers     = {
    "esi": 0, "edi": 0x7F, "ebx": 0x7F, "ecx": 0x7F,   # pre-set divisors to 0x7F
    "ebp": <output_buffer_ea>,                          # if output is at [ebp-N]
  },
  memory_ranges = [...],                     # any input/key region
  capture_ranges = [{"address": <output_ea>, "size": 64, "label": "decrypted"}],
  instruction_limit = 100000,
)
```

The `edi/ebx/ecx = 0x7F` preset is mandatory — see "Why `UC_ERR_EXCEPTION`" below.

**Standalone variant** (for reference; runs via `execute_python`):

```python
from unicorn import Uc, UC_ARCH_X86, UC_MODE_32
from unicorn.x86_const import (
    UC_X86_REG_EBX, UC_X86_REG_ECX, UC_X86_REG_EBP,
    UC_X86_REG_EDI, UC_X86_REG_ESI, UC_X86_REG_ESP,
)

def unicorn_block(block):
    mu = Uc(UC_ARCH_X86, UC_MODE_32)
    ADDRESS = 0x1000000
    mu.mem_map(ADDRESS, 4 * 1024 * 1024, 0x7)         # UC_PROT_ALL
    mu.mem_write(ADDRESS, block)
    # Pre-set every register that could plausibly be an IDIV divisor.
    mu.reg_write(UC_X86_REG_ESI, 0x7F)
    mu.reg_write(UC_X86_REG_EDI, 0x7F)
    mu.reg_write(UC_X86_REG_EBX, 0x7F)
    mu.reg_write(UC_X86_REG_ECX, 0x7F)
    mu.reg_write(UC_X86_REG_ESP, ADDRESS + 0x100000)
    mu.reg_write(UC_X86_REG_EBP, ADDRESS + 0x200000)
    try:
        mu.emu_start(ADDRESS, ADDRESS + len(block))
    except Exception:
        return b""
    ebp = mu.reg_read(UC_X86_REG_EBP)
    raw = bytes(mu.mem_read(ebp - 0xF0000, 0x100000))
    raw = raw.rstrip(b"\x00")
    nul = raw.find(b"\x00")
    return raw[:nul] if nul != -1 else raw
```

The plaintext comes out of the stack region `[EBP - 0xF0000, EBP)`. Decode with `latin-1` (UTF-8 fails on raw malware bytes):

```python
plaintext = unicorn_block(block)
print(plaintext.decode("latin-1", errors="replace"))
```

## Why `UC_ERR_EXCEPTION` and how to fix it

If you skip the divisor pre-set, you'll get:

```
Unhandled CPU exception (UC_ERR_EXCEPTION)
```

immediately at the first `idiv`. The reason: the slice doesn't include the `mov edi, 7Fh` instruction that set up the divisor, so `edi` is zero when `idiv edi` runs, and x86 raises `#DE` (divide error) which Unicorn surfaces as `UC_ERR_EXCEPTION`. Pre-set the divisor registers to a safe non-zero value (`0x7F` for Conti; any non-zero for the generic case) and the exception goes away.

This is a generic bug shape for any "sliced-block emulation" — any block that reads a register which was initialized outside the slice will fault unless you initialize it yourself.

## Conti gotchas

- **500-byte heuristic is brittle** for very long strings. Conti's ransom note is ~6KB and the 500-byte window cuts mid-encrypted-bytes, so ~6 strings out of 103 will fail to decrypt. Either increase the chunk size to a few KB, or detect the prologue more robustly by walking past any `mov [mem], imm8` until you hit the `mov al, [ebp+esi+N]` that started the match.
- **`md.skipdata = True`** is essential for packed/obfuscated code where some bytes aren't valid x86. Without it, `md.disasm()` will stop at the first invalid sequence and return a truncated list.
- **Output may not be UTF-8.** Conti's plaintext is ASCII, but other families (or unicode-encoded samples) won't be. Use `bytes.rstrip(b"\x00")` + `decode("latin-1", errors="replace")` rather than `.decode("utf-8")`.
- **False positives from the regex.** `8A 44` and `8A 84` are common byte patterns in x86 code; expect ~10-20% of regex matches to not be Conti-style decryptors. Filter post-emulation: if the resulting bytes contain no printable ASCII, drop the block.
- **`esi` may not be a counter register.** Some samples use `edi` or another register. The pattern is `cmp <reg>, <len>` followed shortly by `jb` — look for the smallest loop whose body starts with the matched instruction.
- **`imul` may be `lea + shl`.** IDA simplifies both to `imul` in the decompiler. The byte pattern at the call site will differ, but the behavior is equivalent. Emulation handles either case identically.

## Conti sample binary

Conti ransomware SHA256 `565ff723884f77bf7e744527b0eb736373183ce1cc6c6df0fdee4b2929f685c2` ([Abuse.ch](https://bazaar.abuse.ch/sample/565ff723884f77bf7e744527b0eb736373183ce1cc6c6df0fdee4b2929f685c2/)). The original 0ffset script extracts ~97 plaintext strings from it; the remaining ~6 are lost to the 500-byte window clipping long strings.

---

# Part B — ADVobfuscator stack-string decryption

## Source material

OALABS research, *ADVobfuscator* and *Extended ADVObfuscator*, plus the [ADVobfuscator C++ library](https://github.com/andrivet/ADVobfuscator). Worked examples extract ~75 plaintext strings from real `adv2.bin` / `adv3.bin` / `adv6.bin` / `amawhat.bin` malware samples.

This is a sibling workflow to Part A. The two cover different loop signatures used by the same family — Conti emits both.

## What ADVobfuscator is and why

ADVobfuscator is a C++ header-only library that, at compile time, replaces every protected string literal with a decryption routine that builds the encrypted bytes on the stack and decrypts them in place at runtime. The encryption is a single-byte `sub al, <key>` per byte — simpler than Conti's custom `imul / idiv / 0x7F` chain, but enough to defeat `strings` and most AV signature scanners.

Conti (and many other C++ malware families) use ADV for short strings (<= ~26 bytes) and a separate custom loop for longer ones. The two patterns do not overlap at the byte level, so neither tool finds the other's strings — **run both**.

## The ADV loop signature

```asm
8A 44 0C 08        mov     al, [esp+ecx+68h+var_60]   ; load byte
2C 09              sub     al, 9                       ; the per-byte key
88 44 0C 08        mov     [esp+ecx+68h+var_60], al    ; write back
41                 inc     ecx                         ; counter
83 F9 03           cmp     ecx, 3                      ; string length
72 F0              jb      short loc_4012F0            ; loop back
8D 44 24 08        lea     eax, [esp+68h+var_60]       ; pointer to plaintext
```

The counter register varies — `eax / ecx / edx / ebx / esi / edi` — and the loop terminator may be `jb` (`72`) or `jl` (`7c`). The 6-byte regex egg covers all variants:

```python
import re
egg = rb"[\x40-\x43\x46]\x83[\xf8-\xfb\xfe].[\x72\x7c]."
#                ^inc    ^cmp    ^imm   ^jb/jl ^displacement
```

The 4th byte of the match is the string length (`cmp <reg>, <len>`), so you can read it directly out of the matched bytes — no need to disassemble to find `str_len`.

## The four-step pipeline

### Step 1: regex discovery

Run the egg over the `.text` section. Each match is a candidate decryptor loop. Expect ~100 hits in a typical ADV-heavy sample.

### Step 2: estimate the prologue (with retry)

The prologue (the `mov [mem], imm8` block that builds the encrypted bytes on the stack) is roughly `40 * str_len` bytes long. That heuristic is right ~90% of the time. The other 10% are handled by an iterative retry loop that slides the prologue start 1 byte at a time:

```python
code_start = hit_offset - (40 * str_len)
if code_start < 0:
    code_start = 0

for i in range(16):
    raw = ed.text_section_data[code_start + i : hit_offset]
    _, trimmed = filter_bytes(raw)
    code = trimmed + data
    try:
        out = decrypt(code, str_len, ed)
    except Exception:
        continue
    if out is not None and out.isascii():
        print(out.decode("latin-1", errors="replace"))
        break
```

If iteration `i` and iteration `i+1` collapse to the same prologue after filtering, skip — you've already tried that one.

### Step 3: filter the prologue (`filter_bytes`)

The `40 * str_len` chunk often starts in the middle of unrelated code. To make disassembly coherent, walk forward through the chunk and strip any leading `call` / `int` / `ret` / forward-`jmp` instructions. Implementation:

```python
def filter_bytes(data):
    cs = Cs(CS_ARCH_X86, CS_MODE_32)
    cs.detail = True
    cs.skipdata = True
    code_start = 0
    last_jump = None
    out = data
    for insn in cs.disasm(data, 0):
        if insn.mnemonic.startswith(("call", "int", "ret")):
            code_start = insn.address + insn.size
            last_jump = None
        if insn.mnemonic.startswith("j"):
            if last_jump is not None:
                code_start = last_jump
            last_jump = insn.address + insn.size
            if insn.operands[0].value.imm > len(data):
                # Forward jump out of the chunk -- NOP it.
                out = (data[:insn.address] + b"\x90" * insn.size
                       + data[insn.address + insn.size:])
    return code_start, out[code_start:]
```

The result is a prologue that's safe to hand to `emu_start`.

### Step 4: emulate and read

`decrypt(code, str_len, emulator_data)` does the standard setup (stack map, code map at `image_base + .text_rva`, optional `.data` / `.rdata` if `emulator_data` has them) and runs `emu_start` with a `UC_HOOK_CODE` callback that performs the **stack-snapshot diff** technique below.

## The stack-snapshot diff technique — the key novel pattern

This is the most reusable idea in the ADV workflow. The decryptor writes plaintext somewhere on the stack — but you don't know *where* without analyzing the function's frame layout. The hook discovers the offset automatically:

```python
def trace(uc, address, size, user_data):
    insn = next(cs.disasm(uc.mem_read(address, size), address))
    # The loop's terminator is `cmp <reg>, <string_size>; jb`.
    # Hook on the cmp -- first occurrence snapshots the stack, second
    # occurrence walks the stack to find what changed since the snapshot.
    if (insn.mnemonic == "cmp"
            and insn.operands[1].type == X86_OP_IMM
            and insn.operands[1].value.imm == g_string_size):
        if stack_snapshot is None:
            # First iteration: the loop has just written byte 0; snapshot
            # the stack to use as a "before" reference.
            stack_snapshot = bytes(uc.mem_read(stack_base, stack_size))
        elif stack_string_offset is None:
            # Second iteration: byte 1 has just been written. Walk the
            # stack and find the first byte that changed.
            stack_now = bytes(uc.mem_read(stack_base, stack_size))
            for i in range(len(stack_now)):
                if stack_now[i] != stack_snapshot[i]:
                    stack_string_offset = i - 1  # the byte changed was the one BEFORE the divergence
                    break
```

After `emu_start`, read the plaintext from `[stack_base + stack_string_offset, stack_base + stack_string_offset + str_len)`.

> **In Luc Nhan**: this technique uses a custom `UC_HOOK_CODE` callback with closure state — `emulate_code` cannot express it. Run it through `execute_python` (requires user approval). Alternatively, if you can identify the output buffer offset via static analysis first (decompile + read the function's frame layout), fall back to `resolve_emulated_string` with `output_address = stack_base + offset` — much simpler, no approval gate.

**Why this is different from fixed offsets**: it discovers the runtime stack offset of the plaintext without needing to know the function's frame layout. Works for any decryptor that writes output to the stack, regardless of where in the frame it lands. Generalizable beyond ADV — any "the function wrote its output somewhere on the stack, find where" task benefits.

## Globals support

Some ADV variants store the encrypted bytes in a `static const` global and load them onto the stack via `movdqa` from an xmm register. For those, map `.data` and `.rdata` in addition to `.text`:

```python
class EmulatorData:
    def __init__(self):
        self.base = 0x00400000
        self.text_section_rva = ...
        self.data_section_rva = None      # populated only if present
        self.data_section_size = None
        self.data_section_data = None
        self.rdata_section_rva = None
        self.rdata_section_size = None
        self.rdata_section_data = None
```

Populate via `pefile`. In `decrypt()`, map each section that has an RVA before `emu_start`. The stack-snapshot diff technique still discovers the runtime write offset the same way.

Note: ADV with globals may require loading from xmm registers. Unicorn 2.x supports XMM reads/writes but not all instructions; verify your target's decryption prologue with IDA before relying on globals support.

## Wide-string handling

ADV sometimes emits UTF-16 strings (Windows registry paths, etc.). The discovered plaintext will start with a `\x00` byte. Handle it by reading `2 * str_len` and stripping every other byte:

```python
if stack_data[0] == 0:
    stack_data = uc.mem_read(stack_base + stack_string_offset - 1, str_len * 2)
if stack_data[1] == 0:
    stack_data = uc.mem_read(stack_base + stack_string_offset - 2, str_len * 2)
return stack_data.replace(b"\x00", b"")
```

The `replace(b"\x00", b"")` collapses the alternating high bytes of UTF-16LE down to ASCII (for ASCII-range UTF-16 strings).

## ADV gotchas

- **`40 * str_len` is wrong ~10% of the time.** That's why the workflow wraps it in `for i in range(16)`. Keep the retry loop. Skipping it drops ~10% of strings.
- **`filter_bytes` may trim too aggressively** if the prologue chunk starts on a jump. The disassembly will yield an empty `code`; the retry loop's `except Exception` handles it.
- **`cmp` may not fire** if the optimizer hoisted the comparison out of the loop. The decryption still completes; just `stack_string_offset` stays `None` and you skip that block.
- **The plaintext may not be ASCII** — some ADV-protected strings are UTF-16 (handled above), some are paths with non-ASCII chars. Decode with `latin-1` and check `isascii()` before printing.
- **A sample uses BOTH ADV and Conti's custom loop.** Run both Part A and Part B — the regex eggs do not overlap, so neither finds the other's strings.
- **The workflow will silently produce no output on a sample with no ADV matches.** Check that the regex actually fires (verbose logging prints hit counts).

## ADV sample binaries

| SHA256 | Sample |
|---|---|
| `765d19b4728008c1589f222d1fa49f1cb7310204c7a4574eb9f930d0544bed7b` | adv2.bin |
| `3a987fd51423f186242c3fbbdab59113c11d4ac67109e90ab948d5d0591699fb` | adv3.bin |
| `a08c766724927d41cf29f736eca1ef557ba45debd3e29fa066180ec66426dc4f` | adv6.bin |
| `4e0e4660d283270ae7abac2520b0bbd19324ff879c079ddb771c072bc7bbf60e` | amawhat.bin |

Sample strings produced from `adv6.bin` include registry keys, file paths under `%APPDATA%`, C2 URLs, Steam profile paths, Discord / Telegram data paths, and base64 alphabet tables.

---

# Choosing between Part A and Part B

| Situation | Use |
|---|---|
| Sample is packed / won't boot from entry | Either Part A or Part B (both are byte-signature scanners; both work on packed samples) |
| `8A 44 ...` / `8A 84 ...` byte signature fires (Conti `imul/idiv` loop) | Part A |
| `[\x40-\x43\x46]\x83[\xf8-\xfb\xfe]...` byte signature fires (ADV `sub al, key` loop) | Part B |
| Sample might have both | Run both — Conti emits both ADV and its custom loop |
| You know the decryptor's RVA and the output buffer address | `resolve_emulated_string` (built-in tool) — both A and B reduce to this once you've identified the routine |
| You don't know where on the stack the plaintext lands | Part B's stack-snapshot diff technique (via `execute_python`) |

---

# Reference links

## Part A (Conti)

- 0ffset Training Solutions, *Resolving Stack Strings with Capstone Disassembler & Unicorn in Python* — the source article.

## Part B (ADVobfuscator)

- OALABS, *ADVobfuscator* and *Extended ADVObfuscator* — the source research articles.
- [ADVobfuscator C++ library](https://github.com/andrivet/ADVobfuscator).
- [StackStack IDA ADV plugin](https://github.com/idiom/stackstack) — complementary IDA-side plugin.
- [FLOSS / Mandiant string decryption](https://github.com/mandiant/flare-floss/tree/master/floss) — alternative tool, heavier but more comprehensive.

## Sibling references

- `algorithm-reference.md` — high-level deobfuscation methodology. Part A "String Decryption" is the first step in the order of operations there.
- `cff-recovery.md` — sibling workflow for control-flow flattening recovery. Different problem domain but the same basic-block hook structure.
- `api-hashing.md` — sibling workflow for API-hash resolution.
- `tools.md` — reference for `emulate_code` / `resolve_emulated_string` / `execute_python` parameter semantics.
