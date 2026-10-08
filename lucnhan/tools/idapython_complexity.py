"""IDAPython script complexity classifier for the docs-review gate.

Determines whether an ``execute_python`` script is "complex" enough to
warrant running an IDA docs review subagent before user approval.

The classifier is intentionally **pure**: it has no IDA dependencies,
no LLM calls, no globals.  It operates on the Python source string and
an optional ``ValidationResult`` from ``validate_idapython``.

Heuristics (any one makes the script complex)
-------------------------------------------
* Script body (non-blank, non-comment) exceeds ``COMPLEX_LINE_THRESHOLD``
  lines (default 8).
* References two or more IDA Python modules (``ida_*``, ``idautils``,
  ``idc``, ``idaapi``, ``ida_hexrays``, ``ida_typeinf``, ``ida_frame``,
  ``ida_domain``, ``ida_kernwin``, ``ida_ua``).
* Calls known mutating API prefixes (``patch_*``, ``set_*``, ``add_*``,
  ``del_*``, ``create_*``, ``apply_*``, ``rename``, ``make_*``, ``plan_*``,
  ``auto_mark_range``).
* Calls mutating-looking methods on mutating modules
  (``ida_bytes``, ``ida_name``, ``ida_typeinf``, ``ida_funcs``,
  ``ida_xref``, ``ida_segment``).
* Defines classes or functions, or uses visitor-style
  ``ctree_visitor_t`` / ``microcode_filter_t`` subclasses.
* Iterates over database generators
  (``idautils.Functions``, ``FuncItems``, ``Heads``, ``Segments``,
  ``Strings``, ``XrefsTo``, ``XrefsFrom``, ``CodeRefsTo``,
  ``DataRefsTo``, ``Entries``).
* Uses heavy IDA subsystems: ``ida_hexrays``, ``ida_typeinf``,
  ``ida_frame``, ``ida_domain``, ``ida_kernwin``, ``ida_ua``.
* ``validate_idapython()`` reports any warnings or blocked issues
  (when provided).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from .validate_idapython import ValidationResult

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

#: Non-blank / non-comment lines above this triggers the gate.
COMPLEX_LINE_THRESHOLD: int = 8

#: Number of distinct IDA modules used to flag as complex.
COMPLEX_MODULE_COUNT: int = 2

#: Known mutating API prefixes. Any call starting with one of these
#: (followed by an alphanumeric character) counts as a mutation.
_MUTATING_PREFIXES: tuple[str, ...] = (
    "patch_",
    "set_",
    "add_",
    "del_",
    "create_",
    "apply_",
    "make_",
    "plan_",
    "auto_mark_range",
    "rename",
    "save_",
    "remove_",
    "delete_",
    "update_",
    "write_",
    "commit_",
)

#: Modules whose *method calls* (anything containing a dot with these
#: as the root) are considered mutating.  Read-only modules like
#: ``idautils`` are excluded — those are iterated via top-level calls.
_MUTATING_MODULES: frozenset[str] = frozenset(
    {
        "ida_bytes",
        "ida_name",
        "ida_typeinf",
        "ida_funcs",
        "ida_xref",
        "ida_segment",
        "ida_nalt",
        "ida_entry",
        "ida_hexrays",
        "ida_frame",
    }
)

#: Any single use of one of these heavy modules flags the script complex.
_HEAVY_MODULES: frozenset[str] = frozenset(
    {
        "ida_hexrays",
        "ida_typeinf",
        "ida_frame",
        "ida_domain",
        "ida_kernwin",
        "ida_ua",
    }
)

#: Iteration helpers whose use implies database-wide traversal.
_ITERATION_HELPERS: frozenset[str] = frozenset(
    {
        "idautils.Functions",
        "idautils.FuncItems",
        "idautils.Heads",
        "idautils.Segments",
        "idautils.Strings",
        "idautils.XrefsTo",
        "idautils.XrefsFrom",
        "idautils.CodeRefsTo",
        "idautils.DataRefsTo",
        "idautils.Entries",
        "idautils.Names",
        "idautils.Modules",
    }
)

#: IDA modules used to count "module variety" heuristic.  Read-only
#: helpers that do not require deep docs are excluded.
_IDA_MODULES: frozenset[str] = frozenset(
    {
        "idaapi",
        "idautils",
        "idc",
        "ida_funcs",
        "ida_name",
        "ida_bytes",
        "ida_segment",
        "ida_typeinf",
        "ida_nalt",
        "ida_xref",
        "ida_kernwin",
        "ida_hexrays",
        "ida_frame",
        "ida_domain",
        "ida_ua",
        "ida_gdl",
        "ida_idp",
        "ida_netnode",
        "ida_lines",
        "ida_ida",
        "ida_pro",
        "ida_search",
        "ida_loader",
        "ida_dbg",
        "ida_moves",
    }
)

#: Visitor / ctree base classes whose subclassing flags the script.
_VISITOR_BASES: frozenset[str] = frozenset(
    {
        "ctree_visitor_t",
        "microcode_filter_t",
        "minsn_t",
        "optinsn_t",
        "user_visitor_t",
        "code_visitor_t",
    }
)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScriptComplexity:
    """Classification result for an IDAPython script.

    *is_complex* is True if any rule fired.  *reasons* is a tuple of
    short human-readable strings ("uses 3 IDA modules", "calls
    ``ida_bytes.patch_*``", etc.) suitable for showing the agent.
    """

    is_complex: bool
    reasons: tuple[str, ...] = ()

    def format(self) -> str:
        if not self.reasons:
            return "simple"
        bullets = "\n".join(f"- {r}" for r in self.reasons)
        return f"complex ({len(self.reasons)} reason(s)):\n{bullets}"


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _imported_modules(tree: ast.AST) -> set[str]:
    """Return the set of top-level module names imported anywhere in *tree*."""
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".", 1)[0])
    return modules


def _imported_names(tree: ast.AST) -> dict[str, str]:
    """Map ``local_name -> top_module`` for ``from X import Y`` style imports."""
    mapping: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            top = node.module.split(".", 1)[0]
            for alias in node.names:
                mapping[alias.asname or alias.name] = top
        elif isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0]
                local = alias.asname or top
                mapping[local] = top
    return mapping


def _resolve_call_chain(node: ast.Call) -> list[str] | None:
    """Resolve a call target to its attribute chain.

    Returns the list of names from outermost to innermost (e.g. for
    ``idaapi.foo.bar()`` returns ``["idaapi", "foo", "bar"]``) or
    ``None`` if the target is not statically resolvable.
    """
    func = node.func
    parts: list[str] = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
        return list(reversed(parts))
    return None


def _call_is_attribute_of(chain: list[str], imports: dict[str, str], modules: set[str]) -> tuple[str, str] | None:
    """Return ``(module, attr)`` if *chain* ends with a module attribute.

    e.g. ``ida_bytes.patch_byte(ea, 0)`` -> ``("ida_bytes", "patch_byte")``.
    """
    if len(chain) < 2:
        return None
    root = chain[0]
    # Direct module reference: ``ida_bytes.patch_byte``
    if root in modules:
        return (root, chain[-1])
    # Aliased: ``ib.patch_byte`` where ``ib`` is ``import ida_bytes as ib``
    if root in imports:
        return (imports[root], chain[-1])
    return None


def _is_mutating_call(call_name: str | None) -> bool:
    """True if *call_name* looks like a mutating API.

    Accepts both bare names (``set_cmt``) and dotted chains
    (``ida_bytes.set_cmt`` / ``set_cmt``).  For dotted chains only the
    final attribute is examined, so ``idc.set_cmt`` matches ``set_*``.
    """
    if not call_name:
        return False
    target = call_name.rsplit(".", 1)[-1].lower()
    for prefix in _MUTATING_PREFIXES:
        if target == prefix or target.startswith(prefix):
            return True
    return False


def _count_code_lines(source: str) -> int:
    """Count non-blank, non-comment-only lines in *source*."""
    count = 0
    for raw in source.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        count += 1
    return count


def _has_user_definitions(tree: ast.AST) -> bool:
    """True if the script defines a class or function at module level."""
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return True
    return False


def _uses_iteration_helpers(tree: ast.AST) -> bool:
    """True if any call chain matches a known DB iteration helper."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            chain = _resolve_call_chain(node)
            if not chain:
                continue
            dotted = ".".join(chain)
            if dotted in _ITERATION_HELPERS:
                return True
    return False


def _subclasses_visitor(tree: ast.AST) -> bool:
    """True if any class definition extends a known visitor base."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                base_name: str | None = None
                if isinstance(base, ast.Name):
                    base_name = base.id
                elif isinstance(base, ast.Attribute):
                    base_name = base.attr
                if base_name and base_name in _VISITOR_BASES:
                    return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_idapython_script(
    source: str,
    validation: ValidationResult | None = None,
) -> ScriptComplexity:
    """Classify *source* as simple or complex for the docs-review gate.

    Args:
        source: Python source code (the body that would be passed to
            ``execute_python``).
        validation: Optional ``ValidationResult`` from
            ``validate_idapython``.  If provided and contains warnings
            or blocked issues, the script is treated as complex.

    Returns:
        :class:`ScriptComplexity` with ``is_complex`` and ``reasons``.
    """
    reasons: list[str] = []

    code_lines = _count_code_lines(source)
    if code_lines > COMPLEX_LINE_THRESHOLD:
        reasons.append(f"script has {code_lines} non-comment lines (threshold {COMPLEX_LINE_THRESHOLD})")

    # AST-based checks.  We fall back to line-count-only on syntax error.
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Invalid syntax is already a hard fail inside execute_python;
        # still gate on length so the reviewer can give the agent a
        # better error.
        return ScriptComplexity(is_complex=bool(reasons), reasons=tuple(reasons))

    imports_map = _imported_names(tree)
    imported_modules = _imported_modules(tree)
    ida_modules_used = {m for m in imported_modules if m in _IDA_MODULES}

    # Heavy module detection at import time.  ``import ida_hexrays``
    # alone is enough to flag the script complex — the reviewer can
    # verify the specific ctree APIs the script uses.
    heavy_imports = sorted(m for m in imported_modules if m in _HEAVY_MODULES)
    if heavy_imports:
        reasons.append("imports heavy IDA module(s): " + ", ".join(heavy_imports))

    # Expand to include attribute roots that reference IDA modules
    # without an explicit import (e.g. the execute_python namespace
    # pre-imports ``idaapi`` etc. — see ida/tools/scripting.py).
    _PRELOADED_IDA_MODULES = {
        "idaapi",
        "idautils",
        "idc",
        "ida_funcs",
        "ida_name",
        "ida_bytes",
        "ida_segment",
        "ida_typeinf",
        "ida_nalt",
        "ida_xref",
        "ida_kernwin",
        "ida_domain",
    }

    mutating_hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            chain = _resolve_call_chain(node)
            if not chain:
                continue

            dotted = ".".join(chain)

            # Check iteration helpers
            if dotted in _ITERATION_HELPERS:
                reasons.append(f"iterates database with {dotted}()")
                continue

            # Resolve module attribute calls
            attr_info = _call_is_attribute_of(chain, imports_map, _PRELOADED_IDA_MODULES)
            if attr_info is None:
                # Fallback: dotted name like ``ida_bytes.foo``
                if len(chain) >= 2 and chain[0] in _PRELOADED_IDA_MODULES:
                    attr_info = (chain[0], chain[-1])
            if attr_info is not None:
                module, attr = attr_info
                if module in _HEAVY_MODULES:
                    reasons.append(f"uses heavy module {module}.{attr}()")
                # Mutating calls: either on a known mutating module, or
                # any ``set_*`` / ``patch_*`` / etc. on any IDA module.
                if module in _MUTATING_MODULES and _is_mutating_call(attr):
                    mutating_hits.append(f"{module}.{attr}")
                elif module in _PRELOADED_IDA_MODULES and _is_mutating_call(attr):
                    mutating_hits.append(f"{module}.{attr}")
                # Standalone mutating prefix on a method (rare but possible)
                if _is_mutating_call(dotted):
                    mutating_hits.append(f"{dotted}")

            # Bare call whose name looks mutating
            elif len(chain) == 1 and _is_mutating_call(chain[0]):
                mutating_hits.append(chain[0])

    if mutating_hits:
        # Dedupe while preserving order
        seen: set[str] = set()
        unique = []
        for h in mutating_hits:
            if h not in seen:
                seen.add(h)
                unique.append(h)
        reasons.append("calls mutating APIs: " + ", ".join(unique[:5]) + (" ..." if len(unique) > 5 else ""))

    # Module variety heuristic (count heavy + mutating modules used)
    referenced_ida_modules: set[str] = set(ida_modules_used)
    for chain in (_resolve_call_chain(n) for n in ast.walk(tree) if isinstance(n, ast.Call)):
        if not chain:
            continue
        root = chain[0]
        if root in _PRELOADED_IDA_MODULES:
            referenced_ida_modules.add(root)
    # De-duplicate attribute-only references via the full chain root
    # (already handled above for the heavy/mutating checks).
    if len(referenced_ida_modules) >= COMPLEX_MODULE_COUNT:
        reasons.append(f"uses {len(referenced_ida_modules)} IDA modules: " + ", ".join(sorted(referenced_ida_modules)))

    if _has_user_definitions(tree):
        reasons.append("defines classes or functions (likely helper logic)")

    if _subclasses_visitor(tree):
        reasons.append("subclasses a visitor / ctree / microcode base")

    if validation is not None:
        if validation.syntax_error:
            reasons.append(f"script has a syntax error: {validation.syntax_error}")
        if validation.is_blocked:
            reasons.append(f"validator blocked {len(validation.blocked_issues)} hallucinated API(s)")
        elif validation.warnings:
            reasons.append(f"validator flagged {len(validation.warnings)} legacy/warn API(s)")

    return ScriptComplexity(is_complex=bool(reasons), reasons=tuple(reasons))


__all__ = [
    "COMPLEX_LINE_THRESHOLD",
    "COMPLEX_MODULE_COUNT",
    "ScriptComplexity",
    "classify_idapython_script",
]
