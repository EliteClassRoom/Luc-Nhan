"""Database-level tools: segments, imports, exports."""

from __future__ import annotations

import importlib
from typing import Annotated

from ...core.logging import log_debug
from ...tools.base import parse_addr, tool
from ...tools.value_format import bytes_needed_for_type, format_global_value

try:
    ida_ida = importlib.import_module("ida_ida")
    ida_name = importlib.import_module("ida_name")
    ida_nalt = importlib.import_module("ida_nalt")
    ida_segment = importlib.import_module("ida_segment")
    idaapi = importlib.import_module("idaapi")
    idautils = importlib.import_module("idautils")
    idc = importlib.import_module("idc")
except ImportError as e:
    log_debug(f"IDA modules not available: {e}")


@tool(category="database")
def list_segments() -> str:
    """List every segment defined in the IDB with its address range, size, and permissions.

    Output format is one line per segment:
    `  <name>  <start>-<end>  (<size> bytes)  <perms>` where perms is a
    concatenated R/W/X string derived from the segment permission bits.

    Use this for binary layout reconnaissance — identifying packed or
    non-standard sections (e.g. .UPX0, .idata, .rsrc), or filtering on
    RWX/RE/WE permissions to find executable data or writable code.
    For raw bytes inside a specific segment, follow up with read_bytes
    on the segment's start address; for symbols inside a segment, use
    search_functions with a substring query.
    """

    lines = ["Segments:"]
    for seg_ea in idautils.Segments():
        name = idc.get_segm_name(seg_ea)
        end = idc.get_segm_end(seg_ea)
        size = end - seg_ea
        perms = ""
        seg = ida_segment.getseg(seg_ea)
        if seg:
            perms = ""
            if seg.perm & 4:  # R
                perms += "R"
            if seg.perm & 2:  # W
                perms += "W"
            if seg.perm & 1:  # X
                perms += "X"
        lines.append(f"  {name:16s}  0x{seg_ea:x}\u20130x{end:x}  ({size:#x} bytes)  {perms}")
    return "\n".join(lines)


@tool(category="database")
def list_imports() -> str:
    """List every imported function, grouped by source module (DLL).

    Output format groups imports under `[module_name]` headers with
    per-module counts, then `0xADDR  <name>` (or `ordinal #N` for
    ordinal-only entries). Each module shows up to 50 entries; larger
    modules are truncated with a `... and N more` line.

    This is the canonical first-recon tool for capability mapping
    (network, crypto, file, process APIs). If you need a filtered view
    — e.g. only crypto-related imports, or only imports from a single
    DLL — prefer search_imports or imports_by_module instead of writing
    a script with ida_nalt.enum_import_names. The dedicated tools avoid
    the user-approval round-trip that execute_python requires.
    """

    lines = ["Imports:"]
    nimps = ida_nalt.get_import_module_qty()
    for i in range(nimps):
        mod_name = ida_nalt.get_import_module_name(i)
        entries: list = []

        def _cb(ea, name, ordinal):
            if name:
                entries.append(f"    0x{ea:x}  {name}")  # noqa: B023
            else:
                entries.append(f"    0x{ea:x}  ordinal #{ordinal}")  # noqa: B023
            return True

        ida_nalt.enum_import_names(i, _cb)
        lines.append(f"  [{mod_name}] ({len(entries)} imports)")
        lines.extend(entries[:50])
        if len(entries) > 50:
            lines.append(f"    ... and {len(entries) - 50} more")
    return "\n".join(lines)


@tool(category="database")
def search_imports(
    query: Annotated[str, "Substring to search for in import names (case-insensitive)"],
    limit: Annotated[int, "Max number of matches to return"] = 20,
) -> str:
    """Search every imported function across all modules for a name containing *query*.

    Use this when you need a filtered subset of imports — for example
    "show me every crypto-related API", or "find CreateFile". The
    search is a case-insensitive substring match on the import name.

    Output format: a header line with the match count, followed by
    `0xADDR  [module]  <name>` lines (capped at *limit*). Returns a
    clear "No imports matching ..." message when nothing matches, so
    the LLM does not silently treat an empty list as a hit.
    """

    q = query.lower()
    matches: list[tuple[str, int, str]] = []  # (module, ea, display)

    def _cb(ea: int, name: str | None, ordinal: int) -> bool:
        if not name:
            return True
        if q in name.lower():
            matches.append((mod_name, ea, name))
        return True

    nimps = ida_nalt.get_import_module_qty()
    for i in range(nimps):
        mod_name = ida_nalt.get_import_module_name(i) or f"module_{i}"
        ida_nalt.enum_import_names(i, _cb)

    if not matches:
        return f"No imports matching '{query}'"

    lines = [f"Found {len(matches)} import(s) matching '{query}':"]
    for mod_name, ea, name in matches[:limit]:
        lines.append(f"  0x{ea:x}  [{mod_name}]  {name}")
    if len(matches) > limit:
        lines.append(f"  ... and {len(matches) - limit} more")
    return "\n".join(lines)


