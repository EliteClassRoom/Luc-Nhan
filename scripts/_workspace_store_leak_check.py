"""WorkspaceStore leak perf evidence: 100 iterations, GC refcount flat.

Run: python scripts/_workspace_store_leak_check.py

Asserts that after 100 sequential agent runs on the same memory.db, the
total count of live ``WorkspaceStore`` instances is bounded by the number
of ``_memory_stores`` slots (== number of tabs). Without the fix, every
iteration would leak one instance and the count would grow linearly.
"""

from __future__ import annotations

import gc
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

# Make sure we use the local rikugan checkout, not a system install.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rikugan.core.config import RikuganConfig  # noqa: E402
from rikugan.ida.ui.session_controller import IdaSessionController  # noqa: E402
from rikugan.memory.workspace import FilesystemIdentity  # noqa: E402
from rikugan.state.history import SessionHistory  # noqa: E402


def _live_workspace_store_count() -> int:
    return sum(1 for o in gc.get_objects() if type(o).__name__ == "WorkspaceStore")


def main() -> int:
    import rikugan.memory.identity as _ident_mod

    _ident_mod.get_filesystem_identity = lambda _p: FilesystemIdentity("vol", "v")

    tmp = tempfile.mkdtemp()
    cfg = RikuganConfig()
    cfg._config_dir = tmp
    ctrl = IdaSessionController(cfg)
    ctrl._idb_path = "/fake/test.i64"
    ctrl._db_instance_id = "a" * 32
    session = ctrl.session
    session.idb_path = ctrl._idb_path
    session.db_instance_id = ctrl._db_instance_id

    ITERS = 100

    # Warmup — first wire seeds the DB (create path).
    ctrl._wire_central_memory(MagicMock())
    gc.collect()
    gc.collect()
    baseline = _live_workspace_store_count()

    for i in range(ITERS):
        ctrl._wire_central_memory(MagicMock())
        if i % 25 == 0:
            gc.collect()
            n = _live_workspace_store_count()
            print(f"after iter {i:3d}: live WorkspaceStore refs = {n}")

    ctrl.on_agent_finished()
    gc.collect()
    gc.collect()
    final = _live_workspace_store_count()

    print()
    print(f"baseline (after warmup):              {baseline}")
    print(f"final  (after {ITERS} wires + finish): {final}")
    print(f"delta:                                {final - baseline}")
    print(f"controller._memory_stores:            {len(ctrl._memory_stores)}")

    SessionHistory.flush_saves()
    ctrl.shutdown()
    gc.collect()

    # Pass criterion: 100 wires on a single tab leaves a bounded number
    # of live refs. Before the fix, every wire leaked one ref and the
    # count would grow ~linearly to 100. After the fix, every prior
    # store is closed on swap and the active one is closed on the
    # final on_agent_finished, so final <= baseline (one less, not more).
    ok = final <= baseline
    print()
    print("RESULT:", "PASS (no leak)" if ok else "FAIL (refcount grew)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
