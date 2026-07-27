# API hashing analysis

> **Source**: Trích từ `unicorn-ida-skill` (external). Methodology preserved. Author attributions in each section below.
>
> **Gộp 3 workflow**: (B) annotation-only với API hashing, (C) binary-rewriting với `idc.PatchDword`, (D) bulk enumeration của call sites. Mở đầu bằng phần Background giới thiệu khái niệm.

## How to apply in Luc Nhan

This reference describes standalone Python + IDAPython workflows. In Luc Nhan:

- **Hash extraction** (extract the hash function bytes, emulate to compute hashes) → use `emulate_code` with the hash function as the code range and register args set per calling convention. `resolve_emulated_string` does not apply here (output is a 32-bit integer in `eax`, not a string buffer) — use `emulate_code` with `capture_ranges=[]` and read `final_registers["eax"]` from the result.
- **Building the `{hash: name}` dictionary** → `execute_python` (requires `pefile` to enumerate DLL exports — not a built-in Luc Nhan capability). Build once, save as JSON.
- **Annotation variant (Part B)** — `idc.add_enum` / `idc.add_enum_member` / `idc.op_enum` were **removed in IDA 9.x** (the enum API was migrated to the UDT system). On IDA 9.x, the modern equivalent is `ida_typeinf.tinfo_t.create_udt()` + `add_udm()` for the enum members, then `apply_tinfo()` to annotate operands. The simpler workaround that still works on all IDA versions: **use `set_comment` at each call site** with the resolved API name as the comment text — no enum, no operand rewrite, just a persistent comment that survives redecompilation. The full IDA 9.x enum migration is out of scope for this reference; use `set_comment` for annotation-only workflows.
- **Binary-rewriting variant (Part C)** — `idc.PatchDword` still works on IDA 9.x. The bigger constraint is `Appcall.proto`, which requires a **live debug session** — not available in headless mode and not something an agent can set up autonomously. Treat the LummaC2 pipeline as a **manual workflow** the user runs after starting a debug session; do not attempt to fully automate it.
- **Bulk enumeration (Part D)** — IDA xref walker. Use `xrefs_to` for single-site lookup; for the full backward-walking pattern (find hash-loading `mov` before the call), use `execute_python` with `idc.prev_head` + `idc.print_insn_mnem`.

---

# Background — What API hashing is and why

## Source material

IIJ Research, *Effective Malware Analysis using Unicorn* (~470 lines; the appendix contains the full IDAPython script). The article uses the TorNet/PureHVNC loader as the worked example — a sample whose API calls are resolved at runtime via a custom MurmurHash2 over the function name.

Malware that uses API hashing stores only the *hash* of an API name in the binary — never the name itself. At runtime, the malware hashes its known function names until it finds a match against the stored hash, then calls through the resulting pointer.

```text
push    4                ; arg: hash size
push    439C7E33h       ; arg: hash value (MurmurHash2("LoadLibraryA"))
push    0Fh             ; arg: seed
push    0               ; arg: hash algorithm id
call    api_hash_resolver
```

Static IAT analysis finds nothing useful: the IAT is full of standard imports (`LoadLibraryA`, `GetProcAddress`), but the actual malware calls are hidden behind hashes that the analyst must resolve themselves.

The Unicorn-based approach: extract the bytes of the hash algorithm from IDA, emulate it standalone with the function name as input, and read the hash back from EAX. Then build a `{hash: name}` dictionary by hashing every export of the relevant Windows DLLs (also with Unicorn), and you can resolve every hash in the sample to its API name.

---

# Part B — Annotation variant (IIJ / TorNet / PureHVNC)

The simplest variant. Resolves hashes to names and annotates the disassembly. Does not modify code or the IAT.

## Step 1: extract the hash function bytes from IDA

Open the binary in IDA, find the API resolver function, locate the hash algorithm sub-function. Get the bytes:

```python
HASHCODE_START = 0x1007030D   # inclusive
HASHCODE_END   = 0x1007051E   # exclusive
hash_code = ida_bytes.get_bytes(HASHCODE_START, HASHCODE_END - HASHCODE_START)
```

Save `hash_code` to disk if you want to use it from standalone (no IDA).

> **In Luc Nhan**: use `read_function_disassembly` to identify the bounds, then `execute_python` to call `ida_bytes.get_bytes`. Or skip extraction entirely and use `emulate_code` directly with `start_address` / `stop_address` pointing at the hash function in the IDB — no byte extraction needed.

## Step 2: emulate the hash function

**In Luc Nhan, prefer `emulate_code`**:

