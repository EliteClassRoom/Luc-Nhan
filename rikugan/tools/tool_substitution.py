"""Suggest dedicated tools when an ``execute_python`` script is just a wrapper.

When the agent reaches for ``execute_python`` to reimplement
``ida_nalt.get_import_module_qty()`` (which is what ``list_imports``
does), the user pays for an extra approval round-trip and we lose the
benefit of pagination, caching, and the description the dedicated tool
already shows the model. This module catches that pattern and emits a
non-blocking suggestion the LLM can act on.

Design choices
--------------
- **Suggest-only.** We never block execution. The agent may have a
  legitimate reason to script (filtering, batch processing, custom
  formatting); we just make the dedicated-tool option visible.
- **AST-based.** Regex over the raw source would yield too many
  false positives (string literals, comments, docstrings, variable
  names). Walking the AST only matches actual call sites.
- **Module-level mapping table.** Adding new suggestions is a one-line
  change in ``_API_PATTERNS`` — no need to touch the scanner.

The mapping table in this file is the business logic: it encodes
"this IDAPython call sequence is equivalent to that dedicated tool".
It is contribution-driven because it depends on IDAPython muscle
memory that varies by reverse-engineer.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class Suggestion:
    """One suggested replacement for a script pattern.

    Attributes
    ----------
    tool_name
        Name of the dedicated tool the LLM should prefer.
    matched_apis
        Tuple of IDAPython call names that triggered this suggestion
        (e.g. ``("ida_nalt.get_import_module_qty", "ida_nalt.enum_import_names")``).
        Useful for debugging and for the UI hint.
    hint
        Short human-readable reason. Shown to the LLM and to the user
        in the approval card.
    """

    tool_name: str
    matched_apis: tuple[str, ...]
    hint: str


# ---------------------------------------------------------------------------
# Mapping table — contribution-driven
# ---------------------------------------------------------------------------
#
# Each entry is ``(api_name, ToolDefinition-equivalent description)`` where
# ``api_name`` is the fully-qualified last-hop of an IDAPython call
# (e.g. ``ida_nalt.get_import_module_qty``), and the value carries the
# dedicated tool name + a short reason that becomes the LLM hint.
#
# MULTI-CALL tools (e.g. list_imports needs both module_qty + enum_import_names)
# are detected via the ``combo`` callable below — see ``_COMBO_RULES``.
#
# This table is intentionally small at first; expand it as you encounter
# LLM patterns that should reuse existing tools.

_API_PATTERNS: Final[dict[str, tuple[str, str]]] = {
    # Single-call matches: api_name -> (tool_name, hint)
    # Format the hint as a short imperative the LLM can act on.
    "ida_nalt.get_import_module_qty": (
        "list_imports",
        "Enumerating import modules is what list_imports does; prefer that tool to avoid the approval round-trip.",
    ),
    "ida_nalt.enum_import_names": (
        "search_imports",
        "Walking imports one-by-one is what search_imports / list_imports do; prefer those tools.",
    ),
    "idautils.Names": (
        "search_imports",
        "Filtering by segment to collect import names is what search_imports does.",
    ),
    "idautils.Entries": (
        "list_exports",
        "Iterating the export table to list symbols is exactly what list_exports does.",
    ),
    "idautils.Functions": (
        "list_functions",
        "Walking every function is what list_functions (paginated) already does.",
    ),
    "idautils.Strings": (
        "list_strings",
        "Enumerating strings is what list_strings does.",
    ),
    "idautils.XrefsTo": (
        "xrefs_to",
        "Building cross-references to an address is what xrefs_to does directly.",
    ),
    "idautils.XrefsFrom": (
        "xrefs_from",
        "Building cross-references from an address is what xrefs_from does directly.",
    ),
    "ida_segment.getseg": (
        "list_segments",
        "Iterating segments to gather name/range/perm is what list_segments does.",
    ),
    # Annot / Decompiler — contributed mappings
    "ida_name.set_name": (
        "rename_address",
        "Renaming an address or symbol is what rename_address does.",
    ),
    "ida_funcs.get_func_name": (
        "get_function_name",
        "Reading the current function name at an address is what get_function_name does.",
    ),
    "idc.SetComment": (
        "set_comment",
        "Setting a comment at an address is what set_comment does.",
    ),
    "idc.SetFunctionComment": (
        "set_function_comment",
        "Setting a function-level comment is what set_function_comment does.",
    ),
    "idc.SetType": (
        "set_type",
        "Setting a type at an address is what set_type does.",
    ),
    "idc.SetFunctionAttr": (
        "set_function_prototype",
        "Setting a function prototype is what set_function_prototype does.",
    ),
    "ida_hexrays.decompile": (
        "decompile_function",
        "Decompiling a function is what decompile_function does.",
    ),
    # Disassembly / IL
    "idc.GetDisasm": (
        "read_disassembly",
        "Reading raw disassembly at an address is what read_disassembly does.",
    ),
    "ida_ua.decode_insn": (
        "get_instruction_info",
        "Decoding a single instruction is what get_instruction_info does.",
    ),
    "ida_hexrays.gen_microcode": (
        "get_microcode",
        "Generating microcode for a function is what get_microcode does.",
    ),
    # Type / Struct — bare-method-name fallback: LLM often writes
    # ``t = ida_typeinf.tinfo_t(); t.create_udt()`` so the call site
    # we collect is just ``create_udt``.
    "create_udt": (
        "create_struct",
        "Creating a UDT struct is what create_struct does.",
    ),
    "ida_typeinf.iter_struct": (
        "list_structs",
        "Iterating struct types is what list_structs does.",
    ),
    "ida_typeinf.iter_enum": (
        "list_enums",
        "Iterating enum types is what list_enums does.",
    ),
    # Reading struct/UDT layout — bare-method-name fallback: the LLM writes
    # ``tif = ida_typeinf.tinfo_t(); tif.get_named_type(...); tif.get_udt_details(udt)``
    # so the call sites we collect are just ``get_named_type`` / ``get_udt_details``.
    "get_named_type": (
        "get_struct_info",
        "Reading a named struct's type + member layout is what get_struct_info does.",
    ),
    "get_udt_details": (
        "get_struct_info",
        "Dumping UDT member layout (offset/type/name) is what get_struct_info does.",
    ),
    # Reading a raw qword/pointer — bare-method-name: the matcher's last-hop
    # fallback resolves both ida_bytes.get_qword and idc.get_qword to this key.
    "get_qword": (
        "read_global_value",
        "Reading a qword/pointer value is what read_global_value(type_hint='u64'/'ptr') does.",
    ),
}


# ---------------------------------------------------------------------------
# Multi-call patterns (combo rules)
# ---------------------------------------------------------------------------
#
# Some dedicated tools require *several* IDAPython calls to reimplement
# (e.g. list_imports needs both ``get_import_module_qty`` and
# ``enum_import_names``). A combo rule fires when the script mentions
# all the listed APIs *and* no single-API match already won.
#
# Each combo is a tuple ``(frozenset_of_api_names, tool_name, hint)``.

_COMBO_RULES: Final[tuple[tuple[frozenset[str], str, str], ...]] = (
    (
        frozenset({"ida_nalt.get_import_module_qty", "ida_nalt.enum_import_names"}),
        "list_imports",
        "Reimplementing list_imports by hand. Prefer the dedicated tool.",
    ),
    (
        frozenset({"ida_nalt.get_import_module_qty", "ida_nalt.get_import_module_name", "ida_nalt.enum_import_names"}),
        "list_imports",
        "Reimplementing list_imports with module + entry enumeration. Prefer the dedicated tool.",
    ),
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _collect_call_names(tree: ast.AST) -> list[str]:
    """Walk *tree* and return every fully-qualified call target we can statically resolve.

    Static resolution means: only ``name.attr.attr...`` calls.
    Subscript expressions, lambdas, and indirect calls (``getattr``)
    are intentionally skipped — those would produce too many false
    positives and the agent already gets blocked from ``getattr`` by
    ``script_guard`` for unrelated security reasons.
    """
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        parts: list[str] = []
        func = node.func
        while isinstance(func, ast.Attribute):
            parts.append(func.attr)
            func = func.value
        if isinstance(func, ast.Name):
            parts.append(func.id)
            names.append(".".join(reversed(parts)))
    return names


def suggest_substitutions(
    code: str,
    *,
    api_patterns: dict[str, tuple[str, str]] | None = None,
    combo_rules: tuple[tuple[frozenset[str], str, str], ...] | None = None,
) -> list[Suggestion]:
    """Return dedicated-tool suggestions if *code* looks like a wrapper.

    Parameters
    ----------
    code
        The Python source the agent wants to send to ``execute_python``.
    api_patterns
        Override the module-level mapping table (useful for testing).
    combo_rules
        Override the module-level combo rules.
    """
    patterns = api_patterns if api_patterns is not None else _API_PATTERNS
    rules = combo_rules if combo_rules is not None else _COMBO_RULES

    try:
        tree = ast.parse(code)
    except SyntaxError:
        # Validation in script_guard runs separately; suggestion layer
        # must not crash on malformed input.
        return []

    calls = _collect_call_names(tree)
    if not calls:
        return []

    seen_tools: set[str] = set()
    suggestions: list[Suggestion] = []

    # 1. Single-call matches — fire for every resolved API we know about.
    matched: list[str] = []
    for api in calls:
        # Last hop only — ``mod.func`` regardless of how the agent got there.
        last_hop = api.rsplit(".", 1)[-1] if "." in api else api
        # But try the full chain first (``ida_nalt.get_import_module_qty``),
        # then fall back to the bare name (``get_import_module_qty``).
        keys = [api]
        if "." in api:
            keys.append(last_hop)
        for key in keys:
            entry = patterns.get(key)
            if entry is None:
                continue
            tool_name, hint = entry
            if tool_name in seen_tools:
                matched.append(api)
                break
            seen_tools.add(tool_name)
            matched.append(api)
            suggestions.append(
                Suggestion(
                    tool_name=tool_name,
                    matched_apis=(api,),
                    hint=hint,
                )
            )
            break

    # 2. Combo rules — fire only if no single-API match already covered the tool.
    called_set = frozenset(calls)
    for trigger_set, tool_name, hint in rules:
        if tool_name in seen_tools:
            continue
        if trigger_set.issubset(called_set):
            suggestions.append(
                Suggestion(
                    tool_name=tool_name,
                    matched_apis=tuple(sorted(trigger_set)),
                    hint=hint,
                )
            )
            seen_tools.add(tool_name)

    return suggestions


def format_suggestions_for_agent(suggestions: list[Suggestion]) -> str:
    """Render suggestions as a short preamble the LLM reads before the script output.

    Empty input → empty string (no wasted tokens). Designed to be
    prepended to the script's stdout/stderr result so the LLM sees the
    hint *and* the original script output, then can choose to retry
    with the dedicated tool on a future turn.

    The preamble explicitly identifies itself as Rikugan's tool
    substitution guard so the LLM can tell the block apart from
    malformed script output, stale context, or user messages.
    """
    if not suggestions:
        return ""
    lines = [
        "[rikugan] Tool substitution guard — prefer these dedicated tools over this script:",
        "",
    ]
    for s in suggestions:
        lines.append(f"- `{s.tool_name}` — {s.hint}")
    lines.append("")
    lines.append(
        "Call the dedicated tool on your next turn instead of repeating "
        "this script. The guard is suggest-only — this script ran to "
        "completion, but the dedicated tool would skip the user-approval "
        "round-trip and use the cached/paginated result."
    )
    return "\n".join(lines)


# Convenience predicate for callers (e.g. UI) that just want a yes/no.
def has_suggestions(code: str) -> bool:
    """Return True if *code* matches at least one suggestion."""
    return bool(suggest_substitutions(code))


# Resolve at import time so the table can be replaced wholesale in tests
# by reassigning the module attribute, but so callers get a frozen view.
def _resolve_api_patterns() -> dict[str, tuple[str, str]]:
    return dict(_API_PATTERNS)


__all__ = [
    "Suggestion",
    "format_suggestions_for_agent",
    "has_suggestions",
    "suggest_substitutions",
]
