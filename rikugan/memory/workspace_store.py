"""Per-workspace SQLite store: facts, entities, relations, observations, projection state.

Each workspace has its own ``memory.db`` containing structured analysis
records. The store enforces owner validation on every open and uses
optimistic revision control for concurrent-safe upserts.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Literal

from ..constants import MEMORY_WORKSPACE_SCHEMA_VERSION
from .sqlite_backend import begin_immediate_with_retry, open_sqlite
from .workspace import WorkspacePaths, validate_record_id

_SEMANTIC_HASH_RE = re.compile(r"[0-9a-f]{64}")

FactSaveOutcome = Literal["created", "deduplicated"]


class StaleRevisionError(RuntimeError):
    """Raised when expected_revision does not match the current revision."""


# ---------------------------------------------------------------------------
# Validators (shared by put_fact and save_fact_if_semantically_absent)
# ---------------------------------------------------------------------------


def _validate_fact_type(value: str) -> None:
    if not value or not isinstance(value, str):
        raise ValueError("fact_type must be a non-empty string")


def _validate_title(value: str) -> None:
    if not isinstance(value, str):
        raise ValueError("title must be a string")


def _validate_content(value: str) -> None:
    if not isinstance(value, str):
        raise ValueError("content must be a string")


def _validate_confidence(value: float) -> None:
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError("confidence must be finite and within [0, 1]")


def _validate_semantic_hash_shape(value: str) -> None:
    if not _SEMANTIC_HASH_RE.fullmatch(value):
        raise ValueError("semantic_hash must be a 64-character lowercase SHA-256 hex digest")


# ---------------------------------------------------------------------------
# Record dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FactRecord:
    fact_id: str
    fact_type: str
    title: str
    content: str
    semantic_hash: str
    confidence: float
    revision: int
    created_at: float


@dataclass(frozen=True)
class FactRevision:
    fact_id: str
    revision: int
    content: str
    content_hash: str
    confidence: float
    created_at: float


@dataclass(frozen=True)
class EntityRecord:
    entity_id: str
    entity_type: str
    name: str
    metadata: dict[str, Any]
    revision: int


@dataclass(frozen=True)
class RelationRecord:
    relation_id: str
    subject_id: str
    predicate: str
    object_id: str
    confidence: float
    revision: int


@dataclass(frozen=True)
class ObservationRecord:
    observation_id: str
    observation_type: str
    content: str
    created_at: float


@dataclass(frozen=True)
class ProjectionState:
    managed_hash: str
    unmanaged_hash: str
    projection_dirty: bool
    projection_conflict: bool
    projected_revision: int


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


def _migrate_v1(conn: Any) -> None:
    """Workspace schema v1: facts, entities, relations, observations, projection."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS workspace_meta(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS facts(
            fact_id TEXT PRIMARY KEY,
            fact_type TEXT NOT NULL,
            title TEXT NOT NULL,
            current_revision INTEGER NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fact_revisions(
            fact_id TEXT NOT NULL REFERENCES facts(fact_id),
            revision INTEGER NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            confidence REAL NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY(fact_id, revision)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entities(
            entity_id TEXT PRIMARY KEY,
            entity_type TEXT NOT NULL,
            name TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}',
            current_revision INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS relations(
            relation_id TEXT PRIMARY KEY,
            subject_id TEXT NOT NULL REFERENCES entities(entity_id),
            predicate TEXT NOT NULL,
            object_id TEXT NOT NULL REFERENCES entities(entity_id),
            confidence REAL NOT NULL,
            current_revision INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS observations(
            observation_id TEXT PRIMARY KEY,
            observation_type TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS projection_state(
            id INTEGER PRIMARY KEY CHECK(id = 1),
            managed_hash TEXT NOT NULL DEFAULT '',
            unmanaged_hash TEXT NOT NULL DEFAULT '',
            projection_dirty INTEGER NOT NULL DEFAULT 0,
            projection_conflict INTEGER NOT NULL DEFAULT 0,
            projected_revision INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute("INSERT OR IGNORE INTO projection_state(id) VALUES(1)")


def _migrate_v2(conn: Any) -> None:
    """Workspace schema v2: add semantic_hash column and hash guards."""
    from .fact_identity import semantic_fact_hash

    conn.execute(
        "ALTER TABLE facts ADD COLUMN semantic_hash TEXT CHECK(semantic_hash IS NULL OR length(semantic_hash) = 64)"
    )
    rows = conn.execute(
        """
        SELECT f.fact_id, f.fact_type, r.content
        FROM facts f
        JOIN fact_revisions r
          ON r.fact_id = f.fact_id AND r.revision = f.current_revision
        """
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE facts SET semantic_hash = ? WHERE fact_id = ?",
            (semantic_fact_hash(row["fact_type"], row["content"]), row["fact_id"]),
        )
    # Do not use Connection.executescript() here: CPython sqlite3 may commit
    # the outer migration transaction before executing the script.
    conn.execute("CREATE INDEX idx_facts_semantic ON facts(semantic_hash)")
    conn.execute(
        """
        CREATE TRIGGER facts_semantic_hash_insert_guard
        BEFORE INSERT ON facts
        WHEN NEW.semantic_hash IS NULL
          OR length(NEW.semantic_hash) != 64
          OR NEW.semantic_hash GLOB '*[^0-9a-f]*'
        BEGIN
          SELECT RAISE(ABORT, 'semantic_hash must be a 64-character lowercase SHA-256 hex digest');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER facts_semantic_hash_update_guard
        BEFORE UPDATE OF semantic_hash ON facts
        WHEN NEW.semantic_hash IS NULL
          OR length(NEW.semantic_hash) != 64
          OR NEW.semantic_hash GLOB '*[^0-9a-f]*'
        BEGIN
          SELECT RAISE(ABORT, 'semantic_hash must be a 64-character lowercase SHA-256 hex digest');
        END
        """
    )
    invalid = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE semantic_hash IS NULL OR semantic_hash GLOB '*[^0-9a-f]*' OR length(semantic_hash) != 64"
    ).fetchone()[0]
    if invalid:
        raise RuntimeError(f"semantic hash backfill left {invalid} invalid fact(s)")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("workspace v2 migration failed foreign key check")


_MIGRATIONS = {1: _migrate_v1, 2: _migrate_v2}


# ---------------------------------------------------------------------------
# WorkspaceStore
# ---------------------------------------------------------------------------


class WorkspaceStore:
    """SQLite-backed store for one workspace's structured records."""

    def __init__(self, conn: sqlite3.Connection, paths: WorkspacePaths) -> None:
        self._conn = conn
        self._paths = paths

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        paths: WorkspacePaths,
        owner_memory_id: str,
        *,
        workspace_kind: str = "binary",
    ) -> WorkspaceStore:
        """Create a new workspace database with the given owner."""
        conn = open_sqlite(
            paths.database,
            read_only=False,
            expected_version=MEMORY_WORKSPACE_SCHEMA_VERSION,
            migrations=_MIGRATIONS,
            allow_create=True,
        )
        # Store owner metadata
        conn.execute(
            "INSERT OR REPLACE INTO workspace_meta(key, value) VALUES('owner_memory_id', ?)",
            (owner_memory_id,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO workspace_meta(key, value) VALUES('workspace_kind', ?)",
            (workspace_kind,),
        )
        conn.commit()
        return cls(conn, paths)

    @classmethod
    def open(
        cls,
        paths: WorkspacePaths,
        owner_memory_id: str,
        *,
        read_only: bool = False,
    ) -> WorkspaceStore:
        """Open an existing workspace database.

        Raises ``FileNotFoundError`` if the database does not exist.
        """
        conn = open_sqlite(
            paths.database,
            read_only=read_only,
            expected_version=MEMORY_WORKSPACE_SCHEMA_VERSION,
            migrations=_MIGRATIONS,
        )
        # Validate owner
        row = conn.execute("SELECT value FROM workspace_meta WHERE key = 'owner_memory_id'").fetchone()
        if row is None or row["value"] != owner_memory_id:
            conn.close()
            raise ValueError(f"workspace owner mismatch: expected {owner_memory_id}")
        return cls(conn, paths)

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def __enter__(self) -> WorkspaceStore:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Facts
    # ------------------------------------------------------------------

    def put_fact(
        self,
        fact_id: str,
        fact_type: str,
        title: str,
        content: str,
        confidence: float,
        *,
        semantic_hash: str | None = None,
        expected_revision: int,
    ) -> FactRecord:
        """Insert or update a fact with optimistic revision control.

        Raises ``StaleRevisionError`` if *expected_revision* does not match
        the current revision.
        """
        validate_record_id("fact", fact_id)
        _validate_fact_type(fact_type)
        _validate_title(title)
        _validate_content(content)
        _validate_confidence(confidence)

        from .fact_identity import semantic_fact_hash

        current_semantic_hash = semantic_hash or semantic_fact_hash(fact_type, content)
        _validate_semantic_hash_shape(current_semantic_hash)

        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        now = time.time()

        begin_immediate_with_retry(self._conn)
        try:
            row = self._conn.execute(
                "SELECT current_revision FROM facts WHERE fact_id = ?",
                (fact_id,),
            ).fetchone()
            current = int(row["current_revision"]) if row is not None else 0

            if current != expected_revision:
                raise StaleRevisionError(f"expected revision {expected_revision}, found {current}")

            revision = current + 1

            if current == 0:
                self._conn.execute(
                    "INSERT INTO facts(fact_id, fact_type, title, semantic_hash, current_revision, created_at)"
                    " VALUES(?, ?, ?, ?, ?, ?)",
                    (fact_id, fact_type, title, current_semantic_hash, revision, now),
                )
            else:
                self._conn.execute(
                    "UPDATE facts SET fact_type = ?, title = ?, semantic_hash = ?, current_revision = ?"
                    " WHERE fact_id = ?",
                    (fact_type, title, current_semantic_hash, revision, fact_id),
                )

            self._conn.execute(
                "INSERT INTO fact_revisions(fact_id, revision, content, content_hash, confidence, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?)",
                (fact_id, revision, content, content_hash, confidence, now),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

        return FactRecord(
            fact_id=fact_id,
            fact_type=fact_type,
            title=title,
            content=content,
            semantic_hash=current_semantic_hash,
            confidence=confidence,
            revision=revision,
            created_at=now,
        )

    def get_fact(self, fact_id: str) -> FactRecord | None:
        """Get a fact by ID, returning the current revision."""
        row = self._conn.execute(
            """
            SELECT f.fact_id, f.fact_type, f.title, f.semantic_hash,
                   f.current_revision, f.created_at,
                   r.content, r.confidence
            FROM facts f
            JOIN fact_revisions r ON r.fact_id = f.fact_id AND r.revision = f.current_revision
            WHERE f.fact_id = ?
            """,
            (fact_id,),
        ).fetchone()
        if row is None:
            return None
        return FactRecord(
            fact_id=row["fact_id"],
            fact_type=row["fact_type"],
            title=row["title"],
            content=row["content"],
            semantic_hash=row["semantic_hash"],
            confidence=row["confidence"],
            revision=row["current_revision"],
            created_at=row["created_at"],
        )

    def list_facts(self) -> list[FactRecord]:
        """List all current facts."""
        rows = self._conn.execute(
            """
            SELECT f.fact_id, f.fact_type, f.title, f.semantic_hash,
                   f.current_revision, f.created_at,
                   r.content, r.confidence
            FROM facts f
            JOIN fact_revisions r ON r.fact_id = f.fact_id AND r.revision = f.current_revision
            ORDER BY f.created_at
            """
        ).fetchall()
        return [
            FactRecord(
                fact_id=row["fact_id"],
                fact_type=row["fact_type"],
                title=row["title"],
                content=row["content"],
                semantic_hash=row["semantic_hash"],
                confidence=row["confidence"],
                revision=row["current_revision"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def save_fact_if_semantically_absent(
        self,
        *,
        fact_id: str,
        fact_type: str,
        title: str,
        content: str,
        semantic_hash: str,
        confidence: float,
        observation_id: str,
        observation_type: str,
        observation_payload: str,
    ) -> tuple[FactRecord, FactSaveOutcome]:
        """Atomically dedup + insert + observation append under one transaction.

        Looks up by ``semantic_hash`` (indexed), verifies canonical equality
        of ``fact_type`` and ``content`` against any hash match (defense
        against SHA-256 collisions), inserts the new fact and its first
        revision if absent, appends the observation in both branches, and
        commits a single ``BEGIN IMMEDIATE`` transaction.

        Returns the resulting :class:`FactRecord` and the outcome
        (``"created"`` or ``"deduplicated"``).
        """
        from .fact_identity import canonicalize_fact_content, canonicalize_fact_type

        validate_record_id("fact", fact_id)
        validate_record_id("observation", observation_id)
        _validate_fact_type(fact_type)
        _validate_title(title)
        _validate_content(content)
        _validate_confidence(confidence)
        _validate_semantic_hash_shape(semantic_hash)

        begin_immediate_with_retry(self._conn)
        try:
            rows = self._conn.execute(
                """
                SELECT f.fact_id, f.fact_type, f.title, f.semantic_hash,
                       f.current_revision, f.created_at, r.content, r.confidence
                FROM facts f
                JOIN fact_revisions r
                  ON r.fact_id = f.fact_id AND r.revision = f.current_revision
                WHERE f.semantic_hash = ?
                ORDER BY f.created_at, f.fact_id
                """,
                (semantic_hash,),
            ).fetchall()
            match = next(
                (
                    row
                    for row in rows
                    if canonicalize_fact_type(row["fact_type"]) == canonicalize_fact_type(fact_type)
                    and canonicalize_fact_content(row["content"]) == canonicalize_fact_content(content)
                ),
                None,
            )
            if match is None:
                # Insert facts + revision 1 directly inside this transaction;
                # do not call put_fact(), which starts its own transaction.
                now = time.time()
                self._conn.execute(
                    "INSERT INTO facts(fact_id, fact_type, title, semantic_hash, current_revision, created_at)"
                    " VALUES(?, ?, ?, ?, 1, ?)",
                    (fact_id, fact_type, title, semantic_hash, now),
                )
                self._conn.execute(
                    "INSERT INTO fact_revisions(fact_id, revision, content, content_hash, confidence, created_at)"
                    " VALUES(?, 1, ?, ?, ?, ?)",
                    (
                        fact_id,
                        content,
                        hashlib.sha256(content.encode("utf-8")).hexdigest(),
                        confidence,
                        now,
                    ),
                )
                selected_id = fact_id
                outcome: FactSaveOutcome = "created"
            else:
                selected_id = match["fact_id"]
                outcome = "deduplicated"
            payload = json.loads(observation_payload)
            payload["fact_id"] = selected_id
            payload["outcome"] = outcome
            self._conn.execute(
                "INSERT INTO observations(observation_id, observation_type, content, created_at) VALUES(?, ?, ?, ?)",
                (
                    observation_id,
                    observation_type,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    time.time(),
                ),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

        record = self.get_fact(selected_id)
        if record is None:
            raise RuntimeError(f"saved fact disappeared after commit: {selected_id}")
        return record, outcome

    # ------------------------------------------------------------------
    # Entities
    # ------------------------------------------------------------------

    def put_entity(
        self,
        entity_id: str,
        entity_type: str,
        name: str,
        metadata: dict[str, Any] | None = None,
    ) -> EntityRecord:
        """Insert or update an entity."""
        validate_record_id("entity", entity_id)
        meta_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        now = time.time()

        begin_immediate_with_retry(self._conn)
        try:
            row = self._conn.execute(
                "SELECT current_revision FROM entities WHERE entity_id = ?",
                (entity_id,),
            ).fetchone()
            revision = (int(row["current_revision"]) if row else 0) + 1

            if row is None:
                self._conn.execute(
                    "INSERT INTO entities(entity_id, entity_type, name, metadata, current_revision, created_at)"
                    " VALUES(?, ?, ?, ?, ?, ?)",
                    (entity_id, entity_type, name, meta_json, revision, now),
                )
            else:
                self._conn.execute(
                    "UPDATE entities SET entity_type = ?, name = ?, metadata = ?, current_revision = ?"
                    " WHERE entity_id = ?",
                    (entity_type, name, meta_json, revision, entity_id),
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

        return EntityRecord(
            entity_id=entity_id,
            entity_type=entity_type,
            name=name,
            metadata=metadata or {},
            revision=revision,
        )

    def get_entity(self, entity_id: str) -> EntityRecord | None:
        """Get an entity by ID."""
        row = self._conn.execute(
            "SELECT * FROM entities WHERE entity_id = ?",
            (entity_id,),
        ).fetchone()
        if row is None:
            return None
        return EntityRecord(
            entity_id=row["entity_id"],
            entity_type=row["entity_type"],
            name=row["name"],
            metadata=json.loads(row["metadata"]),
            revision=row["current_revision"],
        )

    # ------------------------------------------------------------------
    # Relations
    # ------------------------------------------------------------------

    def put_relation(
        self,
        relation_id: str,
        subject_id: str,
        predicate: str,
        object_id: str,
        confidence: float,
    ) -> RelationRecord:
        """Insert or update a relation between two entities."""
        validate_record_id("relation", relation_id)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be finite and within [0, 1]")
        now = time.time()

        begin_immediate_with_retry(self._conn)
        try:
            row = self._conn.execute(
                "SELECT current_revision FROM relations WHERE relation_id = ?",
                (relation_id,),
            ).fetchone()
            revision = (int(row["current_revision"]) if row else 0) + 1

            if row is None:
                self._conn.execute(
                    "INSERT INTO relations(relation_id, subject_id, predicate, object_id, confidence, current_revision, created_at)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (relation_id, subject_id, predicate, object_id, confidence, revision, now),
                )
            else:
                self._conn.execute(
                    "UPDATE relations SET subject_id = ?, predicate = ?, object_id = ?, confidence = ?, current_revision = ?"
                    " WHERE relation_id = ?",
                    (subject_id, predicate, object_id, confidence, revision, relation_id),
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

        return RelationRecord(
            relation_id=relation_id,
            subject_id=subject_id,
            predicate=predicate,
            object_id=object_id,
            confidence=confidence,
            revision=revision,
        )

    def list_relations(self) -> list[RelationRecord]:
        """List all current relations."""
        rows = self._conn.execute("SELECT * FROM relations ORDER BY created_at").fetchall()
        return [
            RelationRecord(
                relation_id=row["relation_id"],
                subject_id=row["subject_id"],
                predicate=row["predicate"],
                object_id=row["object_id"],
                confidence=row["confidence"],
                revision=row["current_revision"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Observations
    # ------------------------------------------------------------------

    def append_observation(
        self,
        observation_id: str,
        observation_type: str,
        content: str,
    ) -> ObservationRecord:
        """Append an immutable observation record."""
        validate_record_id("observation", observation_id)
        now = time.time()
        begin_immediate_with_retry(self._conn)
        try:
            self._conn.execute(
                "INSERT INTO observations(observation_id, observation_type, content, created_at) VALUES(?, ?, ?, ?)",
                (observation_id, observation_type, content, now),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

        return ObservationRecord(
            observation_id=observation_id,
            observation_type=observation_type,
            content=content,
            created_at=now,
        )

    def count_observations(self) -> int:
        """Count all observations."""
        row = self._conn.execute("SELECT COUNT(*) AS cnt FROM observations").fetchone()
        return int(row["cnt"])

    # ------------------------------------------------------------------
    # Projection state
    # ------------------------------------------------------------------

    def projection_state(self) -> ProjectionState:
        """Return the current projection state."""
        row = self._conn.execute("SELECT * FROM projection_state WHERE id = 1").fetchone()
        return ProjectionState(
            managed_hash=row["managed_hash"],
            unmanaged_hash=row["unmanaged_hash"],
            projection_dirty=bool(row["projection_dirty"]),
            projection_conflict=bool(row["projection_conflict"]),
            projected_revision=row["projected_revision"],
        )

    def mark_projection_dirty(self) -> None:
        """Mark the projection as dirty (needs regeneration)."""
        begin_immediate_with_retry(self._conn)
        try:
            self._conn.execute("UPDATE projection_state SET projection_dirty = 1 WHERE id = 1")
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def mark_projection_clean(
        self,
        *,
        managed_hash: str,
        unmanaged_hash: str,
        projected_revision: int,
    ) -> None:
        """Mark the projection as clean after successful regeneration."""
        begin_immediate_with_retry(self._conn)
        try:
            self._conn.execute(
                "UPDATE projection_state SET projection_dirty = 0, projection_conflict = 0,"
                " managed_hash = ?, unmanaged_hash = ?, projected_revision = ?"
                " WHERE id = 1",
                (managed_hash, unmanaged_hash, projected_revision),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def mark_projection_conflict(self) -> None:
        """Mark the projection as having a conflict."""
        begin_immediate_with_retry(self._conn)
        try:
            self._conn.execute("UPDATE projection_state SET projection_conflict = 1 WHERE id = 1")
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