```
emulate_code(
  start_address = "0x1007030D",            # hash function start
  stop_address  = "0x1007051E",            # exclusive end (address of the ret + 1)
  registers     = {
    "ebp": "<scratch_ebp_ea>",
    "edi": <string_length>,                # the function reads EDI / ESI as length
    "esi": <string_length>,
  },
  memory_ranges = [
    {"address": "<string_ea>", "size": <string_length>},     # the API name bytes
    {"address": "<ebp+offset>", "size": 4},                   # EBP+0x0C: length
    {"address": "<ebp+offset>", "size": 4},                   # EBP+0x10: seed
  ],
  capture_ranges = [],
  instruction_limit = 100000,
)
# Result: final_registers["eax"] contains the 32-bit hash.
```

The hash function in the IIJ example expects its arguments at fixed `EBP`-relative offsets:

- `EBP + 0x0C` — string length (4 bytes)
- `EBP + 0x10` — seed value (4 bytes)
- `EBP - 0x48` — string bytes (the API name)

It also reads `EDI` and `ESI` (used by code outside the snippet — they must be non-zero or at least a sane length value).

**Standalone variant** (for reference; runs via `execute_python`):

```python
import struct
from unicorn import Uc, UC_ARCH_X86, UC_MODE_32
from unicorn.x86_const import (
    UC_X86_REG_EAX, UC_X86_REG_EBP, UC_X86_REG_EDI, UC_X86_REG_ESI,
)

def calculate_hash(string: bytes, hash_code: bytes, seed: int = 0xB801FCDA) -> int:
    """Emulate the sample's hash algorithm on `string`, return the hash."""
    uc = Uc(UC_ARCH_X86, UC_MODE_32)

    STACK = 0x00100000
    STACK_SIZE = 0x00100000
    uc.mem_map(STACK, STACK_SIZE)
    uc.mem_write(STACK, b"\x00" * STACK_SIZE)

    CODE = 0x00200000
    CODE_SIZE = 0x00100000
    uc.mem_map(CODE, CODE_SIZE, UC_PROT_ALL := 0x7)
    uc.mem_write(CODE, hash_code)

    EBP = STACK + (STACK_SIZE // 2)
    uc.reg_write(UC_X86_REG_EBP, EBP)

    uc.mem_write(EBP + 0x0C, struct.pack("<I", len(string)))
    uc.mem_write(EBP + 0x10, struct.pack("<I", seed))
    uc.mem_write(EBP - 0x48, string)
    uc.reg_write(UC_X86_REG_EDI, len(string))
    uc.reg_write(UC_X86_REG_ESI, len(string))

    uc.emu_start(CODE, CODE + len(hash_code))
    return uc.reg_read(UC_X86_REG_EAX) & 0xFFFFFFFF


# Test:
# calculate_hash(b"LoadLibraryA", hash_code) -> 0x439C7E33 for the IIJ sample.
```

The function call returns a 32-bit integer hash. You don't need to know *what* hash algorithm this is — the bytes are sufficient.

## Step 3: build the `{hash: name}` dictionary

Iterate the exports of the DLLs the malware touches (typically `kernel32.dll` and `ntdll.dll`), and hash each export name with the same `calculate_hash`:

```python
import pefile

API_HASH_DLLS = ("kernel32.dll", "ntdll.dll")

def build_api_dict(dll_dir=r"C:\Windows\System32") -> dict:
    api_dict = {}
    for dll in API_HASH_DLLS:
        try:
            pe = pefile.PE(fr"{dll_dir}\{dll}")
            for sym in pe.DIRECTORY_ENTRY_EXPORT.symbols:
                if sym.name:
                    api_dict[calculate_hash(sym.name, hash_code)] = sym.name
        except (AttributeError, pefile.PEFormatError, FileNotFoundError):
            continue
    return api_dict


api_dict = build_api_dict()
# Example: api_dict[0x439C7E33] == b"LoadLibraryA"
```

This step takes ~30 seconds for kernel32+ntdll on a fast machine, and ~10 minutes for the full Windows API surface if you go wider. The first two DLLs cover the vast majority of real-world malware.

> **In Luc Nhan**: `pefile` is not a built-in tool. Run this through `execute_python`. The dict can be cached as JSON for reuse across sessions — building it once per sample family is enough.

## Step 4: annotate IDA with the dictionary

