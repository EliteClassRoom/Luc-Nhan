# Reference load order

The loader concatenates `SKILL.md` with every `.md` file in this directory, and
`sanitize_skill_body()` then hard-caps the assembled body at `SKILL_MAX_CHARS` (50_000
characters).  This skill is ~110 KB assembled, so **the order below decides what reaches the
model**:

1. `api-reference.md` — ctree, microcode, xrefs, hooks, netnodes, types: the lookup table the
   agent needs while writing scripts.
2. `idapython-porting-guide-ida-9.md` — 84 KB porting guide.  It does **not** fit in the prompt;
   read it on demand with `read_skill_reference(skill="ida-scripting",
   file="idapython-porting-guide-ida-9.md", offset=N)`.

Files that are not listed load after the listed ones, alphabetically.  Parsed by
`lucnhan/skills/loader.py::_read_reference_order`; this file is never injected.
