"""Raw knowledge memory subsystem for Luc Nhan.

Stores structured analysis knowledge (memories, entities, relations,
observations) as JSONL files under ``<idb_dir>/.lucnhan-kb/`` and
human-readable Markdown notes under ``<idb_dir>/notes/``.

This module is intentionally host-agnostic — the IDA-specific dispatch
and Qt widgets live elsewhere. Only I/O, parsing, ingestion, retrieval,
and report-context building happen here.

Storage layout
--------------
::

    <idb_dir>/
    ├── notes/
    │   ├── index.md
    │   ├── functions/
    │   ├── findings/
    │   ├── data-structures/
    │   ├── iocs.md
    │   └── reports/
    └── .lucnhan-kb/
        ├── memories.jsonl
        ├── entities.jsonl
        ├── relations.jsonl
        ├── observations.jsonl
        └── meta.json
"""

from __future__ import annotations

from .paths import KnowledgePaths
from .raw_store import KnowledgeRawStore
from .schema import KnowledgeEntity, KnowledgeMemory, KnowledgeObservation, KnowledgeRelation

__all__ = [
    "KnowledgeEntity",
    "KnowledgeMemory",
    "KnowledgeObservation",
    "KnowledgePaths",
    "KnowledgeRawStore",
    "KnowledgeRelation",
]
