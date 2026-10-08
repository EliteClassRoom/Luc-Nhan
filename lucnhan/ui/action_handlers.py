"""Shared prompt-generating action handlers for IDA Pro.

Each handler takes a context dict with keys: ea, func_ea, func_name, selected_text.
Returns the prompt text to place in the input area.

Host-specific actions (IDA's microcode optimizer, smart-patch) live in
their respective ``<host>/ui/actions.py`` modules.
"""

from __future__ import annotations

from typing import Any


def _func_label(ctx: dict[str, Any]) -> tuple[str, int]:
    """Return (display_name, effective_address) from context."""
    name = ctx["func_name"] or f"sub_{ctx['ea']:x}"
    ea = ctx["func_ea"] or ctx["ea"]
    return name, ea


def handle_send_to(ctx: dict[str, Any]) -> str:
    sel = ctx["selected_text"]
    if sel:
        return sel
    name = ctx["func_name"]
    ea = ctx["ea"]
    if name:
        return f"Analyze the function {name} at 0x{ea:x}"
    return f"Analyze the code at 0x{ea:x}"


def handle_explain(ctx: dict[str, Any]) -> str:
    name, ea = _func_label(ctx)
    return f"Explain the function {name} at 0x{ea:x}. Decompile it and provide a detailed analysis."


def handle_rename(ctx: dict[str, Any]) -> str:
    name, ea = _func_label(ctx)
    return (
        f"Analyze the function {name} at 0x{ea:x}. "
        "Based on its behavior, suggest better names for the function "
        "and its local variables. Apply the renames."
    )


def handle_deobfuscate(ctx: dict[str, Any], *, optimizer_term: str = "IL") -> str:
    name, ea = _func_label(ctx)
    return (
        f"Deobfuscate the function {name} at 0x{ea:x}. "
        "Identify obfuscation patterns (opaque predicates, junk code, "
        "control-flow flattening, encrypted strings) and explain them. "
        f"If possible, apply {optimizer_term} optimizations to clean the output."
    )


def handle_vuln_audit(ctx: dict[str, Any]) -> str:
    name, ea = _func_label(ctx)
    return (
        f"Audit the function {name} at 0x{ea:x} for security vulnerabilities. "
        "Check for buffer overflows, format strings, integer overflows, "
        "use-after-free, command injection, and other memory-safety issues. "
        "List each finding with severity and evidence."
    )


def handle_suggest_types(ctx: dict[str, Any]) -> str:
    name, ea = _func_label(ctx)
    return (
        f"Analyze the function {name} at 0x{ea:x} and infer types. "
        "Examine pointer dereference patterns to suggest structs, "
        "identify enum-like constants, and propose proper parameter types. "
        "Apply the type changes."
    )


def handle_annotate(ctx: dict[str, Any]) -> str:
    name, ea = _func_label(ctx)
    return (
        f"Annotate the function {name} at 0x{ea:x} with comments. "
        "Add a function-level comment summarizing its purpose, and "
        "add inline comments to key basic blocks explaining the logic."
    )


def handle_clean(ctx: dict[str, Any], *, ir_term: str = "IL") -> str:
    name, ea = _func_label(ctx)
    return (
        f"Clean the {ir_term} for {name} at 0x{ea:x}. "
        f"Read the {ir_term}, identify junk or obfuscated instructions, "
        "NOP or patch them if needed, then redecompile to verify."
    )


def handle_xref_analysis(ctx: dict[str, Any]) -> str:
    name, ea = _func_label(ctx)
    return (
        f"Perform a deep cross-reference analysis on {name} at 0x{ea:x}. "
        "Trace all callers and callees, identify data references, "
        "and map out the call graph around this function."
    )