> **⚠ IDA 9.x caveat**: The original IIJ script uses `idc.add_enum`, `idc.add_enum_member`, `idc.get_enum`, and `idc.op_enum` — **all removed in IDA 9.x**. On IDA 9.x, the modern equivalent is `ida_typeinf.tinfo_t.create_udt()` + `add_udm()`, but the migration is non-trivial. The simpler, version-stable alternative is **`set_comment` at each call site** with the resolved API name as the comment text — no enum, no operand rewrite, just a persistent disassembly comment that survives redecompilation. The original `op_enum` script is preserved below for reference; on IDA 9.x use `set_comment` instead.

Original IIJ annotation script (IDA 8.x and earlier):

```python
import idaapi, idautils, idc

API_RESOLVER_FN = 0x10067C3A     # address of the resolver function
ENUM_NAME       = "APIHASH"

# Create the enum once. Members will be added on demand below.
enum = idc.get_enum(ENUM_NAME)
if enum == idc.BADADDR:
    enum = idc.add_enum(idaapi.BADNODE, ENUM_NAME, idaapi.hex_flag())

# Walk every xref to the resolver; the third push operand before the call
# holds the hash value.
for xref in idautils.XrefsTo(API_RESOLVER_FN):
    ea = xref.frm
    push_count = 0
    while ea != idc.BADADDR:
        if idc.print_insn_mnem(ea) == "push":
            push_count += 1
            if push_count == 3:
                hash_value = idc.get_operand_value(ea, 0) & 0xFFFFFFFF
                break
        ea = idc.prev_head(ea)

    if hash_value not in api_dict:
        print(f"[-] unresolved: {hash_value:#x} at {xref.frm:#x}")
        continue

    name = api_dict[hash_value].decode()
    print(f"[+] {hash_value:#x} -> {name} at {xref.frm:#x}")

    # Add the enum member (one-time per hash) and apply it to the operand.
    if idc.get_enum_member(enum, hash_value, 0, 0) == -1:
        idc.add_enum_member(enum, f"{ENUM_NAME}_{name}", hash_value)
    idc.op_enum(ea, 0, enum, 0)
```

After running this, every `push 439C7E33h` instruction in the disassembly shows as `push APIHASH_LoadLibraryA`, and the decompiler reflects the same.

> **Luc Nhan equivalent on IDA 9.x**: replace the enum-members + `op_enum` block with `set_comment`:
>
> ```python
> # IDA 9.x-compatible annotation
> set_comment(ea=xref.frm, comment=f"API hash: {name} ({hash_value:#x})")
> ```
>
> This is less flashy than the enum (the `push` operand still shows the raw hash constant), but the comment is visible in both the disassembly and the decompiler and survives redecompilation.

## Common gotchas for the annotation variant

- **String is lowercase.** The IIJ sample lowercases the input before hashing. If you skip this you'll get wrong hashes for any API name that contains uppercase letters.
  ```python
  api_dict = {calculate_hash(sym.name.lower(), hash_code): sym.name
              for ... in ...}
  ```
- **Seed value matters.** `0xB801FCDA` is the default for the IIJ sample; the actual value is whatever the malware uses. Find it with IDA — it's the third argument to the resolver, hardcoded in every call site.
- **Algorithm per family.** Guloader, Emotet, Conti, etc. each use their own hash (or a modified version of a public one). The IIJ approach works for *any* of them because the bytes are the contract — you don't need to understand the algorithm to compute the hash.
- **Byte-range boundaries.** Cut the slice cleanly at function prologues / epilogues. Including a `RET` at the end is fine (Unicorn stops when EIP leaves the mapped region); including initialization code from outside the hash function is also fine but wasteful.
- **EMU_START hits unmapped memory.** Some hash implementations read past the input string. If you crash at `[EBP - 0x48 + len]`, either pad the string or hook `UC_HOOK_MEM_UNMAPPED` to map a scratch page.

---

# Part C — Binary-rewriting variant (LummaC2)

> **⚠ Manual workflow warning**: this variant **requires a live debug session** because of `Appcall.proto`. It cannot be fully automated in headless mode or run without user setup. Use it only when the annotation variant (Part B) is insufficient — i.e., when you need the decompiler to produce clean `call RealApi` output rather than annotated `call ResolveApiByHash` + comment.

## Source material

Outpost24 KrakenLabs, *Everything You Need To Know About LummaC2 Stealer*. The article walks through the full Unicorn + IDAPython pipeline that turns LummaC2's API-hashed binary into a fully-deobfuscated IDB the decompiler can recover cleanly.

This is the **binary-rewriting** variant of the API-hashing workflow. It complements Part B (which covers the annotation variant — read-only `MakeComm` + auto-generated enum) with the next stage: mutate the IDB so the decompiler produces clean output.

