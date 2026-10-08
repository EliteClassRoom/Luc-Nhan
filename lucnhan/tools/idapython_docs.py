"""Offline IDAPython docs lookup — reads from bundled lucnhan/data/idapython-docs/.

This is the runtime counterpart of scripts/build_idapython_docs.py. Once
the bundle is built and committed, this tool serves IDAPython docs to the
LLM agent with zero network dependency.

Replaces web_fetch(url=...python.docs.hex-rays.com/_sources/...) in
documentation fetches.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

from ..core.errors import ToolError
from .base import tool

DOCS_DIR: Path = Path(__file__).resolve().parent.parent / "data" / "idapython-docs"

#: Module names are [a-z0-9_]+ per spec. Reject anything else.
_MODULE_NAME_RE: re.Pattern[str] = re.compile(r"^[a-z0-9_]+$")

#: Default pagination — fits under TOOL_RESULT_TRUNCATE_LEN (~8000 chars).
DEFAULT_LIMIT: int = 7400
MAX_LIMIT: int = 7600


def _validate_module_name(module: str) -> str | None:
    """Return sanitized module name or None if invalid (path-traversal reject)."""
    if not module or not _MODULE_NAME_RE.match(module):
        return None
    return module


def _format_missing_module_error(module: str) -> str:
    """Build the user-facing error message when a module is not in the bundle."""
    available: list[str] = []
    if DOCS_DIR.is_dir():
        for p in sorted(DOCS_DIR.glob("*.rst.txt")):
            name = p.stem.removesuffix(".rst")
            if _validate_module_name(name):
                available.append(name)

    shown = ", ".join(available[:20])
    total = len(available)
    return (
        f"[Module '{module}' not found in offline bundle]\n"
        f"Available modules ({total}): {shown}"
        f"{'...' if total > 20 else ''}\n"
        f"Tip: run scripts/build_idapython_docs.py to refresh, "
        f"or fall back to web_fetch() for this module."
    )


#: Lines of context returned around each `name` match in point-lookups.
#: Keeps the response small enough to fit under tool-truncation limit even
#: with multiple matches, while still preserving enough surrounding context
#: to read the full directive (signature + brief description).
_POINT_LOOKUP_CONTEXT_LINES: int = 20


def _extract_section_around(content: str, name: str) -> str | None:
    """Find every line containing ``name`` and return a context window around it.

    The RST files in the bundle are Sphinx-generated; function/class
    directives span multiple lines. Returning 20 lines of context (~20
    lines each side) covers most directives without making the response
    too large.

    If multiple lines match, all are returned separated by ``...``.
    Returns ``None`` if no line matches.
    """
    lines = content.splitlines(keepends=True)
    half = _POINT_LOOKUP_CONTEXT_LINES // 2
    sections: list[str] = []
    seen_ranges: list[tuple[int, int]] = []

    for i, line in enumerate(lines):
        if name not in line:
            continue
        start = max(0, i - half)
        end = min(len(lines), i + half + 1)
        # Skip if this range overlaps a previously captured range
        if any(start < prev_end and end > prev_start for prev_start, prev_end in seen_ranges):
            continue
        sections.append("".join(lines[start:end]))
        seen_ranges.append((start, end))

    if not sections:
        return None
    return "\n...\n".join(sections)


@tool(
    name="lookup_idapython_doc",
    category="documentation",
    mutating=False,
    timeout=5.0,
)
def lookup_idapython_doc(
    module: Annotated[
        str,
        "Module name (e.g. 'ida_typeinf', 'idautils', 'ida_hexrays'). Must match `[a-z0-9_]+`.",
    ],
    name: Annotated[
        str,
        "Optional: filter to a specific function/class name within the module. "
        "Returns ~20 lines of context around each match. Use this for point-lookups "
        "instead of `hasattr(idc, 'X')` or `inspect.signature()` — no execute_python "
        "user-approval needed. Example: lookup_idapython_doc(module='ida_typeinf', name='apply_cdecl').",
    ] = "",
    offset: Annotated[int, "Character offset for pagination (0 = beginning)."] = 0,
    limit: Annotated[
        int,
        f"Max characters to return (default {DEFAULT_LIMIT}, max {MAX_LIMIT}).",
    ] = DEFAULT_LIMIT,
) -> str:
    """Look up an IDAPython module's documentation from the bundled offline bundle.

    Reads from ``lucnhan/data/idapython-docs/<module>.rst.txt`` — works
    without network access. Use this BEFORE web_fetch against
    python.docs.hex-rays.com because that site is bot-protected
    (403 Forbidden on deep-link HTML pages).

    Two modes:
    - Pass only ``module`` (and optional ``offset``/``limit``) to read the
      full module reference, paginated.
    - Pass ``module`` AND ``name`` to filter to ~20 lines of context
      around each occurrence of ``name`` within the module — much cheaper
      than reading 200 KB of RST just to confirm one function exists.

    Returns raw RST content (same format as Sphinx source files).
    """
    safe = _validate_module_name(module)
    if safe is None:
        return f"[Error] invalid module name: {module!r}"

    if offset < 0:
        offset = 0
    if limit < 1:
        limit = 1
    if limit > MAX_LIMIT:
        limit = MAX_LIMIT

    file_path = DOCS_DIR / f"{safe}.rst.txt"
    if not file_path.is_file():
        return _format_missing_module_error(safe)

    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolError(
            f"Failed to read offline docs for {safe}: {exc}",
            tool_name="lookup_idapython_doc",
        ) from exc

    # Point-lookup mode: filter to ~20 lines around each match of `name`.
    name_clean = name.strip() if name else ""
    if name_clean:
        filtered = _extract_section_around(content, name_clean)
        if filtered is None:
            return (
                f"[Offline IDAPython docs: {safe}; "
                f"no entry matches '{name_clean}']\n\n"
                f"No occurrence of '{name_clean}' found in module {safe}. "
                f"Try lookup_idapython_doc(module='{safe}') without `name` to "
                f"read the full reference, or check the spelling."
            )
        content = filtered
        total_chars = len(content)
        header_label = f"name={name_clean!r}"
    else:
        total_chars = len(content)
        header_label = ""

    if total_chars == 0:
        return f"[Offline IDAPython docs: {safe}; total chars: 0; showing offset 0-0]\n\n(empty response)"
    if offset >= total_chars:
        chunk = ""
    else:
        chunk = content[offset : offset + limit]

    label = f"{safe} ({header_label})" if header_label else safe
    header = (
        f"[Offline IDAPython docs: {label}; total chars: {total_chars:,}; "
        f"showing offset {offset}-{min(offset + limit, total_chars)}]"
    )
    if not chunk:
        return f"{header}\n\n(reached end of content)"
    return f"{header}\n\n{chunk}"


__all__ = [
    "DEFAULT_LIMIT",
    "DOCS_DIR",
    "MAX_LIMIT",
    "lookup_idapython_doc",
]
