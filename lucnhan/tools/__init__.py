"""Shared tool framework: @tool decorator, ToolRegistry, and security helpers.

Host-specific tool implementations live in:
  - lucnhan.ida.tools   (IDA Pro)

Only the framework core (base) is imported at module level.
``web`` and ``web_fetch`` are imported lazily via ``__getattr__`` — they are
not resolved until an attribute access forces the import.
"""

from __future__ import annotations

import importlib
from types import ModuleType

from . import base

_LAZY_MODULES = {"web", "web_fetch"}


def __getattr__(name: str) -> ModuleType:
    if name in _LAZY_MODULES:
        mod = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = mod
        return mod
    raise AttributeError(name)


__all__ = ["base", "web", "web_fetch"]
