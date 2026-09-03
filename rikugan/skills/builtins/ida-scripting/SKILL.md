---
name: IDA Scripting
description: Write IDAPython scripts — verified for IDA 9.x only. Loads the negative-knowledge anti-hallucination table (DO NOT USE APIs) and the symptom→fix map. ALWAYS load this skill before calling `execute_python`.
tags: [scripting, ida, python, automation, idapython, documentation]
author: Rikugan
version: 4.0
triggers:
  - idapython
  - ida python
  - write ida script
  - ida script
  - ida api
  - ida_bytes
  - ida_funcs
  - ida_hexrays
  - ida_typeinf
  - ida_name
  - ida_segment
  - ida_xref
  - ida_kernwin
  - idautils
  - ida_frame
  - idaapi
  - ida_ua
  - ida_nalt
  - ida_ida
  - ida_lines
  - idc module
  - idc
  - ida documentation
  - idapython documentation
  - ida docs
  - how do i read bytes
  - how to iterate functions
  - how to use decompiler
  - how to find xrefs
  - how to create structure
  - how to rename
  - domain api
  - ida_domain
  - ida 9.1
  - ida 9.
  - ida release notes
  - microcode
  - ctree
  - how do i get operands
  - how to read operands
  - how to decode instruction
  - get_operands
  - attributeerror ida
allowed_tools:
  - web_fetch
  - lookup_idapython_doc
  - execute_python
  - decompile_function
  - get_decompiler_variables
  - get_pseudocode
  - list_functions
  - search_functions
  - get_function_info
  - list_strings
  - search_strings
  - get_string_at
  - read_bytes
  - read_disassembly
  - read_function_disassembly
  - get_instruction_info
  - set_comment
  - set_function_comment
  - set_type
  - rename_function
  - rename_address
  - rename_variable
  - xrefs_to
  - xrefs_from
  - function_xrefs
---

## 🚨 Pre-Write Checklist — DO BEFORE CALLING `execute_python`

Before you write any IDAPython code, walk this list. If you skip it, the
static validator will block execution or the runtime will throw `AttributeError`.

1. **Confirm a built-in tool can't do it.** 60+ purpose-built tools cover
   rename, decompile, xrefs, strings, types. Check the tool list FIRST.
   `execute_python` is the LAST resort.
2. **Cite the API in your reasoning.** Before writing the call, say out loud:
   "I am about to call `<module>.<name>` — this exists in IDA 9.x because…"
   If you cannot cite the module's docs mentally, you don't actually know the API.
3. **Never invent convenience helpers.** IDA Python does NOT provide
   "get_all_operands()", "for_each_xref()", etc. If a one-liner you want
   doesn't exist, write the explicit version (allocate + iterate).
4. **Use modern modules over `idc`.** Prefer `ida_ua`, `ida_bytes`, `ida_funcs`,
   `ida_typeinf`. The `idc` module is legacy wrappers — they still work but
   smell of IDA 6.x.
5. **If you must call `execute_python`, write code that survives validation.**
   The static validator blocks known-bad APIs and warns on legacy ones. Use
   the DO NOT USE table below as a deny-list.

## 🚫 Hallucinated APIs — DO NOT USE

These do NOT exist in any version of IDA Python. If your code calls them,
it will fail with `AttributeError` at runtime. The static validator will
**block** execution.

| Hallucinated call | Use instead |
|---|---|
| `idaapi.get_operands(ea)` | `insn = ida_ua.insn_t(); ida_ua.decode_insn(insn, ea); insn.ops[i]` |
| `idaapi.get_instruction_operands(ea)` | `insn.ops[]` (see above) |
| `idaapi.get_insn_operands(ea)` | `insn.ops[]` |
| `idautils.GetOperands(...)` | `insn.ops[]` |
| `idaapi.op_for_each(...)` | Python `for op in insn.ops:` |
| `ida_struct.add_struc(...)` | `ida_typeinf.tinfo_t.create_udt(...)` (removed in IDA 9.x) |
| `ida_enum.add_enum(...)` | `ida_typeinf.tinfo_t` with `BTF_ENUM` (removed in IDA 9.x) |
| `idaapi.get_struct(name)` | `ida_typeinf.get_named_type(...)` then parse |
| `idc.AddStruc(...)` | `ida_typeinf.tinfo_t().create_udt(...)` |
| `ida_struct.del_struc(...)` | `ida_typeinf.remove_named_type(...)` (removed in IDA 9.x) |
| `ida_struct.get_struc(...)` | `ida_typeinf.get_named_type(...)` (removed in IDA 9.x) |
| `ida_enum.get_enum(...)` | `ida_typeinf.get_named_type(...)` then iterate (removed in IDA 9.x) |
| `idaapi.get_function_at(ea)` | `ida_funcs.get_func(ea)` — returns func_t or None |
| `idaapi.get_function_name(ea)` | `ida_funcs.get_func_name(ea)` or `ida_name.get_name(ea)` |
| `idc.AddEnum(...)` | `ida_typeinf.tinfo_t` with `BTF_ENUM` (removed in IDA 9.x) |

