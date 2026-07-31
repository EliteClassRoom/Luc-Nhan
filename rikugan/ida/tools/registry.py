"""IDA tool registry: wires IDA-specific tool modules into the shared ToolRegistry.

Boot-critical tools (read-only, no optional dependencies) are registered
eagerly during panel construction.  Advanced tools (decompiler, microcode,
types, scripting, web) are deferred until first agent turn or first tool
schema build, reducing cold-open import cost and avoiding pulling heavy
Hex-Rays / type / network dependencies before the UI appears.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rikugan.core.host import HAS_HEXRAYS, has_ida_ui
from rikugan.core.thread_safety import idasync
from rikugan.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Boot-critical tool modules — imported eagerly.
# These are the read-only foundation tools most agents rely on.
# ---------------------------------------------------------------------------
from . import (
    annotations,
    database,
    disassembly,
    file_io,
    functions,
    navigation,
    strings,
    xrefs,
)
_BOOT_TOOL_MODULES = (
    navigation,
    functions,
    strings,
    database,
    disassembly,
    xrefs,
    annotations,
    file_io,
)



# ---------------------------------------------------------------------------
# Advanced tool modules — imported lazily on first tool schema build.
# These pull in decompiler, microcode, types, scripting, and web modules.
# ---------------------------------------------------------------------------

_ADVANCED_MODULE_NAMES = (
    "rikugan.ida.tools.decompiler",
    "rikugan.ida.tools.types_tools",
    "rikugan.ida.tools.scripting",
    "rikugan.ida.tools.microcode",
    "rikugan.ida.tools.emulation",
    "rikugan.tools.web",
    "rikugan.tools.web_fetch",
    "rikugan.tools.idapython_docs",
)

# ---------------------------------------------------------------------------
# Failed-module tracking — persisted across registration attempts so that
# only previously-failed modules are retried on subsequent prompts or
# settings reloads.
# ---------------------------------------------------------------------------

_failed_advanced_modules: list[str] = []


@dataclass(frozen=True)
class AdvancedToolRegistrationResult:
    """Result of a deferred advanced tool registration call.

    Attributes are immutable; callers check ``ok`` or inspect
    ``failed_modules`` / ``registered`` to decide whether to retry.
    """

    registered: int = 0
    failed_modules: list[str] = field(default_factory=list)
    skipped_modules: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed_modules


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_default_registry(
    dispatch_wrapper: Callable[..., Any] | None = None,
    ida_ui: bool | None = None,
) -> ToolRegistry:
    """Create a registry with built-in IDA tools.

    Parameters
    ----------
    dispatch_wrapper:
        Optional callable that wraps tool handlers for main-thread
        dispatch.  Defaults to ``idasync`` in UI mode.  Headless
        controllers pass their ``IdaHeadlessDispatcher.wrap``.
    ida_ui:
        Whether IDA has an interactive UI.  Defaults to auto-detect
        via ``has_ida_ui()``.

    Boot-critical tools are registered immediately.  Advanced tools are
    deferred — call ``register_advanced_tools(registry)`` before the
    first agent turn to ensure all tools are available.
    """
    if dispatch_wrapper is None:
        dispatch_wrapper = idasync
    if ida_ui is None:
        ida_ui = has_ida_ui()
    registry = ToolRegistry(dispatch_wrapper=dispatch_wrapper)
    registry.set_capabilities({"hexrays": HAS_HEXRAYS, "ida_ui": ida_ui})
    for mod in _BOOT_TOOL_MODULES:
        registry.register_module(mod)
    return registry


def register_advanced_tools(registry: ToolRegistry) -> AdvancedToolRegistrationResult:
    """Import and register all advanced (deferred) tool modules.

    Returns an ``AdvancedToolRegistrationResult`` with per-module status.
    Only previously-failed module names are retried on subsequent calls
    so that successful registrations are not reloaded unnecessarily.

    Per-module failures are logged at WARNING level (not debug) so
    operators are aware when optional tools are missing.  Registration
    is idempotent — re-registration overwrites existing entries.
    """
    import importlib

    from rikugan.core.logging import log_warning
    from rikugan.core.startup_timing import end, start

    global _failed_advanced_modules

    t_adv = start("tools.register_advanced")
    count = 0
    failed_modules: list[str] = []

    # Determine which modules to try: on retry, only the previously-failed
    # ones; on first call, all advanced module names.
    if _failed_advanced_modules:
        target_modules = [m for m in _ADVANCED_MODULE_NAMES if m in _failed_advanced_modules]
    else:
        target_modules = list(_ADVANCED_MODULE_NAMES)

    for mod_name in target_modules:
        try:
            mod = importlib.import_module(mod_name)
            registry.register_module(mod)
            tools_in_mod = len(
                [
                    n
                    for n in dir(mod)
                    if callable(getattr(mod, n, None))
                    and getattr(getattr(mod, n, None), "_tool_definition", None) is not None
                ]
            )
            count += tools_in_mod
        except Exception as e:
            log_warning(f"Failed to register advanced tool module {mod_name}: {e}")
            failed_modules.append(mod_name)

    # Persist failed modules for retry on next prompt/settings-reload,
    # but clear previously-failed modules that succeeded this time.
    _failed_advanced_modules = list(failed_modules)

    end("tools.register_advanced", t_adv)
    return AdvancedToolRegistrationResult(
        registered=count,
        failed_modules=failed_modules,
    )


def reset_failed_advanced_modules() -> None:
    """Clear the failed-module cache so ALL advanced modules are retried.

    Called on full settings reload when environment may have changed.
    """
    global _failed_advanced_modules
    _failed_advanced_modules.clear()
