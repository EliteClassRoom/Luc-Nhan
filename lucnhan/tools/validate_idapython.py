"""Static validator for IDAPython scripts.

Catches known-hallucinated APIs BEFORE execution so the agent can self-correct
without burning an IDA round-trip. Hybrid enforcement:

* ``BLOCKED_CALLS`` — function definitely does not exist or module was removed.
  Validation result reports these as ``block`` severity. The caller is expected
  to refuse execution.
* ``WARNED_CALLS`` — function still works but is legacy (e.g. ``idc.*`` family).
  Reports as ``warn`` severity. The caller may proceed but should surface the
  warning to the user/agent.

Why this exists
---------------
LLMs occasionally invent "convenience" APIs that look plausible but do not
exist in any version of IDA Python (e.g. ``idaapi.get_operands()``,
``ida_struct.add_struc()`` — module removed in IDA 9.x). Without validation
the error surfaces as ``AttributeError`` at execution time and the agent has
to retry. This module moves the check to script-write time.

Keep this in sync with the DO NOT USE table in
``lucnhan/skills/builtins/ida-scripting/SKILL.md``. When you add an entry
to one, add it to the other.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# Known-bad tables
# ---------------------------------------------------------------------------

# Function calls that do NOT exist in any IDA Python version. Mirrors the
# "Hallucinated APIs — DO NOT USE" table in the ida-scripting skill.
# Value is the suggested fix (one short sentence).
BLOCKED_CALLS: dict[str, str] = {
    # Convenience helpers that AI invents from `idc.GetOperand*` patterns.
    "idaapi.get_operands": "Use insn = ida_ua.insn_t(); ida_ua.decode_insn(insn, ea); insn.ops[i]",
    "idaapi.get_instruction_operands": "Use insn = ida_ua.insn_t(); ida_ua.decode_insn(insn, ea); insn.ops[i]",
    "idaapi.get_insn_operands": "Use insn = ida_ua.insn_t(); ida_ua.decode_insn(insn, ea); insn.ops[i]",
    "idautils.GetOperands": "Use insn = ida_ua.insn_t(); ida_ua.decode_insn(insn, ea); insn.ops[i]",
    "idaapi.op_for_each": "Use 'for op in insn.ops:' after decoding the instruction",
    # Modules removed in IDA 9.x — see AGENTS.md §"IDA 9.x API changes".
    "ida_struct.add_struc": "Removed in IDA 9.x. Use ida_typeinf.tinfo_t().create_udt()",
    "ida_struct.del_struc": "Removed in IDA 9.x. Use ida_typeinf.remove_named_type() or equivalent",
    "ida_struct.get_struc": "Removed in IDA 9.x. Use ida_typeinf.get_named_type()",
    "ida_enum.add_enum": "Removed in IDA 9.x. Use ida_typeinf.tinfo_t() with BTF_ENUM",
    "ida_enum.get_enum": "Removed in IDA 9.x. Use ida_typeinf.get_named_type() then iterate",
    "idc.AddStruc": "Removed in IDA 9.x. Use ida_typeinf.tinfo_t().create_udt()",
    "idc.AddEnum": "Removed in IDA 9.x. Use ida_typeinf.tinfo_t() with BTF_ENUM",
    # Convenience helpers that look like Ghidra/other RE tools.
    "idaapi.get_function_at": "Use ida_funcs.get_func(ea) — returns func_t or None",
    "idaapi.get_function_name": "Use ida_funcs.get_func_name(ea) or ida_name.get_name(ea)",
}

# Modules removed in IDA 9.x. ``import ida_struct`` or
# ``from ida_struct import add_struc`` will fail at import time.
BLOCKED_MODULES: frozenset[str] = frozenset(
    {
        "ida_struct",
        "ida_enum",
    }
)

# Function calls that still work but are discouraged. Mirrors the
# "Legacy/discouraged" subsection of the skill's DO NOT USE table.
WARNED_CALLS: dict[str, str] = {
    "idc.GetOperandValue": "Legacy. Prefer insn.ops[n].value after ida_ua.decode_insn()",
    "idc.GetOpnd": "Legacy. Prefer ida_lines.generate_disasm_line(ea, 0)",
    "idc.GetOperandType": "Legacy. Prefer insn.ops[n].type after ida_ua.decode_insn()",
    "idc.NextHead": "Legacy. Prefer idautils.Heads(start, end) generator",
    "idc.ScreenEA": "Legacy. Prefer ida_kernwin.get_screen_ea()",
}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

Severity = Literal["block", "warn"]


@dataclass(frozen=True)
class ValidationIssue:
    """A single hallucination or legacy-API finding."""

    severity: Severity
    line: int  # 1-based
    column: int  # 0-based
    call: str  # fully-qualified name like "idaapi.get_operands"
    message: str  # human-readable explanation
    fix: str  # suggested replacement

    def format(self) -> str:
        location = f"L{self.line}:{self.column}"
        icon = "BLOCK" if self.severity == "block" else "WARN "
        return f"[{icon}] {location} {self.call}\n        {self.message}\n        Fix: {self.fix}"


@dataclass(frozen=True)
class ValidationResult:
    """Aggregate result of validating a script."""

    issues: tuple[ValidationIssue, ...]
    syntax_error: str | None = None  # populated when ast.parse fails

    @property
    def is_blocked(self) -> bool:
        """True iff any issue has ``severity == 'block'``."""
        return any(issue.severity == "block" for issue in self.issues)

    @property
    def blocked_issues(self) -> tuple[ValidationIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "block")

    @property
    def warnings(self) -> tuple[ValidationIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "warn")

    def format_for_agent(self) -> str:
        """Multi-line, copy-pasteable into an error message for the LLM."""
        if self.syntax_error:
            return f"[SYNTAX ERROR] {self.syntax_error}"
        if not self.issues:
            return ""
        return "\n".join(issue.format() for issue in self.issues)


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _resolve_call_name(node: ast.Call) -> str | None:
    """Return ``"module.sub.func"`` for an ``ast.Call`` node if statically resolvable.

    Walks the attribute chain (e.g. ``idaapi.foo.bar``) and returns the dotted
    name. Returns ``None`` if the call target cannot be statically resolved
    (subscript expressions, lambdas, etc.).
    """
    func = node.func
    parts: list[str] = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
        return ".".join(reversed(parts))
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_idapython(source: str) -> ValidationResult:
    """AST-scan ``source`` for known-hallucinated IDs Python APIs.

    Args:
        source: Python source code to validate (the body that would be
            passed to ``execute_python``).

    Returns:
        ``ValidationResult`` with zero or more issues. ``result.is_blocked``
        is True iff at least one ``block``-severity issue was found.
        A ``SyntaxError`` is captured in ``result.syntax_error`` and does not
        count as a hallucination — caller should let syntax errors propagate
        naturally.

    Pure function: does not import IDA, does not touch globals. Safe to call
    from any code path (test, hook, agent loop, etc.).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return ValidationResult(issues=(), syntax_error=str(exc))

    issues: list[ValidationIssue] = []

    for node in ast.walk(tree):
        # 1. Function calls: idaapi.foo(...)
        if isinstance(node, ast.Call):
            name = _resolve_call_name(node)
            if name is None:
                continue
            line, col = node.lineno, node.col_offset
            if name in BLOCKED_CALLS:
                issues.append(
                    ValidationIssue(
                        severity="block",
                        line=line,
                        column=col,
                        call=name,
                        message="Function does not exist in IDA Python (AI hallucinated).",
                        fix=BLOCKED_CALLS[name],
                    )
                )
            elif name in WARNED_CALLS:
                issues.append(
                    ValidationIssue(
                        severity="warn",
                        line=line,
                        column=col,
                        call=name,
                        message="Legacy API — discouraged in modern IDAPython.",
                        fix=WARNED_CALLS[name],
                    )
                )

        # 2. ``import ida_struct`` — module removed in IDA 9.x.
        elif isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0]
                if top in BLOCKED_MODULES:
                    issues.append(
                        ValidationIssue(
                            severity="block",
                            line=node.lineno,
                            column=node.col_offset,
                            call=f"import {alias.name}",
                            message=f"Module '{top}' was removed in IDA 9.x.",
                            fix="Use 'ida_typeinf' instead. See ida-scripting skill.",
                        )
                    )

        # 3. ``from ida_struct import add_struc``
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".", 1)[0] in BLOCKED_MODULES:
                top = node.module.split(".", 1)[0]
                joined = ", ".join(a.name for a in node.names)
                issues.append(
                    ValidationIssue(
                        severity="block",
                        line=node.lineno,
                        column=node.col_offset,
                        call=f"from {node.module} import {joined}",
                        message=f"Module '{top}' was removed in IDA 9.x.",
                        fix="Use 'ida_typeinf' instead. See ida-scripting skill.",
                    )
                )

    return ValidationResult(issues=tuple(issues))


__all__ = [
    "BLOCKED_CALLS",
    "BLOCKED_MODULES",
    "WARNED_CALLS",
    "Severity",
    "ValidationIssue",
    "ValidationResult",
    "validate_idapython",
]
