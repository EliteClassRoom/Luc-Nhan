"""Python scripting execution tool."""

from __future__ import annotations

import importlib
from typing import Annotated

from ... import constants
from ...core.errors import ToolError
from ...core.logging import log_debug
from ...tools.base import tool
from ...tools.script_guard import run_guarded_script, safe_builtins
from ...tools.tool_substitution import format_suggestions_for_agent, suggest_substitutions
from ...tools.validate_idapython import validate_idapython

# Cached namespace of common IDA modules — populated once, reused across calls.
_IDA_MODULE_NAMES = (
    "idaapi",
    "idautils",
    "idc",
    "ida_funcs",
    "ida_name",
    "ida_bytes",
    "ida_segment",
    # ida_struct/ida_enum were removed in IDA 9.x — use ida_typeinf instead.
    "ida_typeinf",
    "ida_nalt",
    "ida_xref",
    "ida_kernwin",
    # Domain API (IDA 9.1+) — optional; absent on older IDA, see _get_base_namespace
    "ida_domain",
)
_cached_namespace: dict | None = None


def _get_base_namespace() -> dict:
    """Return a cached namespace with common IDA modules pre-imported."""
    global _cached_namespace
    if _cached_namespace is None:
        ns: dict = {}
        for mod_name in _IDA_MODULE_NAMES:
            try:
                ns[mod_name] = importlib.import_module(mod_name)
            except ImportError as e:
                log_debug(f"Optional IDA module {mod_name!r} not available: {e}")
        _cached_namespace = ns
    # Return a copy so user code can't pollute the cache
    result: dict = {"__builtins__": safe_builtins()}
    result.update(_cached_namespace)
    return result


@tool(category="scripting", mutating=True)
def execute_python(
    code: Annotated[str, "Python code to execute in IDA's scripting environment"],
) -> str:
    """Execute arbitrary Python code in IDA's context and return stdout/stderr.

    The code runs with full access to IDA's Python API (idaapi, idautils, idc, etc.).
    Use print() to produce output that will be returned.

    A static validator (``lucnhan.tools.validate_idapython``) runs before exec:
    * Calls to known-hallucinated APIs (e.g. ``idaapi.get_operands()``) cause
      execution to be REFUSED — the agent sees a ``ToolError`` with a fix
      suggestion and must rewrite the script.
    * Calls to legacy ``idc.*`` helpers produce a warning prefix in the output
      but do not block execution.

    See ``lucnhan/skills/builtins/ida-scripting/SKILL.md`` for the full
    anti-hallucination ruleset and how to keep them in sync.
    """
    # 1. Static hallucination check — block BEFORE execution if any BLOCK call.
    validation = validate_idapython(code)
    if validation.is_blocked:
        report = validation.format_for_agent()
        raise ToolError(
            "Script blocked by IDAPython hallucination guard. "
            "Rewrite using the suggested fix; see the ida-scripting skill "
            "for the full DO NOT USE table.\n\n" + report,
            tool_name=constants.EXECUTE_PYTHON_TOOL_NAME,
        )

    # 2. Tool-substitution hint — runs AFTER the hallucination check so we
    #    do not spend tokens suggesting a tool for a script that should be
    #    rewritten anyway. Suggest-only: we never block, the agent may
    #    have a legitimate reason to script (filtering, batch ops, etc.).
    suggestions = suggest_substitutions(code)
    preamble = format_suggestions_for_agent(suggestions)

    # 3. Run guarded execution.
    output = run_guarded_script(code, _get_base_namespace)

    # 4. Compose the visible output: warnings + substitution hints come
    #    first so the LLM sees them before its own script's stdout.
    parts: list[str] = []
    if preamble:
        parts.append(preamble)
    if validation.warnings:
        parts.append(
            "[validate_idapython] Legacy/discouraged APIs detected — "
            "prefer modern ida_* equivalents:\n" + validation.format_for_agent() + "\n--- script output follows ---"
        )
    parts.append(output)
    return "\n\n".join(parts)
