---
name: Deobfuscation
description: >-
  Systematic binary deobfuscation — string decryption (Conti-style, ADVobfuscator), API hashing
  resolution (MurmurHash2, LummaC2, Guloader), control flow flattening (CFF) removal, opaque
  predicate elimination, mixed boolean-arithmetic (MBA) simplification, bogus control flow,
  instruction substitution reversal, dead code removal, anti-disassembly fixes, and self-restoring
  binary deobfuscation.
  Trigger: deobfuscate, unobfuscate, deobfuscation, CFF, flatten, opaque predicate, MBA,
  obfuscated, OLLVM, Tigress, VMProtect, string decryption, junk code, bogus control flow,
  instruction substitution, anti-disassembly, ADVobfuscator, Conti, MurmurHash, MurmurHash2,
  LummaC2, Guloader, API hashing, hash resolver, self-restoring, pushfq, popfq
tags:
  - deobfuscation
  - obfuscation
  - cff
  - mba
  - opaque-predicates
  - hexrays
  - microcode
  - ollvm
  - tigress
  - string-decryption
  - bogus-control-flow
  - instruction-substitution
  - api-hashing
  - self-restoring
mode: plan
triggers:
  - deobfuscate
  - deobfuscation
  - unobfuscate
  - cff
  - control flow flattening
  - flatten
  - opaque predicate
  - mba
  - mixed boolean arithmetic
  - obfuscated
  - ollvm
  - tigress
  - vmprotect
  - string decryption
  - junk code
  - bogus control flow
  - instruction substitution
  - anti-disassembly
  - advobfuscator
  - conti
  - murmurhash
  - lummac2
  - guloader
  - api hashing
  - hash resolver
  - self-restoring
  - pushfq
---
# Deobfuscation Mode

Deobfuscate fully first. Analyze afterward.
**Never draw conclusions from obfuscated code — it misleads.**

Host-specific tools, API details, and full algorithm implementations are in the auto-loaded references:

- **IDA core**: `references/ida/tools.md` (available tools), `guide.md` (workflow & technique rules), `microcode-guide.md` (reading/writing microcode), `algorithm-reference.md` (recognition & methodology for CFF / opaque predicates / MBA / instruction substitution)
- **IDA methodology (emulation-centric)** — ported from external references, each file has a "How to apply in Luc Nhan" header:
  - `references/ida/cff-recovery.md` — emulation / pattern-matching approach to CFF recovery (VB2023 paper); complements the microcode block-optimizer approach in `algorithm-reference.md`
  - `references/ida/string-decryption.md` — inline stack-string decryptors (Conti `imul/idiv/0x7F` loop + ADVobfuscator `sub al, key` loop) with regex signatures and the stack-snapshot diff technique
  - `references/ida/self-restoring-binary.md` — `pushfq/popfq`-bracketed XOR deobfuscation; the `reg_write(RIP, ...)` capture-without-execute trick
  - `references/ida/api-hashing.md` — API-hash resolution workflows: annotation (IIJ/TorNet), binary-rewriting (LummaC2), bulk enumeration (Guloader)

**Read the host-specific tools reference first** to know what primitives are available.

## Order of Operations

### 1. String Decryption (always first — unlocks context)

Skip only if the user asked for a specific deobfuscation type.

Strings are the fastest path to understanding a binary. Encrypted strings signal intentional obfuscation.

1. Check for readable strings — very few in a large binary means strings are encrypted.
2. Find the decode stub: a small function called frequently, often before string use.
3. Decompile it and identify the algorithm (XOR, RC4, custom).
4. Use xrefs on the decode function to locate all call sites.
5. At each call site, trace arguments to extract encrypted data and key.
6. Prefer `emulate_code` / `resolve_emulated_string` to compute plaintext
   directly from the binary when the decoder is self-contained (no API
   calls, no unmodeled state, no internal branches leaving the stub). See
   `references/ida/tools.md` for register, memory-range, and exclusive-`stop_address` rules.
7. Fall back to `execute_python` reimplementation only for decoders that
   emulate cannot handle: those that touch external APIs, depend on
   captured/unmodeled state, or include control flow that leaves the
   proposed range.