These DO exist but are **legacy/discouraged**. The validator will **warn**
but not block — prefer the modern equivalent.

| Legacy call | Modern equivalent |
|---|---|
| `idc.GetOperandValue(ea, n)` | `insn.ops[n].value` (after `decode_insn`) |
| `idc.GetOpnd(ea, n)` | `ida_lines.generate_disasm_line(ea, 0)` |
| `idc.GetOperandType(ea, n)` | `insn.ops[n].type` |
| `idc.NextHead(ea)` | `idautils.Heads(start, end)` generator |
| `idc.ScreenEA()` | `ida_kernwin.get_screen_ea()` |

## 🩺 Symptom → Fix Map

When the tool result is an `AttributeError` or your code doesn't import, look
up the symptom here. **Do not retry the same broken code** — rewrite using the
Fix column.

| Symptom | Cause | Fix |
|---|---|---|
| `AttributeError: module 'idaapi' has no attribute 'get_operands'` | AI hallucinated convenience function | Use `ida_ua.insn_t()` + `decode_insn` + iterate `insn.ops` |
| `AttributeError: module 'idaapi' has no attribute 'get_instruction_operands'` | Same family of hallucination | Same fix |
| `ImportError: No module named 'ida_struct'` | IDA 9.x removed it | Use `ida_typeinf` UDT API |
| `ImportError: No module named 'ida_enum'` | IDA 9.x removed it | Use `ida_typeinf` enum API |
| `AttributeError: 'NoneType' object has no attribute 'start_ea'` | `ida_funcs.get_func(ea)` returned `None` | Always check `if func is None: return` |
| `ida_hexrays.DecompilationFailure` | Decompiler can't handle this function | Wrap in `try/except`, fall back to disassembly |
| `NameError: name 'BADADDR' is not defined` | Constant scope issue | Use `idaapi.BADADDR` (already pre-imported in the namespace) |

---

Task: Write IDAPython scripts with `execute_python`. Use modern `ida_*` modules;
avoid the legacy `idc` module where a direct `ida_*` call exists.

## Environment

`execute_python` pre-imports: `idaapi`, `idautils`, `idc`, `ida_funcs`, `ida_name`,
`ida_bytes`, `ida_segment`, `ida_typeinf`, `ida_nalt`, `ida_xref`, `ida_kernwin`,
`ida_domain` (when installed, IDA 9.1+). Note: `ida_struct`/`ida_enum` are NOT
available (removed in IDA 9) — use `ida_typeinf`. Full stdlib
except process-execution modules (`subprocess`, `os.system`, `os.exec*` — blocked).
`print()` output is captured and returned.

## Module Router

| Task | Module | Key items |
|------|--------|-----------|
| Bytes/memory | `ida_bytes` | `get_bytes`, `patch_bytes`, `get_byte/word/dword/qword`, `get_strlit_contents`, `get_full_flags`, `is_code`, `is_data` |
| Functions | `ida_funcs` | `func_t`, `get_func`, `add_func`, `get_func_name`, `get_next_func` |
| Names | `ida_name` | `set_name`, `get_name`, `demangle_name`, `get_name_ea`, `SN_CHECK` |
| Types | `ida_typeinf` | `tinfo_t`, `udt_type_data_t`, `udm_t`, `apply_tinfo`, `apply_cdecl`, `parse_decl` |
| Decompiler | `ida_hexrays` | `decompile`, `cfunc_t`, `lvar_t`, `ctree_visitor_t`, `mba_t` |
| Segments | `ida_segment` | `segment_t`, `getseg`, `get_segm_by_name` |
| Xrefs | `ida_xref` | `xrefblk_t`, `add_cref`, `add_dref`, `XREF_ALL` |
| Instructions | `ida_ua` | `insn_t`, `op_t`, `decode_insn` |
| Stack frames | `ida_frame` | `get_func_frame`, `define_stkvar` |
| Iteration | `idautils` | `Functions`, `Heads`, `XrefsTo`, `XrefsFrom`, `CodeRefsTo`, `DataRefsTo`, `Strings`, `Names`, `Segments` |
| UI/dialogs | `ida_kernwin` | `msg`, `ask_str`, `ask_yn`, `jumpto`, `get_screen_ea`, `Choose` |
| Database info | `ida_ida` | `inf_get_procname`, `inf_is_64bit`, `inf_get_min_ea`, `inf_get_max_ea` |
| Analysis | `ida_auto` | `auto_wait`, `plan_and_wait` |
| Flow graphs | `ida_gdl` | `FlowChart`, `BasicBlock` |
| Register tracking | `ida_regfinder` | `find_reg_value`, `reg_value_info_t` |
| Persistent storage | `ida_netnode` | `netnode`, `hashset`, `hashstr`, `altset`, `setblob` |

