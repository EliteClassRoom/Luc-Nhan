"""JSONL-backed store for raw knowledge memory.

Design notes
------------

* Memories, entities, and relations are **upsert by ID** — readers can
  safely reconstruct the file by ID because the IDs are deterministic
  (``mem:<cat>:<addr>:<hash>``, ``func:0x401000``, ``rel:<src>:<pred>:<dst>``).
* Observations are **append-only** (immutable event log).
* Writes are atomic per record type: read, merge by ID, write temp
  file in the same directory, then ``os.replace`` over the target.
  This makes a torn write recoverable on the next read.
* Malformed JSONL lines are skipped with a debug log so a single bad
  record from an older or newer build doesn't blow up the panel.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Iterable
from typing import Any

from ..core.atomic_io import atomic_replace
from ..core.logging import log_debug, log_error
from .paths import KnowledgePaths, relation_id
from .schema import (
    KnowledgeEntity,
    KnowledgeMemory,
    KnowledgeObservation,
    KnowledgeRelation,
)

# File handles are short-lived JSONL writes, so a small lock per file
# is enough to keep parallel writers from corrupting the file. The
# actual content is reconstructed in-memory before the atomic replace.
_FILE_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _FILE_LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            _FILE_LOCKS[path] = lock
        return lock


class KnowledgeRawStore:
    """Thin façade over the JSONL files for one :class:`KnowledgePaths`."""

    def __init__(self, paths: KnowledgePaths):
        self.paths = paths
        # Caller is responsible for ``paths.ensure()`` — we don't do it
        # in __init__ because reading may be used before any write.

    # ------------------------------------------------------------------
    # Path-level toggles
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """True if the JSONL directory exists or can be created."""
        return bool(self.paths and self.paths.idb_path and self.paths.kb_dir)

    # ------------------------------------------------------------------
    # Low-level JSONL I/O
    # ------------------------------------------------------------------

    @staticmethod
    def _read_jsonl(path: str) -> list[dict[str, Any]]:
        if not os.path.isfile(path):
            return []
        records: list[dict[str, Any]] = []
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for ln, raw in enumerate(f, 1):
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError as e:
                        # Be defensive: skip malformed lines but report.
                        log_debug(f"Skipping malformed JSONL in {path}:{ln}: {e}")
        except OSError as e:
            log_error(f"Failed to read {path}: {e}")
        return records

    @staticmethod
    def _write_jsonl_atomic(path: str, records: Iterable[dict[str, Any]]) -> None:
        """Write ``records`` to ``path`` atomically (temp + replace)."""
        parent = os.path.dirname(path) or "."
        os.makedirs(parent, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(prefix=".rikugan-tmp-", dir=parent)
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                for rec in records:
                    f.write(json.dumps(rec, ensure_ascii=False, default=str))
                    f.write("\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            atomic_replace(tmp_path, path)
        except Exception:
            # Best-effort cleanup of the temp file
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            raise

    @staticmethod
    def _append_jsonl(path: str, record: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with _lock_for(path):
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str))
                f.write("\n")
                try:
                    f.flush()
                    os.fsync(f.fileno())
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Generic upsert-by-ID
    # ------------------------------------------------------------------

    @staticmethod
    def _upsert_by_id(
        records: list[dict[str, Any]], new_record: dict[str, Any], id_field: str = "id"
    ) -> list[dict[str, Any]]:
        """Return a new list where ``new_record`` replaces any existing
        record with the same ``id_field``, or is appended.
        """
        new_id = new_record.get(id_field, "")
        out: list[dict[str, Any]] = []
        replaced = False
        for rec in records:
            if rec.get(id_field) == new_id:
                out.append(new_record)
                replaced = True
            else:
                out.append(rec)
        if not replaced:
            out.append(new_record)
        return out

    def _locked_upsert(self, path: str, record: dict[str, Any]) -> None:
        """Read-modify-write under a per-file lock.

        Hold the lock across read + merge + write so concurrent
        upserts from worker threads don't overwrite each other (the
        atomic rename alone is not enough — two threads reading the
        same stale snapshot and writing back will silently drop
        updates).
        """
        with _lock_for(path):
            existing = self._read_jsonl(path)
            merged = self._upsert_by_id(existing, record)
            self._write_jsonl_atomic(path, merged)

    # ------------------------------------------------------------------
    # Memories
    # ------------------------------------------------------------------

    def upsert_memory(self, memory: KnowledgeMemory) -> None:
        self.paths.ensure()
        self._locked_upsert(self.paths.memories_path, memory.to_dict())

    def list_memories(self) -> list[KnowledgeMemory]:
        return [KnowledgeMemory.from_dict(r) for r in self._read_jsonl(self.paths.memories_path)]

    # ------------------------------------------------------------------
    # Entities
    # ------------------------------------------------------------------

    def upsert_entity(self, entity: KnowledgeEntity) -> None:
        self.paths.ensure()
        self._locked_upsert(self.paths.entities_path, entity.to_dict())

    def list_entities(self) -> list[KnowledgeEntity]:
        return [KnowledgeEntity.from_dict(r) for r in self._read_jsonl(self.paths.entities_path)]

    def get_entity(self, entity_id: str) -> KnowledgeEntity | None:
        for ent in self.list_entities():
            if ent.id == entity_id:
                return ent
        return None

    # ------------------------------------------------------------------
    # Relations
    # ------------------------------------------------------------------

    def upsert_relation(self, relation: KnowledgeRelation) -> None:
        self.paths.ensure()
        self._locked_upsert(self.paths.relations_path, relation.to_dict())

    def list_relations(self) -> list[KnowledgeRelation]:
        return [KnowledgeRelation.from_dict(r) for r in self._read_jsonl(self.paths.relations_path)]

    def upsert_relation_from(self, src: str, predicate: str, dst: str, **kwargs: Any) -> KnowledgeRelation:
        """Helper that builds a deterministic relation ID and upserts."""
        rid = relation_id(src, predicate, dst)
        rel = KnowledgeRelation(
            id=rid,
            binary_id=self.paths.binary_id,
            src=src,
            predicate=predicate,
            dst=dst,
            evidence=kwargs.get("evidence", ""),
            confidence=kwargs.get("confidence", 0.7),
            source_refs=kwargs.get("source_refs", []),
        )
        self.upsert_relation(rel)
        return rel

    # ------------------------------------------------------------------
    # Observations (append-only)
    # ------------------------------------------------------------------

    def append_observation(self, observation: KnowledgeObservation) -> None:
        self.paths.ensure()
        self._append_jsonl(self.paths.observations_path, observation.to_dict())

    def list_observations(self) -> list[KnowledgeObservation]:
        return [KnowledgeObservation.from_dict(r) for r in self._read_jsonl(self.paths.observations_path)]

    # ------------------------------------------------------------------
    # Counts + clear (for the UI tab)
    # ------------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        return {
            "memories": len(self.list_memories()),
            "entities": len(self.list_entities()),
            "relations": len(self.list_relations()),
            "observations": len(self.list_observations()),
        }

    def count_observations(self) -> int:
        """Cheap observation count without re-reading the other JSONL files.

        Used by the Knowledge tab so it can fetch memories/entities/
        relations ONCE each, then issue a single observation count
        instead of doing a second pass through the same files
        (which ``counts()`` would do).
        """
        if not os.path.isfile(self.paths.observations_path):
            return 0
        n = 0
        try:
            with open(self.paths.observations_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.strip():
                        n += 1
        except OSError:
            return 0
        return n