## How this differs from Part B

Three load-bearing distinctions:

1. **Goal.** Part B stops at annotating the disassembly (`MakeComm` + enum members). This Part **mutates the IDB** (`PatchDword` over the existing `call ResolveApiByHash`) so the decompiler recovers clean argument display without manual cleanup.
2. **Calling convention.** Part B's hash function reads its args from `EBP`-relative stack offsets; LummaC2's uses `ECX = string pointer`, `EAX = hash` (32-bit only — see gotchas).
3. **Hash discovery.** Part B's script walks pushes backward looking for the hash operand; the LummaC2 script walks backward looking for `mov ecx/edi/esi, <imm>` patterns to handle three different ways the malware may load the hash before the call.

## Step 1: `emulate_murmurhash2(data, seed=32)` — emulate the hash shellcode

LummaC2 inlines a hand-rolled MurmurHash2 (171 bytes of shellcode) into the binary. The shellcode expects `ECX` to point at a null-terminated string and returns the 32-bit hash in `EAX`. The bytes are extracted from the binary — they're the contract, so don't hardcode a copy from the article; re-extract from your sample.

```python
import unicorn

# Shellcode bytes extracted from the LummaC2 sample's ResolveApiByHash wrapper.
# 171 bytes, hand-rolled MurmurHash2 variant.
CODE = bytes.fromhex(
    "56578BF98BD78D4A018A024284C075F92BD18BF283F62083FA047C4D538BDAC1"
    "EB026BC3FC03D00FB64F030FB64702C1E1080BC869F695E9D15B0FB64701C1E1"
    "080BC80FB607C1E10883C7040BC869C995E9D15B8BC1C1E81833C169C895E9D1"
    "5B33F183EB0175BF5B83EA01741C83EA01740E83EA01751D0FB64702C1E01033"
    "F00FB64701C1E00833F00FB60733C669F095E9D15B8BC6C1E80D33C669C895E9"
    "D15B5F5E8BC1C1E80F33C1"
)

def emulate_murmurhash2(data: bytes, seed: int = 32) -> int:
    CODE_OFFSET = 0x1000000
    LIBNAME     = 0x7000000
    STACK_BASE  = 0x00300000
    STACK_SIZE  = 0x00100000

    mu = unicorn.Uc(unicorn.UC_ARCH_X86, unicorn.UC_MODE_32)
    mu.mem_map(CODE_OFFSET, 4 * 1024 * 1024)
    mu.mem_write(CODE_OFFSET, CODE)

    mu.mem_map(LIBNAME, 4 * 1024 * 1024)
    mu.mem_write(LIBNAME, data)

    mu.mem_map(STACK_BASE, STACK_SIZE)
    mu.mem_write(STACK_BASE, b"\x00" * STACK_SIZE)

    mu.reg_write(unicorn.x86_const.UC_X86_REG_ESP, STACK_BASE + 0x800)
    mu.reg_write(unicorn.x86_const.UC_X86_REG_EBP, STACK_BASE + 0x1000)
    mu.reg_write(unicorn.x86_const.UC_X86_REG_ECX, LIBNAME)

    mu.emu_start(CODE_OFFSET, CODE_OFFSET + len(CODE))
    return mu.reg_read(unicorn.x86_const.UC_X86_REG_EAX) & 0xFFFFFFFF
```

The function returns the 32-bit hash from `EAX`. Test by hashing `"LoadLibraryA"` (or any other known export) and confirming the output matches what you see statically in the binary.

## Step 2: `dump_hash_dlls` — build the `{hash: name}` dictionary

Walk a directory of Windows DLLs, parse each with `pefile`, hash every export name with the function above, and save as JSON. The dictionary shape is `{hash: "dll_APIName"}` so the lookup string already carries the DLL — `patch_apicall` needs to know which DLL to force-load.

```python
import os, json, pefile

HASH_DICT_PATH = r"c:\murmurhash2_hashes_dict.json"

def dump_hash_dlls(dlls_dir: str = "dlls/") -> dict:
    """Walk `dlls_dir`, hash every export of every DLL via emulate_murmurhash2."""
    api_dict = {}
    for dirpath, _, filenames in os.walk(dlls_dir):
        for filename in filenames:
            if not filename.endswith(".dll"):
                continue
            try:
                pe = pefile.PE(os.path.join(dirpath, filename))
            except pefile.PEFormatError:
                continue
            for exp in pe.DIRECTORY_ENTRY_EXPORT.symbols:
                if not exp.name:
                    continue
                try:
                    h = emulate_murmurhash2(exp.name)
                except Exception as e:
                    print(f"hash fail {exp.name}: {e}")
                    continue
                # JSON keys are strings; format: "dll_APIName" so the lookup
                # string carries the DLL info needed by patch_apicall.
                api_dict[str(h)] = f"{filename[:-4]}_{exp.name.decode()}"
    return api_dict

if __name__ == "__main__":
    api_dict = dump_hash_dlls()
    with open(HASH_DICT_PATH, "w") as fd:
        json.dump(api_dict, fd, indent=2)
    print(f"dumped {len(api_dict)} hashes")
```