## Core Patterns (verified for IDA 9.x)

```python
# Iterate functions
for ea in idautils.Functions():
    name = ida_funcs.get_func_name(ea)
    func = ida_funcs.get_func(ea)        # func_t or None — check before .start_ea
    # func.start_ea, func.end_ea, func.flags

# Iterate instructions in a function
for head in idautils.FuncItems(func_ea):
    insn = ida_ua.insn_t()
    if ida_ua.decode_insn(insn, head):
        print(f"{head:#x}: itype={insn.itype}")

# Cross-references
for xref in idautils.XrefsTo(ea, ida_xref.XREF_ALL):
    print(f"{xref.frm:#x} -> {xref.to:#x} type={xref.type}")
for ref in idautils.CodeRefsTo(ea, False):   # False = no flow
    ...
for ref in idautils.DataRefsTo(ea):
    ...

# Read / write bytes
data = ida_bytes.get_bytes(ea, size)
val = ida_bytes.get_qword(ea)
s = ida_bytes.get_strlit_contents(ea, -1, ida_nalt.STRTYPE_C)  # returns bytes
ida_bytes.patch_bytes(ea, b"\x90\x90")

# Names
name = ida_name.get_name(ea)
ida_name.set_name(ea, "new_name", ida_name.SN_CHECK)

# Decompile (ALWAYS wrap — raises DecompilationFailure)
try:
    cfunc = ida_hexrays.decompile(ea)
    print(cfunc)
    for lvar in cfunc.lvars:
        print(f"{lvar.name}: {lvar.type()}")
except ida_hexrays.DecompilationFailure:
    pass

# Walk the ctree (decompiled AST)
class CallVisitor(ida_hexrays.ctree_visitor_t):
    def __init__(self):
        super().__init__(ida_hexrays.CV_FAST)
    def visit_expr(self, e):
        if e.op == ida_hexrays.cot_call:
            print(f"Call at {e.ea:#x}")
        return 0
cfunc = ida_hexrays.decompile(ea)
CallVisitor().apply_to(cfunc.body, None)

# Wait for auto-analysis before reading bulk results
ida_auto.auto_wait()

# Strings
for s in idautils.Strings():
    print(f"{s.ea:#x}: {str(s)}")

# Persistent storage (netnodes)
node = ida_netnode.netnode("$my_data", 0, True)
node.hashset("key", "value")
print(node.hashstr("key"))
```

## Types — IDA 9.x (CRITICAL: offsets/sizes in BITS)

`ida_struct` and `ida_enum` were **removed** in IDA 9. Use `ida_typeinf`.
`udm_t.offset` and `udm_t.size` are in **BITS** — multiply byte values by 8.

```python
# Build a struct — concise way (IDA 9.x). add_udm's 'type' arg accepts a C
# declaration string, sidestepping the BT_INT32/BTF_INT32 ambiguity entirely.
# Offsets are in BITS — multiply byte values by 8.
tif = ida_typeinf.tinfo_t()
tif.create_udt(ida_typeinf.udt_type_data_t(), ida_typeinf.BTF_STRUCT)  # empty struct shell
tif.add_udm("field1", "int", offset=0 * 8)        # byte 0 -> bit 0
tif.add_udm("field2", "char *", offset=4 * 8)     # pointer field at byte 4
tif.set_named_type(ida_typeinf.get_idati(), "MyStruct", ida_typeinf.NTF_REPLACE)

# Build a struct — explicit way (full control over size/flags per member)
udt = ida_typeinf.udt_type_data_t()
m = ida_typeinf.udm_t()
m.name = "field1"
t = ida_typeinf.tinfo_t()
t.create_simple_type(ida_typeinf.BT_INT32)   # NOT tinfo_t(BT_INT32) — unreliable
m.type = t
m.offset = 0 * 8    # byte 0 -> bit 0
m.size = 4 * 8      # 4 bytes -> 32 bits
udt.push_back(m)
tif = ida_typeinf.tinfo_t()
tif.create_udt(udt, ida_typeinf.BTF_STRUCT)
tif.set_named_type(ida_typeinf.get_idati(), "MyStruct", ida_typeinf.NTF_REPLACE)

# Iterate struct members
for udm in tif.iter_struct():
    print(f"  {udm.name}: {udm.type.dstr()} @ bit {udm.offset}")

# Build a tinfo_t from a C declaration — avoids manual BT_*/BTF_* construction.
# Constructor parses the declaration directly (til defaults to get_idati()).
fnptr_t = ida_typeinf.tinfo_t("int (*)(void *, size_t)")   # function pointer
# Or step-by-step via parse() (also takes til=None, pt_flags=0):
arr_t = ida_typeinf.tinfo_t()
arr_t.parse("char[16]")                                    # fixed array type

# Apply a C declaration directly (for well-known C types)
ida_typeinf.apply_cdecl(ida_typeinf.get_idati(), ea, "int __cdecl func(int a, char *b)")

# Apply a tinfo_t
ida_typeinf.apply_tinfo(ea, tif, ida_typeinf.TINFO_DEFINITE)
```

