"""Shared helpers for the knowledge-store test suite.

The tests under ``rikugan/tests/knowledge`` historically defined their
own ``fresh_store`` / ``fresh`` helpers inline.  The drift caused
filename and behavior differences (some used ``x.i64``, others
``x.idb``) that made it harder to grep for fixture changes.

This module pins the canonical IDB filename (``x.idb``) and the
canonical store-construction helper so every test pulls from the
same source of truth.
"""

from __future__ import annotations

import os

from rikugan.memory.paths import KnowledgePaths, knowledge_paths
from rikugan.memory.raw_store import KnowledgeRawStore

# Canonical IDB filename used by the knowledge-store test suite.
# ``.idb`` is what ``HeadlessSessionController`` and the IDA fallback
# pass to ``make_store``, so the tests mirror that input shape.
CANONICAL_IDB_NAME = "x.idb"


def fresh_store(tmp: str) -> tuple[KnowledgeRawStore, KnowledgePaths]:
    """Build a real ``(store, paths)`` pair rooted at *tmp*.

    Returns a :class:`KnowledgeRawStore` whose JSONL + ``notes/``
    directories are created on disk.  ``tmp`` must be an existing
    directory (the test fixture's ``tempfile.mkdtemp()`` output).
    """
    paths = knowledge_paths(os.path.join(tmp, CANONICAL_IDB_NAME))
    paths.ensure()
    return KnowledgeRawStore(paths), paths


__all__ = ["CANONICAL_IDB_NAME", "fresh_store"]