For kernel32 + ntdll + a few high-value DLLs this takes ~30 seconds; the full Windows API surface is ~10 minutes.

## Step 3: `resolve_all_APIs(resolve_ea)` — find and annotate every call site

Walk every xref to `ResolveApiByHash`. For each call site, scan backward up to 30 instructions looking for a `mov ecx / mov edi / mov esi, <imm>` that holds the hash. LummaC2 uses three calling conventions; the walker handles all three:

```text
.text:0040214B B9 73 10 FF E8     mov     ecx, 0E8FF1073h
.text:00402150 E8 7E 61 00 00     call    ResolveApiByHashWrapper      ; direct

.text:004074F6 BF 29 A1 D3 5F     mov     edi, 5FD3A129h              ; via EDI
.text:004074FB BA C8 4B 42 00     mov     edx, offset aWininet_dll
.text:00407500 8B CF              mov     ecx, edi
.text:00407502 E8 CC 0D 00 00     call    ResolveApiByHashWrapper

.text:00407D8C BE 30 E2 95 3D     mov     esi, 3D95E230h              ; via ESI
.text:00407D91 8B D3              mov     edx, ebx
.text:00407D93 6A 00              push    0
.text:00407D95 8B CE              mov     ecx, esi
.text:00407D97 E8 37 05 00 00     call    ResolveApiByHashWrapper
```

The walker looks for `mov ecx`, `mov edi`, `mov esi`. The `< 0x10` heuristic on the immediate skips intermediate register-to-register moves (`mov ecx, edi` has a 1-byte immediate) so the walker keeps walking back to find the actual hash load.

```python
import idautils, idc

HASHES_DICT: dict = {}      # populated by setup()

def setup(hashes_dict_file: str) -> None:
    global HASHES_DICT
    with open(hashes_dict_file, "rb") as fd:
        HASHES_DICT = json.load(fd)


def resolve_all_APIs(resolve_ea: int) -> list:
    """Walk every xref to ResolveApiByHash; annotate; return list of patches."""
    if resolve_ea is None:
        print("[!] Resolve failed.")
        return []

    patches = []
    total_found = total_resolved = 0

    for ref in idautils.CodeRefsTo(resolve_ea, 1):
        total_found += 1
        curr_ea = ref
        api_hash = 0

        for _ in range(30):                # walk back up to 30 instructions
            prev = idc.PrevHead(curr_ea)
            insn = idc.GetDisasm(prev).replace(" ", "")

            if "movecx" in insn or "movedi" in insn or "movesi" in insn:
                api_hash = idc.GetOperandValue(prev, 1)

                if api_hash < 0x10:
                    # Register-to-register move (mov ecx, edi) — keep walking.
                    curr_ea = prev
                    continue

                key = str(api_hash)
                if key in HASHES_DICT:
                    apicall = HASHES_DICT[key].split("_")[-1]
                    print(f"[+] {hex(prev)} {hex(api_hash)} -> {apicall}")
                    idc.MakeComm(ref, apicall)
                    patches.append((ref, HASHES_DICT[key]))
                    total_resolved += 1
                else:
                    print(f"[?] hash {hex(api_hash)} not in dictionary")
                break

            curr_ea = prev

    print(f"Total APIs found: {total_found}  resolved: {total_resolved}")
    return patches
```

After this runs, every `call ResolveApiByHash` has a comment showing the resolved API name (e.g., `; InternetOpenA`), and `patches` is a list of `(call_addr, "dll_APIName")` tuples ready for the rewriter.

> **In Luc Nhan**: `idc.MakeComm` is the legacy API. Use `idc.set_cmt(ea, comment, rptble=0)` on IDA 9.x, or call the built-in `set_comment` tool. The walker itself (`idc.PrevHead` + `idc.GetDisasm`) is unchanged across versions.

## Step 4: `patch_apicall(addr, apicall)` — rewrite the binary in place

Three sub-steps: force-load the DLL, patch the `call`, NOP-walk the hash-loading movs.