8. Annotate decrypted strings at each call site (C2 addresses, file paths, registry keys, API names).
9. Rename the decode function (e.g., `decrypt_string`).

**Inline stack-string decryptors (Conti, ADVobfuscator)** — when the
encrypted bytes are built on the stack inside the routine itself (no
external encrypted blob), regex signature scanning is the fastest way to
discover all decryptor loops. See `references/ida/string-decryption.md`
for the Conti `\x8A\x44.{2,3}|\x8A\x84.{2,6}` egg, the ADVobfuscator
`[\x40-\x43\x46]\x83[\xf8-\xfb\xfe].[\x72\x7c].` egg, the 4-step
pipeline (regex → prologue slice → emulate → read), the `UC_ERR_EXCEPTION`
uninitialized-divisor fix, and the stack-snapshot diff technique for
discovering the runtime output offset.

**API hashing (MurmurHash2, LummaC2, Guloader)** — when the binary calls
APIs by hash rather than name, resolve them by emulating the hash
function to build a `{hash: name}` dictionary. See
`references/ida/api-hashing.md` for three variants: annotation-only
(IIJ/TorNet), binary-rewriting (LummaC2, requires debug session), and
bulk call-site enumeration (Guloader xref walker). The hash extraction
step uses `emulate_code` and reads `final_registers["eax"]` for the
result.

### 2. Structural Deobfuscation

**You are the deobfuscator.** Read the IL/pseudocode, understand the obfuscation, then use write primitives to undo it. Read, understand, modify, verify.

Work through these techniques in order — each one may reveal patterns hidden by the previous layer.

#### Control Flow Flattening (CFF)

A state machine dispatcher replaces linear control flow.

**What it looks like:**
- A loop with a back edge to a header block.
- The header compares a variable (state variable) against many constants.
- Each case body assigns a new constant to the state variable.
- At low optimization: state var may appear as memory stores (`[rbp-0x10].d = 0x25`), not register vars.

**How to remove:**
1. Identify the state variable — the variable compared most frequently against constants. **No entropy filtering** — state values may not look random.
2. Assume **multiple dispatcher blocks** — any block comparing the state variable is a dispatcher.
3. Map state to handler: the non-dispatcher jump target for each state value.
4. Rewire each handler to jump directly to its successor handler (bypass dispatcher).
5. NOP all state variable assignments (dispatcher bookkeeping).
6. Remove dead dispatcher blocks (the decompiler handles this automatically after rewiring).
7. Redecompile — the function should read as linear code.

#### Opaque Predicates

Conditions that always evaluate the same way.

**Forms:**
- Algebraic: `x * (x-1) % 2 == 0` (always true), `x^2 + x` is always even.
- Constant-returning functions hidden behind indirection.
- Environment checks that are constant at analysis time.

Check the decompiler output first — it may already resolve some to `if (true)`/`if (false)`.

**How to remove:**
- Always-true: force the branch to the taken target (convert to unconditional goto).
- Always-false: NOP the branch instruction entirely.
- The dead branch becomes unreachable — the decompiler prunes it automatically.
- For non-trivially-constant predicates, use Z3 via scripting to prove the predicate, then install an optimizer/transform to force it.

#### Bogus Control Flow (BCF)

Conditional jumps with opaque predicates branching to junk/cloned blocks. Common in OLLVM.

**What it looks like:**
- A conditional branch where one successor contains junk code (no real data flow contribution) or a clone of the original block with added noise.
- The opaque predicate ensures the junk path is never taken.

**How to remove:**
- Resolve the opaque predicate (same technique as above) — force the branch to the real successor.
- Dead block elimination handles the junk blocks automatically.

#### Mixed Boolean-Arithmetic (MBA)

Simple operations disguised as complex boolean-arithmetic expressions.

**Common identities:**
- `(x ^ y) + 2*(x & y)` = `x + y`
- `(x | y) - (x & ~y)` = `y`
- `~(~x & ~y)` = `x | y`
- `(x & 0xFF) | (x & ~0xFF)` = `x`
- `(x | y) + (x & y)` = `x + y` (alternative form)

