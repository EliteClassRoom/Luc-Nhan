# IDAPython offline docs bundle

This directory contains the bundled Hex-Rays IDAPython reference, used by
Rikugan's docs-reviewer subagent to verify API usage in `execute_python`
scripts.

**Right way to read these files** — call the tool:

```
lookup_idapython_doc(module="ida_typeinf")
```

This goes through Rikugan's path-traversal guard, gets logged in trace
output, and returns the raw RST source (~5–15 KB per module).

**Wrong way** — do NOT do this from agent scripts:

```python
import os

doc_path = os.path.join(..., "data", "idapython-docs", "ida_bytes.rst.txt")
with open(doc_path) as f:
    content = f.read()
```

That bypasses path-traversal protection, breaks the trace logging, and
guesses the install path (which is wrong on most systems).

## What's here

54 `.rst.txt` files, one per module (plus `MANIFEST.json`). Each file is
the raw Sphinx source for that module's API reference.

```
ida_api.rst.txt        ida_idp.rst.txt         ida_netnode.rst.txt
ida_auto.rst.txt       ida_kernwin.rst.txt     ida_offset.rst.txt
ida_bitname.rst.txt    ida_lines.rst.txt       ida_pro.rst.txt
ida_bytes.rst.txt      ida_lumina.rst.txt      ida_registry.rst.txt
ida_dbg.rst.txt        ida_moves.rst.txt       ida_search.rst.txt
ida_dirtree.rst.txt    ida_nalt.rst.txt        ida_segment.rst.txt
ida_elf.rst.txt        ida_name.rst.txt        ida_srclang.rst.txt
ida_enum.rst.txt       ida_netnode.rst.txt     ida_strlist.rst.txt
ida_expr.rst.txt       ida_offset.rst.txt      ida_struct.rst.txt
ida_fixup.rst.txt      ida_ua.rst.txt          ida_typeinf.rst.txt
ida_frame.rst.txt      ida_undo.rst.txt        ida_ua.rst.txt
ida_funcs.rst.txt      ida_xref.rst.txt
ida_fpro.rst.txt       init.rst.txt
ida_gdl.rst.txt        lumina_model.rst.txt
ida_hexrays.rst.txt
ida_ida.rst.txt        idaapi.rst.txt
ida_idd.rst.txt        idautils.rst.txt
                       idc.rst.txt
```

`init.rst.txt` is the module index. `MANIFEST.json` records per-file
SHA-256 hashes, byte sizes, fetch timestamps, and source URLs.

## Updating the bundle

To rebuild against the latest Hex-Rays upstream:

```bash
python scripts/build_idapython_docs.py
```

This is stdlib-only — no install required. Re-runs:

1. `GET https://python.docs.hex-rays.com/` → parse module list
2. For each module, `GET /_sources/<module>/index.rst.txt` → write
   atomic `.rst.txt` file
3. Write fresh `MANIFEST.json` with new SHA-256s + timestamps

To check for upstream drift without rebuilding:

```bash
python scripts/build_idapython_docs.py --verify
```

Exits 0 if the bundle is in sync, 1 if drift detected (local differs from
upstream), 2 if the network is unreachable.

## Why offline?

Hex-Rays docs serve HTML pages through bot protection that returns
`403 Forbidden` on deep-link paths (`/<module>/<func>.html`). The raw
RST sources at `/_sources/<module>/index.rst.txt` and module indexes
return `200 OK` because they bypass the JS challenge. The docs-reviewer
subagent used to waste ~1–2 turns retrying the broken HTML pattern
before self-recovering. The offline bundle eliminates that round-trip
and works on networks where Hex-Rays is blocked entirely.