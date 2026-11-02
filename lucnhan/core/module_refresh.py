"""Refresh ``lucnhan.constants`` in a live IDA session.

Why this exists
---------------
Python caches every imported module in ``sys.modules``.  The plugin runs
straight from a source checkout — the IDA plugin directory is a junction
to the repository — so editing a module while IDA is running leaves the
session with a mix of versions: modules imported *before* the edit keep
their old contents, modules imported *after* it pick up the new ones.

Adding a constant to ``lucnhan.constants`` and opening the panel in the
same IDA session therefore dies with::

    ImportError: cannot import name 'SUBAGENT_CONTEXT_WINDOW' from 'lucnhan.constants'

because the cached module predates the edit while the freshly imported
consumer (``lucnhan.agent.subagent``) expects the new name.  The file on
disk is already correct — only the in-memory copy is stale.

``refresh_constants_module()`` reloads ``lucnhan.constants`` *in place*
— the same module object with a refreshed namespace — so every later
import sees the current source and the session keeps working without an
IDA restart.

Only the constants module is reloaded.  It exports immutable values
only, so a refresh cannot invalidate an ``isinstance`` check or an
``except`` clause the way reloading a module that defines classes
would.
"""

from __future__ import annotations

import importlib
import sys

from .logging import log_debug

#: The module cross-module constants are imported from.
CONSTANTS_MODULE = "lucnhan.constants"


def refresh_module(module_name: str) -> bool:
    """Reload ``module_name`` in place when it is already imported.

    Returns True when the module was reloaded, False when it was not
    imported yet (nothing to refresh) or when the reload failed.  A
    broken source file must not take the session down, so a failed
    reload is logged and the previously loaded module is kept.

    Only pass side-effect-free modules (immutable data only): reloading
    a module that defines classes creates a second copy of every class,
    which breaks ``isinstance`` checks and ``except`` clauses across the
    two versions.
    """
    module = sys.modules.get(module_name)
    if module is None:
        return False
    try:
        importlib.reload(module)
    except Exception as exc:
        log_debug(f"module refresh failed for {module_name}: {exc}")
        return False
    return True


def refresh_constants_module() -> bool:
    """Refresh ``lucnhan.constants`` so later imports see the current source."""
    return refresh_module(CONSTANTS_MODULE)
