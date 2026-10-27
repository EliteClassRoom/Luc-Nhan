# Reference load order

The loader concatenates every `.md` file in this directory into the skill body, and
`lucnhan.core.sanitize.sanitize_skill_body()` then hard-caps that body at
`SKILL_MAX_CHARS = 50_000`.  The assembled deobfuscation body is far larger than the cap, so
**the order below decides which references actually reach the model**.  Crash-prevention rules
and the tool catalogue come first; long technique case studies come last.

Parsed by `lucnhan/skills/loader.py::_read_reference_order` (one file name per line, bullets and
inline `#` comments allowed).  This file is never injected.

1. `tools.md` — which primitives exist (`get_microcode`, `nop_microcode`,
   `install_microcode_optimizer`, `emulate_code`, `execute_python`); every later step depends on it.
2. `guide.md` — the "Critical Rules" table (maturity guard, `EQ_IGNSIZE`, dirty marking) plus the
   Path A (microcode optimizers) / Path B (ida-domain hooks) decision table.
3. `microcode-guide.md` — microcode read/write idioms and the `MMAT_LVARS` prohibition.
4. `algorithm-reference.md` — per-technique methodology (CFF, opaque predicates, MBA, bogus
   control flow, instruction substitution).
5. `cff-recovery.md` — emulation / pattern-matching CFF recovery (large, technique-specific).
6. `self-restoring-binary.md` — the `pushfq`/`popfq` workflow (requires `execute_python`).
7. `string-decryption.md` — inline stack-string decryptors (Conti, ADVobfuscator) with long
   disassembly case studies.
8. `api-hashing.md` — the largest file; annotation / binary-rewrite / bulk-enumeration variants.

Files that are not listed load after the listed ones, alphabetically.
