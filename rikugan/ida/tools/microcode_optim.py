"""Microcode optimizer classes and lifecycle management.

Provides NOP, dynamic instruction, and dynamic block optimizers that
integrate with IDA's Hex-Rays optimization pipeline.  The global
``installed_optimizers`` dict keeps references alive so IDA doesn't GC them.
"""

from __future__ import annotations

import importlib
import textwrap
from collections.abc import Callable
from typing import Any

from ...core.host import HAS_HEXRAYS as _HAS_HEXRAYS
from ...core.logging import log_debug
from ...tools.script_guard import check_ast, safe_builtins

ida_hexrays = None
try:
    ida_hexrays = importlib.import_module("ida_hexrays")
except ImportError as e:
    log_debug(f"IDA modules not available: {e}")

# ---------------------------------------------------------------------------
# Global optimizer registry — keeps references alive so IDA doesn't GC them
# ---------------------------------------------------------------------------
installed_optimizers: dict[str, object] = {}


# ---------------------------------------------------------------------------
# Optimizer classes
# ---------------------------------------------------------------------------


class NopOptimizer(ida_hexrays.optinsn_t if _HAS_HEXRAYS else object):
    """Instruction optimizer that NOPs instructions at specified addresses."""

    def __init__(self, name: str, target_eas: set, func_ea: int):
        if _HAS_HEXRAYS:
            super().__init__()
        self.name = name
        self.target_eas = target_eas
        self.func_ea = func_ea
        self.applied_count = 0

    def func(self, blk, ins, optflags):
        if ins.ea in self.target_eas:
            ins.opcode = ida_hexrays.m_nop
            self.applied_count += 1
            return 1
        return 0


class DynamicInsnOptimizer(ida_hexrays.optinsn_t if _HAS_HEXRAYS else object):
    """Instruction optimizer wrapping user-provided Python code."""

    def __init__(self, name: str, description: str, optimize_fn):
        if _HAS_HEXRAYS:
            super().__init__()
        self.name = name
        self.description = description
        self._optimize = optimize_fn

    def func(self, blk, ins, optflags):
        try:
            result = self._optimize(blk, ins)
            return int(result) if result else 0
        except Exception as e:
            log_debug(f"DynamicInsnOptimizer '{self.name}' raised: {e}")
            return 0


class DynamicBlockOptimizer(ida_hexrays.optblock_t if _HAS_HEXRAYS else object):
    """Block optimizer wrapping user-provided Python code."""

    def __init__(self, name: str, description: str, optimize_fn):
        if _HAS_HEXRAYS:
            super().__init__()
        self.name = name
        self.description = description
        self._optimize = optimize_fn

    def func(self, blk):
        try:
            result = self._optimize(blk)
            return int(result) if result else 0
        except Exception as e:
            log_debug(f"DynamicBlockOptimizer '{self.name}' raised: {e}")
            return 0


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------


def remove_optimizer(name: str) -> None:
    """Remove an optimizer from the registry and uninstall it."""
    opt = installed_optimizers.pop(name, None)
    if opt is not None:
        try:
            opt.remove()
        except (AttributeError, RuntimeError) as e:
            log_debug(f"remove_optimizer '{name}': {e}")  # may already be removed


def build_optimizer_namespace() -> dict[str, Any]:
    """Build the execution namespace for user-provided optimizer code.

    Includes ida_hexrays and all its opcode/operand/maturity constants.
    """
    namespace = {"__builtins__": safe_builtins()}
    namespace["ida_hexrays"] = ida_hexrays
    for attr in dir(ida_hexrays):
        if attr.startswith("m_") or attr.startswith("mop_") or attr.startswith("MMAT_") or attr.startswith("BLT_"):
            namespace[attr] = getattr(ida_hexrays, attr)
    return namespace


def compile_optimizer(name: str, python_code: str) -> Callable[..., Any]:
    """Compile user-provided optimizer code and extract the optimize function.

    Returns the callable, or raises with an error message string.

    Security note: the code first passes through the shared AST blocklist
    (:func:`rikugan.tools.script_guard.check_ast` — blocked modules, calls,
    attrs, dunders), then the ``exec`` compiles it into a namespace built by
    :func:`build_optimizer_namespace` using ``safe_builtins()`` (restricted
    builtins) plus only the ``ida_hexrays`` opcode/operand constants. This is
    an intentional, sandboxed use of exec analogous to the ``execute_python``
    tool — not arbitrary code execution.
    """
    code = textwrap.dedent(python_code)
    violation = check_ast(code)
    if violation:
        raise ValueError(f"Optimizer code rejected by script guard: {violation}")
    namespace = build_optimizer_namespace()
    exec(compile(code, f"<optimizer:{name}>", "exec"), namespace)

    optimize_fn = namespace.get("optimize")
    if optimize_fn is None or not callable(optimize_fn):
        raise ValueError("Code must define a callable 'optimize' function.")
    return optimize_fn