@tool(category="database")
def imports_by_module(
    module_name: Annotated[str, "Module (DLL) name to filter by, e.g. 'kernel32' or 'kernel32.dll'"],
    limit: Annotated[int, "Max number of imports to return"] = 50,
) -> str:
    """Return imports from a single module (DLL).

    *module_name* is matched as a case-insensitive substring against the
    DLL name in the import table, so both 'kernel32' and 'kernel32.dll'
    work. If multiple modules match, the first one wins; the response
    includes the resolved name so the LLM can disambiguate.

    When the requested module is not present, the response lists every
    available module so the LLM can recover and pick a different one
    instead of looping with the same query.
    """

    target = module_name.lower()
    nimps = ida_nalt.get_import_module_qty()

    available: list[str] = []
    found_idx = -1
    found_name = ""
    for i in range(nimps):
        name = ida_nalt.get_import_module_name(i) or f"module_{i}"
        available.append(name)
        if target in name.lower() and found_idx < 0:
            found_idx = i
            found_name = name

    if found_idx < 0:
        avail = ", ".join(available) if available else "(no modules)"
        return f"Module '{module_name}' not found. Available modules: {avail}"

    entries: list[str] = []
    found_idx_ref = found_idx

    def _cb(ea: int, name: str | None, ordinal: int) -> bool:
        if name:
            entries.append(f"    0x{ea:x}  {name}")
        else:
            entries.append(f"    0x{ea:x}  ordinal #{ordinal}")
        return True

    ida_nalt.enum_import_names(found_idx_ref, _cb)

    if not entries:
        return f"Module '{found_name}' has no imports"

    lines = [f"Imports from {found_name} ({len(entries)} entries):"]
    lines.extend(entries[:limit])
    if len(entries) > limit:
        lines.append(f"    ... and {len(entries) - limit} more")
    return "\n".join(lines)


@tool(category="database")
def list_exports() -> str:
    """List exported functions and symbols, ordered by their export-table index.

    Output format is one line per entry: `0xADDR  <name>`. Up to 200
    entries are returned; the rest are silently truncated.

    Use this when reviewing the binary's public surface area (PE exports,
    ELF dynamic symbols). If you need a filtered list — by name
    substring or by prefix — use search_exports instead of writing a
    script with idautils.Entries().
    """

    lines = ["Exports:"]
    for i, (_, _, ea, name) in enumerate(idautils.Entries()):
        lines.append(f"  0x{ea:x}  {name}")
        if i >= 200:
            lines.append("  ... (truncated)")
            break
    return "\n".join(lines)


@tool(category="database")
def get_binary_info() -> str:
    """Return high-level metadata about the loaded binary (processor, bitness, entry point, address range, file type, total function count).

    The on-disk file name is intentionally NOT emitted — file names are
    untrusted labels and must not influence analysis. Conclusion about
    the binary's purpose, family, or behavior must come from code, imports,
    xrefs, disassembly, and decompilation.

    Output is plain text, one fact per line:
    `Processor:`, `Bits:`, `Entry point:`, `Min/Max address:`,
    `File type:`, `Functions: <N>`.

    This is the right tool for the first turn of any new analysis —
    it gives you the binary's processor, layout, and scale in one call.
    For per-function details, follow up with list_functions (paginated)
    or search_functions (substring match). For strings, use
    list_strings; for imports/exports, use list_imports/list_exports.
    Never reimplement these with execute_python.
    """

    lines: list[str] = []
    try:
        lines.append(f"Processor: {ida_ida.inf_get_procname()}")
        lines.append(f"Bits: {ida_ida.inf_get_app_bitness()}")
        lines.append(f"Entry point: 0x{ida_ida.inf_get_start_ea():x}")
        lines.append(f"Min address: 0x{ida_ida.inf_get_min_ea():x}")
        lines.append(f"Max address: 0x{ida_ida.inf_get_max_ea():x}")
    except AttributeError:
        lines.append("Processor: (unavailable)")  # IDA API not supported

    try:
        lines.append(f"File type: {idaapi.get_file_type_name()}")
    except AttributeError as e:
        log_debug(f"get_binary_info: get_file_type_name unavailable: {e}")

    func_count = sum(1 for _ in idautils.Functions())
    lines.append(f"Functions: {func_count}")

    return "\n".join(lines)