```python
def patch_nops(addr: int, size: int) -> None:
    for i in range(size):
        idc.PatchByte(addr + i, 0x90)


def patch_apicall(addr: int, apicall: str) -> bool:
    """Rewrite 'call ResolveApiByHash' at `addr` into 'call <real api>'.
    NOP the trailing 'call eax' and the hash-loading movs above it.
    Returns True on success.
    """
    # 1. Force-load the DLL via AppCall if it isn't loaded yet.
    loadlib = Appcall.proto(
        "kernel32_LoadLibraryA", "int __stdcall loadlib(const char *fn);"
    )

    apiaddr = idc.LocByName(apicall)
    if apiaddr == 0xFFFFFFFF:
        loadlib(f"{apicall.split('_')[0]}.dll")
        apiaddr = idc.LocByName(apicall)
        if apiaddr == 0xFFFFFFFF:
            return False                           # still not loaded — skip

    # 2. Patch the call: 0xE8 <rel32>.
    # & 0xFFFFFFFF is mandatory — Python ints are arbitrary precision,
    # but the 0xE8 rel32 is 32-bit and PatchDword rejects negatives.
    rel_offset = (apiaddr - addr - 5) & 0xFFFFFFFF
    idc.PatchDword(addr + 1, rel_offset)

    # NOP the trailing 'call eax' (2 bytes: 0xFF 0xD0).
    idc.PatchByte(addr + 5, 0x90)
    idc.PatchByte(addr + 6, 0x90)

    # 3. Walk back NOPping the hash-loading movs.
    curr_ea = addr
    for _ in range(20):
        prev = idc.PrevHead(curr_ea)
        insn = idc.GetDisasm(prev).replace(" ", "")

        if "movecx" in insn or "movedx" in insn:
            operand_type = idc.GetOpType(prev, 1)
            param        = idc.GetOperandValue(prev, 1)

            if operand_type not in (1, 2, 5):     # register / memory / imm
                curr_ea = prev
                continue

            if param < 0x10:
                # 2-byte mov (e.g. 8B CE  mov ecx, esi)
                if "eax" not in insn:             # don't NOP a result-mov
                    patch_nops(prev, 2)
            else:
                # 5-byte mov (e.g. B9 D6 3F B0 78  mov ecx, 78B03FD6h)
                patch_nops(prev, 5)

        curr_ea = prev

    return True


def patch_apicall_wrapper(patches: list) -> None:
    total = 0
    for addr, apicall in patches:
        if patch_apicall(addr, apicall):
            total += 1
    print(f"Total APIs patched: {total}")
```

After this runs, the binary is fully deobfuscated: every `call ResolveApiByHash` is now `call RealApi`, and the surrounding hash-loading instructions are NOPs. The decompiler sees clean calls with real argument types.

## Step 5: the end-to-end pipeline

```python
setup(r"c:\murmurhash2_hashes_dict.json")
patches = resolve_all_APIs(0x004082D3)      # EA of ResolveApiByHash
patch_apicall_wrapper(patches)
```

Run from inside IDA with a debug session paused at the Entry Point — `Appcall.proto` needs a live process to call `LoadLibraryA` against. After completion, the IDB is rewritten in place; save it (`File → Produce IDB → All`) before closing IDA so the patches persist.

## LummaC2 gotchas

- **32-bit only.** LummaC2's hash function uses `EAX/ECX/0xE8 rel32`. A 64-bit variant (or different family) would need `RAX/RCX` and `0xE8`/`0xE9` with a different patching strategy. Verify with `idaapi.get_inf_sizeof_long()` before running — if it returns 8, this script will silently corrupt the IDB.
- **AppCall requires a debug session.** `Appcall.proto` calls run in the debuggee's address space; without a live process, `loadlib("wininet.dll")` returns 0 and the API can't be resolved. Start a debug session and pause at OEP before running.
- **The 30-instruction window is a heuristic.** Some LummaC2 build variants load the hash farther back (or interleave setup code). If `total_resolved < total_found`, increase the window or check whether a `push imm; mov ecx, [esp+...]` variant is in play.
- **`& 0xFFFFFFFF` is mandatory on `rel_offset`.** Without it, Python's arbitrary-precision integer can produce a negative number for backward calls, which `idc.PatchDword` rejects. The mask is not optional.
- **Hash dictionary can be huge.** Across the full Windows API surface, `dump_hash_dlls` produces ~100K entries. Build once and save as JSON; loading the JSON in `setup` is faster than re-hashing.
- **Re-running accumulates duplicates.** `idc.MakeComm` is idempotent, but `patches` will double up if you re-run `resolve_all_APIs`. Dedupe by `addr` before passing to `patch_apicall_wrapper` if re-running.
- **The walker matches the disassembled mnemonics as strings.** It uses `idc.GetDisasm(prev).replace(" ", "")` and substring-tests against `movecx`/`movedi`/`movesi`. Comments containing those substrings (rare but possible) would cause false positives. Strip comments first or check with `idc.GetMnem(prev)` if you see anomalies.