## Domain API (IDA 9.1+, optional)

High-level, Pythonic layer on top of IDAPython. In the namespace when the
`ida-domain` package is installed (`import ida_domain` succeeds); otherwise fall
back to classic `ida_*` modules.

```python
from ida_domain import Database as db
for f in db.functions: print(f.name)
db.names[ea] = "new_name"          # rename
for x in db.xrefs.to(ea): print(x)
data = db.read(ea, size)
cfunc = db.decompile(ea)
```

## Critical Rules

- `ida_funcs.get_func()` returns `None` if no function — check before `.start_ea`.
- `BADADDR` via `idaapi.BADADDR` — always compare against it.
- `ida_hexrays.decompile()` raises `DecompilationFailure` — always wrap in try/except.
- `ida_bytes.get_strlit_contents()` returns `bytes`, not `str` — decode if needed.
- IDA 9 removed `ida_struct`/`ida_enum` → `ida_typeinf`. `get_inf_structure()` → `inf_get_*()`.
- `udm_t.offset`/`udm_t.size` in BITS. Use `create_simple_type()`, never `tinfo_t(BT_*)`.
- Pass `NTF_REPLACE` to `set_named_type()` when redefining.
- Process execution blocked. Static analysis only.

## When to fetch more

The deep static reference below (`## Reference: api-reference.md`) covers ctree,
microcode, types, xrefs, hooks, and netnodes exhaustively. For per-module
references, **always try the offline docs tool first**:

```
lookup_idapython_doc(module="ida_<module>")
```

Reads from the plugin's bundled docs at `data/idapython-docs/<module>.rst.txt`.
The bundle covers 54 modules (~95% of common usage); the tool returns a clear
"Module not in offline bundle" error with available modules if it misses.

For **point lookups** (e.g. "does `ida_typeinf.apply_cdecl` exist?"), use the
`name` parameter to filter to just that entry:

```
lookup_idapython_doc(module="ida_typeinf", name="apply_cdecl")
```

Returns ~20 lines of context around each match — much cheaper than reading
200 KB of RST just to verify one function. Use this **instead of**
executing probe code via `execute_python` (e.g. `hasattr(idc, 'X')`): the
docs tool needs no user approval, while `execute_python` always does.

Common modules: `ida_typeinf`, `ida_name`, `idautils`, `ida_hexrays`,
`ida_frame`, `ida_funcs`, `ida_bytes`, `ida_xref`, `ida_segment`,
`ida_kernwin`, `ida_ua`, `idc`, `idaapi`.

> **Fallback to online only after offline fails.** Use `web_fetch` ONLY when:
>
> 1. `lookup_idapython_doc` returned "Module not in offline bundle" (module
>    name is not in our 54-module bundle — common for rare modules like
>    `ida_pro`, `ida_lumina`, etc.), OR
> 2. You consulted the offline docs but the verification still has gaps
>    (specific function/parameter isn't documented offline, or docs are
>    ambiguous). Read the relevant section first before falling back.
>
> Do **not** use `web_fetch` as a first attempt — preferring online when
> offline would have worked wastes time and risks 403 errors.
>
> Fallback URL pattern:
> `web_fetch(url="https://python.docs.hex-rays.com/_sources/ida_<module>/index.rst.txt", format="text")`
> Note: this is the raw RST source format; deep-link HTML pages return 403.

For IDA 9.x migration details, fetch the porting guide:
`https://docs.hex-rays.com/developer/idapython/idapython-porting-guide-ida-9`