@tool(category="database")
def read_bytes(
    address: Annotated[str, "Start address (hex string)"],
    size: Annotated[int, "Number of bytes to read"] = 64,
) -> str:
    """Read raw bytes from the IDB at the given address and return a hex + ASCII dump.

    Output format is the standard hexdump with 16 bytes per row:
    `  0xADDR  HH HH HH ...  HH HH  |ascii|` (note the double-space gap
    that splits each row into two 8-byte groups for readability).

    The `size` parameter is capped at 1024 bytes per call; for larger
    ranges, make multiple calls with progressively advancing addresses.
    For interpreting a known global value with a type hint (pointer,
    string, integer), prefer read_global_value — it formats the data
    for you instead of returning raw hex.
    """

    _MAX_READ_BYTES = 1024
    ea = parse_addr(address)
    size = int(size)
    if size > _MAX_READ_BYTES:
        size = _MAX_READ_BYTES

    lines = []
    for off in range(0, size, 16):
        row_ea = ea + off
        hex_parts = []
        ascii_parts = []
        for j in range(16):
            if off + j >= size:
                hex_parts.append("  ")
                ascii_parts.append(" ")
            else:
                b = idc.get_wide_byte(row_ea + j)
                hex_parts.append(f"{b:02x}")
                ascii_parts.append(chr(b) if 0x20 <= b < 0x7F else ".")
        hex_str = " ".join(hex_parts[:8]) + "  " + " ".join(hex_parts[8:])
        ascii_str = "".join(ascii_parts)
        lines.append(f"  0x{row_ea:08x}  {hex_str}  |{ascii_str}|")
    return "\n".join(lines)


# --- Global-value inspection ----------------------------------------------
# Helpers + read_global_value ported from the fork; format_global_value /
# bytes_needed_for_type live in tools.value_format so the formatting logic
# is shared and tested in one place rather than re-implemented per host.


def _resolve_addr_or_name(value: str) -> int:
    """Resolve *value* as a hex address, falling back to a symbol name."""
    try:
        return parse_addr(value)
    except (TypeError, ValueError):
        ea = ida_name.get_name_ea(idc.BADADDR, value)
        if ea == idc.BADADDR:
            raise ValueError(f"Unknown address or name: {value}") from None
        return ea


def _pointer_size() -> int:
    """Return the binary's pointer width in bytes (2/4/8)."""
    # inf_get_app_bitness() (IDA >=7.6) returns 16/32/64 directly — it replaces
    # the never-existent inf_is_32bit() and the removed get_inf_structure().
    return {16: 2, 32: 4, 64: 8}.get(ida_ida.inf_get_app_bitness(), 8)


def _read_raw_bytes(ea: int, size: int) -> bytes:
    """Read *size* bytes starting at *ea* via the IDA byte API."""
    return bytes(idc.get_wide_byte(ea + i) & 0xFF for i in range(max(0, size)))


def _resolve_pointer_name(ea: int) -> str:
    """Return the symbol name at *ea* (empty for null/invalid addresses)."""
    if ea in (0, idc.BADADDR):
        return ""
    return ida_name.get_name(ea) or ""


@tool(category="database")
def read_global_value(
    address: Annotated[str, "Global/data address or symbol name"],
    type_hint: Annotated[
        str,
        "auto, u8/i8/u16/i16/u32/i32/u64/i64, ptr, string, utf16, or bytes",
    ] = "auto",
    size: Annotated[int, "Bytes to inspect for auto/string/bytes; 0 selects a sensible default"] = 0,
) -> str:
    """Read and interpret a global variable or data value with a type-aware formatter.

    Accepts either a hex address (`0x401000`) or a symbol name
    (`g_pConfigStart`). The `type_hint` controls how the bytes are
    interpreted:
      - `auto` — infer from context (default; size 0 picks a sensible width)
      - `u8/i8/u16/i16/u32/i32/u64/i64` — fixed-width integer
      - `ptr` — pointer with auto-resolved target symbol
      - `string` / `utf16` — null-terminated string of the given encoding
      - `bytes` — raw hex bytes

    Use this when you need to read a single global with structure; for
    raw byte ranges without interpretation, use read_bytes instead.
    """

    ea = _resolve_addr_or_name(address)
    pointer_size = _pointer_size()
    read_size = bytes_needed_for_type(type_hint, pointer_size, requested_size=size)
    data = _read_raw_bytes(ea, read_size)
    return format_global_value(
        address=ea,
        data=data,
        pointer_size=pointer_size,
        type_hint=type_hint,
        name=ida_name.get_name(ea) or "",
        resolve_pointer=_resolve_pointer_name,
    )