## LummaC2 sample binary

| Field | Value |
|---|---|
| Family | LummaC2 (C-based info-stealer, since Dec 2022) |
| SHA256 | `277d7f450268aeb4e7fe942f70a9df63aa429d703e9400370f0621a438e918bf` |
| C2 | `195[.123[.226[.91` |
| Targets | Crypto wallets (Binance, Electrum, Ethereum), browser extensions (MetaMask, Authy, Trezor Password Manager), `*.txt` files under `%userprofile%` |
| Source | Outpost24 KrakenLabs public article |

---

# Part D — Bulk call-site enumeration (Guloader)

## Source material

Companion to the **VEH-style INT3 control flow** emulation pattern (separate from this reference). Contains the IDA Python snippets used to enumerate every call site of a known string-decryption routine, so you can feed each one to the emulation helper.

## Why this matters

A typical Guloader binary has **30–60** encrypted strings, all decrypted by the same routine at a known address. To extract them in bulk you need to:

1. Identify the decryptor routine (the one that reads `[ARG_BUFF:4]` as a length, XORs `[ARG_BUFF+4:length]`, writes plaintext back, then `RET`s).
2. Find every `call` that targets it.
3. For each call site, walk backward to find the immediate-previous `call` — that's the *string-data loader* that wrote the encoded blob into `ARG_BUFF`.
4. Run the string-data loader through the emulator and read `ARG_BUFF` after it returns.

Steps 2 and 3 are the awkward part — IDA doesn't index "the call before this call" for you. The two helpers below do it.

## Find every call site of a known function

```python
import idautils
import ida_bytes
import idc

def get_xref_list(fn_addr):
    """Return every address in the binary that has an xref TO fn_addr."""
    return [addr.frm for addr in idautils.XrefsTo(fn_addr)]

# Example: 0x025A28 is the decryptor in the Guloader sample at
# SHA256 e3a8356689b97653261ea6b75ca911bc65f523025f15649e87b1aef0071ae107
xref_list = get_xref_list(0x025A28)
```

> **In Luc Nhan**: use the built-in `xrefs_to` tool for single lookups. For the full enumeration pattern, run the snippet above via `execute_python`.

## Walk backward to the string-data loader

Each string-data loader is the *previous* `call` instruction before the call to the decryptor. Walk backward from the call site until you hit a `call`:

```python
def get_string_fn(ptr_addr):
    """Starting from a `call decryptor`, walk backward and return the address
    of the previous `call` — that's the function that wrote encoded data
    into ARG_BUFF."""
    out = None
    limit_count = 0
    while limit_count < 10000:
        ptr_addr = idc.prev_head(ptr_addr)
        if ida_bytes.is_code(ida_bytes.get_full_flags(ptr_addr)):
            if idc.print_insn_mnem(ptr_addr) == 'call':
                out = idc.get_operand_value(ptr_addr, 0)
                break
        limit_count += 1
    return out
```

The 10 000-instruction bound is paranoia; in practice the loader is one or two instructions back.

## Wire it together