**How to remove:**
- When both operands are constants: compute the result, replace with a mov/constant.
- When operands are symbolic: pattern-match known identities and replace with the simplified form.
- Work bottom-up (simplify inner expressions first).
- Apply `(1 << (size * 8)) - 1` bitmask when constant-folding to handle unsigned overflow.
- Multiple passes may be needed — MBA expressions can be nested.

#### Instruction Substitution

Simple operations replaced with equivalent complex sequences. Common in OLLVM.

**Common patterns (match and reverse):**
- `a + b` replaced by: `a - (-b)`, `(a ^ b) + 2*(a & b)`, `(a | b) + (a & b)`
- `a - b` replaced by: `a + (-b)`, `(a ^ b) - 2*(~a & b)`
- `a ^ b` replaced by: `(a | b) - (a & b)`, `(~a & b) | (a & ~b)`
- `a & b` replaced by: `(a | b) - (a ^ b)`, `~(~a | ~b)`
- `a | b` replaced by: `(a & b) + (a ^ b)`, `~(~a & ~b)`

**How to remove:** Pattern-match the complex sequence and replace with the original simple operation. Multiple passes may be needed (substitutions can be chained).

#### Junk Code / Dead Stores

Instructions computing values never used.

- Identify via def-use chains: if a variable is defined but has no uses before its next definition, the definition is dead.
- A `mov` to a variable that is immediately overwritten is a dead store — NOP it.
- After CFF removal, many state variable assignments become dead — clean them up.

#### Anti-Disassembly

Junk bytes inserted to confuse linear disassembly.

**Common patterns:**
- Junk bytes after unconditional jumps: `jmp +2; db 0xE8` (fake call prefix).
- Overlapping instructions exploiting x86 variable-length encoding.

**How to fix:** NOP the junk bytes at the byte level, or redefine function boundaries. Check disassembly first — the disassembler may already handle some patterns.

#### Self-Restoring Binaries (pushfq/popfq XOR brackets)

A class of obfuscation that **cannot be reversed with IL-level tools**:
code stays encrypted on disk *and* in memory; each block is XOR-decrypted
just before execution and re-encrypted immediately after. The
`pushfq/popfq` marker pair brackets each obfuscation window. See
`references/ida/self-restoring-binary.md` for the full workflow: a
`UC_HOOK_CODE` state-machine hook with the `reg_write(RIP, address+size)`
trick to capture plaintext bytes without executing them, plus a recursive
`call_addrs` worklist for nested obfuscated functions. **This workflow
requires `execute_python`** (custom Unicorn hooks are not exposed by
`emulate_code`); the technique fundamentally depends on intercepting
each instruction before it executes.

### 3. VM Boundary (if detected)

Not reversible with IL-level tools.
1. Identify: VM entry point, bytecode buffer, handler table address, handler count.
2. Document all addresses and the dispatch mechanism.
3. Focus deobfuscation on non-virtualized code paths.
4. Recommend specialized VM lifting tooling (e.g., Miasm, Triton).

> **Note**: self-restoring binaries (above) are sometimes misidentified as
> VM-protected because static analysis sees only ciphertext. Before
> escalating to "VM boundary", check for `pushfq/popfq` bracket markers —
> if present, the self-restoring workflow can recover the plaintext.

### 4. Post-Deobfuscation

- Redecompile all modified functions.
- Code should now be readable. If not, check for more obfuscation layers.
- Apply all annotations (renamed functions, decrypted string comments, recovered types).
- Proceed with normal analysis.

## Critical Rules

- **Read the IL before acting.** One read call beats 20 guesses.
- **String decryption ALWAYS comes first** (unless user specifies otherwise).
- **After each modification, redecompile to verify improvement.**
- **Iterative approach:** complex obfuscation needs multiple passes — install optimizer/transform, redecompile, check, refine.
- **If IL-level approach fails, fall back to byte-level patching.**
- **Never draw conclusions from obfuscated code.**
- **Check the host-specific reference files** for tool-specific rules (maturity guards, operand comparison, dirty marking).