```python
import unicorn, unicorn.x86_const as x86
import struct

def xor_crypt(data, key):
    """Repeating-key XOR. Length-prefixed buffers come out plaintext; UTF-16
    strings need `.decode('utf-16')` after this."""
    out = bytearray(len(data))
    for i, b in enumerate(data):
        out[i] = b ^ key[i % len(key)]
    return bytes(out)

# 1) Collect every string-decryptor xref, and for each one find the loader.
xref_list = get_xref_list(DECRYPTOR_ADDR)
str_fn_list = []
for xref in xref_list:
    fn = get_string_fn(xref)
    if fn is not None:
        str_fn_list.append(fn)

# 2) Run each loader through Unicorn. After emu_start returns, ARG_BUFF
#    contains the encoded data and the function (which the loader then
#    internally calls) decrypts it — so emulate both. Or, if you've
#    already pulled the decryptor into its own emulation session, just
#    emulate the loader and pass its output buffer to the decryptor.
def emulate_loader(fn_addr):
    CODE = 0x10000
    STACK = 0x900000
    ARG_BUFF = 0x400000

    uc = unicorn.Uc(unicorn.UC_ARCH_X86, unicorn.UC_MODE_32)
    uc.mem_map(CODE, 0x300000, unicorn.UC_PROT_ALL)
    # uc.mem_write(CODE, sc_blob)   # shellcode bytes loaded earlier
    uc.mem_map(STACK - 0x100000, 0x200000, unicorn.UC_PROT_ALL)
    uc.reg_write(x86.UC_X86_REG_ESP, STACK)
    uc.reg_write(x86.UC_X86_REG_EBP, STACK)
    uc.mem_map(ARG_BUFF, 0x1000, unicorn.UC_PROT_ALL)

    # Fake-return sentinel -> emu_start stops on ret
    uc.mem_write(STACK, struct.pack('<I', 0xDEADBEEF))
    uc.emu_start(CODE + fn_addr, 0xDEADBEEF, 0, 0)

    raw = bytes(uc.mem_read(ARG_BUFF, 0x1000))
    length = struct.unpack('<I', raw[:4])[0]
    return raw[4:4 + length]

# 3) For each loader, emulate it, then XOR-decrypt (key is per-sample).
results = []
for fn in str_fn_list:
    try:
        enc = emulate_loader(fn)
        ptxt = xor_crypt(enc, key).replace(b'\x00', b'')
        results.append(ptxt.decode('utf-8', errors='replace'))
    except Exception:
        pass

for r in results:
    print(r)
```

The output of a typical Guloader binary looks like:

```
user32
psapi.dll
Msi.dll
Publisher
Skattekister138
OverOps146.70.147.12/vSFjv98.fla
SOFTWARE\AppDataLow\
windir=
\system32\
\syswow64\
iertutil.dll
wininet.dll
KERNELBASE.DLL
shell32
advapi32
C:\Program Files\Qemu-ga\qemu-ga.exe
C:\Program Files\qga\qga.exe
TEMP=
```

These are the imports + environment paths + C2 + sandbox-escape strings you'd report in a write-up. From here you can chase the `OverOps...` URL through DNS / passive-DNS to attribute the campaign.

## Guloader tips

- **The `call decryptor` itself isn't a candidate** — only the *previous* `call` (the loader) is. If `get_string_fn` returns the decryptor's own address, walk one more step back.
- **Loader sometimes is a `mov [ARG_BUFF], <encoded_addr>; call decryptor` sequence** rather than a separate function call. In that case the encoded address is already in IDA; read it directly with `idc.get_wide_dword`.
- **Identify the decryptor once, then use this script as a starting point** — different samples have different decryptor addresses but the xref + walk-back logic is invariant.

---

# Choosing between the three variants

| Situation | Use |
|---|---|
| Sample uses IAT (plain imports) | Not API hashing — use IAT analysis tools (`list_imports`) |
| Sample resolves APIs via hashes + `GetProcAddress` | Part B (annotation variant) — `calculate_hash` + export-enumeration dictionary |
| Sample uses both IAT and hashes | Combine: Part B handles the hashes; IAT analysis tools handle the static imports |
| Sample uses dynamic API resolution via `LoadLibrary` + `GetProcAddress` by name | Not API hashing at all — different problem domain |
| You need clean decompiler output (`call RealApi` instead of annotated `call ResolveApiByHash`) | Part C (binary-rewriting variant) — but requires a debug session for `Appcall.proto` |
| You need to bulk-process 30–60 call sites of a known decryptor | Part D (bulk enumeration) — pair with Part B's hash dictionary if the decryptor is hash-based |

---

# Reference links

## Part B (annotation variant)

- IIJ Research, *Effective Malware Analysis using Unicorn* — same directory as this skill's source repo.

## Part C (binary-rewriting variant)

- Outpost24 KrakenLabs, *Everything You Need To Know About LummaC2 Stealer* — the source article. Includes network-traffic screenshots, exfiltration routine before/after comparisons, MITRE ATT&CK mapping, targeted wallet/extension list.

## Part D (bulk enumeration)

- Companion to the VEH-style INT3 control flow emulation pattern (outside this reference).

## Sibling references

- `algorithm-reference.md` — high-level deobfuscation methodology.
- `cff-recovery.md` — sibling workflow for control-flow flattening recovery.
- `string-decryption.md` — sibling workflow for inline string decryptors.
- `self-restoring-binary.md` — sibling workflow for self-restoring binary deobfuscation.
- `tools.md` — reference for `emulate_code`, `xrefs_to`, `set_comment`, `execute_python`, `list_imports` parameter semantics.
