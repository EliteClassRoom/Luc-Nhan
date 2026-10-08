"""Central registry: workspaces, identity evidence, path aliases, legacy sources.

The registry is a single SQLite database (``registry.db``) under the central
memory root. It stores routing metadata only — not analysis facts.

Identity evidence follows the spec's ordered decision table:

* ``filesystem`` and ``raw_sha256`` evidence is unique in its current namespace.
* ``db_instance`` (netnode UUID) evidence may coexist across copied workspaces.
* Path aliases are non-authoritative many-to-one metadata.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..constants import MEMORY_REGISTRY_SCHEMA_VERSION
from .sqlite_backend import begin_immediate_with_retry, open_sqlite


class EvidenceConflictError(RuntimeError):
    """Raised when current durable evidence (filesystem/raw_sha256) is already bound."""


@dataclass(frozen=True)
class WorkspaceRecord:
    """One workspace row in the registry."""

    memory_id: str
    kind: Literal["binary", "raw"]
    state: Literal["active", "provisional", "disabled", "retired"]
    display_name: str
    created_at: float
    last_seen_at: float


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


def _migrate_v1(conn: Any) -> None:
    """Registry schema v1: workspaces, evidence, path aliases, legacy sources."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS workspaces(
            memory_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN ('binary', 'raw')),
            state TEXT NOT NULL CHECK(state IN ('active', 'provisional', 'disabled', 'retired')),
            display_name TEXT NOT NULL,
            created_at REAL NOT NULL,
            last_seen_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS identity_evidence(
            evidence_id INTEGER PRIMARY KEY,
            memory_id TEXT NOT NULL REFERENCES workspaces(memory_id),
            kind TEXT NOT NULL CHECK(kind IN ('filesystem', 'db_instance', 'raw_sha256')),
            value TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('current', 'retired', 'pending')),
            created_at REAL NOT NULL,
            retired_at REAL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_current_durable_evidence
        ON identity_evidence(kind, value)
        WHERE status = 'current' AND kind IN ('filesystem', 'raw_sha256')
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_workspace_evidence
        ON identity_evidence(memory_id, kind, value, status)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS path_aliases(
            alias_id INTEGER PRIMARY KEY,
            memory_id TEXT NOT NULL REFERENCES workspaces(memory_id),
            normalized_path TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('current', 'retired')),
            last_seen_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_path_alias
        ON path_aliases(normalized_path)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS legacy_sources(
            source_fingerprint TEXT PRIMARY KEY,
            path_metadata TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('detected', 'dismissed', 'imported')),
            last_seen_at REAL NOT NULL
        )
        """
    )


def _migrate_v2(conn: Any) -> None:
    """Registry schema v2: analysis cases and membership."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cases(
            case_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('active', 'disabled', 'deleted')),
            revision INTEGER NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS case_members(
            case_id TEXT NOT NULL REFERENCES cases(case_id),
            memory_id TEXT NOT NULL REFERENCES workspaces(memory_id),
            status TEXT NOT NULL CHECK(status IN ('current', 'removed')),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY(case_id, memory_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_case_members_memory
        ON case_members(memory_id, status)
        """
    )


_MIGRATIONS = {1: _migrate_v1, 2: _migrate_v2}


class MemoryRegistry:
    """Central identity/evidence registry backed by ``registry.db``."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)

    def initialize(self) -> None:
        """Create the registry database if it does not exist."""
        conn = open_sqlite(
            self._db_path,
            read_only=False,
            expected_version=MEMORY_REGISTRY_SCHEMA_VERSION,
            migrations=_MIGRATIONS,
            allow_create=True,
        )
        conn.close()

    def _connect(self, *, read_only: bool = False) -> Any:
        return open_sqlite(
            self._db_path,
            read_only=read_only,
            expected_version=MEMORY_REGISTRY_SCHEMA_VERSION,
            migrations=_MIGRATIONS,
        )

    # ------------------------------------------------------------------
    # Workspace CRUD
    # ------------------------------------------------------------------

    def create_workspace(
        self,
        kind: str,
        display_name: str,
        *,
        memory_id: str | None = None,
    ) -> WorkspaceRecord:
        """Create a new workspace row and return its record."""
        from .workspace import new_memory_id, validate_memory_id

        mid = memory_id or new_memory_id()
        validate_memory_id(mid)
        now = time.time()

        conn = self._connect()
        try:
            begin_immediate_with_retry(conn)
            conn.execute(
                "INSERT INTO workspaces(memory_id, kind, state, display_name, created_at, last_seen_at)"
                " VALUES(?, ?, 'active', ?, ?, ?)",
                (mid, kind, display_name, now, now),
            )
            conn.commit()
        finally:
            conn.close()
        return WorkspaceRecord(
            memory_id=mid,
            kind=kind,  # type: ignore[arg-type]
            state="active",
            display_name=display_name,
            created_at=now,
            last_seen_at=now,
        )

    def get_workspace(self, memory_id: str) -> WorkspaceRecord | None:
        """Fetch one workspace by ID."""
        conn = self._connect(read_only=True)
        try:
            row = conn.execute(
                "SELECT * FROM workspaces WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            if row is None:
                return None
            return _row_to_record(row)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Evidence binding
    # ------------------------------------------------------------------

    def bind_evidence(
        self,
        memory_id: str,
        kind: str,
        value: str,
        *,
        status: str = "current",
    ) -> None:
        """Bind an evidence row to a workspace.

        Raises ``EvidenceConflictError`` if the evidence is a current
        durable type (filesystem or raw_sha256) and already bound to
        another workspace.
        """
        now = time.time()
        conn = self._connect()
        try:
            begin_immediate_with_retry(conn)
            try:
                conn.execute(
                    "INSERT INTO identity_evidence(memory_id, kind, value, status, created_at) VALUES(?, ?, ?, ?, ?)",
                    (memory_id, kind, value, status, now),
                )
                conn.commit()
            except Exception as exc:
                conn.rollback()
                if "uq_current_durable_evidence" in str(exc) or "UNIQUE" in str(exc).upper():
                    raise EvidenceConflictError(f"{kind} evidence {value!r} is already bound as current") from exc
                raise
        finally:
            conn.close()

    def retire_evidence(self, memory_id: str, kind: str, value: str) -> None:
        """Mark a previously-bound evidence row as retired."""
        now = time.time()
        conn = self._connect()
        try:
            begin_immediate_with_retry(conn)
            conn.execute(
                "UPDATE identity_evidence SET status = 'retired', retired_at = ?"
                " WHERE memory_id = ? AND kind = ? AND value = ? AND status = 'current'",
                (now, memory_id, kind, value),
            )
            conn.commit()
        finally:
            conn.close()

    def find_evidence(self, kind: str, value: str) -> list[WorkspaceRecord]:
        """Return all workspaces bound to the given evidence (current only)."""
        conn = self._connect(read_only=True)
        try:
            rows = conn.execute(
                """
                SELECT w.* FROM workspaces w
                JOIN identity_evidence e ON e.memory_id = w.memory_id
                WHERE e.kind = ? AND e.value = ? AND e.status = 'current'
                ORDER BY w.created_at
                """,
                (kind, value),
            ).fetchall()
            return [_row_to_record(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Raw SHA-256 resolve-or-create
    # ------------------------------------------------------------------

    def find_raw(self, source_sha256: str) -> WorkspaceRecord | None:
        """Return the workspace bound to a current ``raw_sha256`` digest, or None."""
        conn = self._connect(read_only=True)
        try:
            row = conn.execute(
                """
                SELECT w.* FROM workspaces w
                JOIN identity_evidence e ON e.memory_id = w.memory_id
                WHERE e.kind = 'raw_sha256' AND e.value = ? AND e.status = 'current'
                """,
                (source_sha256,),
            ).fetchone()
            return _row_to_record(row) if row else None
        finally:
            conn.close()

    def resolve_or_create_raw(
        self,
        source_sha256: str,
        display_name: str,
    ) -> WorkspaceRecord:
        """Find or create the workspace for a raw-binary SHA-256 digest.

        Within one ``BEGIN IMMEDIATE`` transaction, queries current
        ``raw_sha256`` evidence, returns its workspace when present, or
        otherwise creates a new raw workspace plus evidence atomically.
        """
        conn = self._connect()
        try:
            begin_immediate_with_retry(conn)
            row = conn.execute(
                """
                SELECT w.* FROM workspaces w
                JOIN identity_evidence e ON e.memory_id = w.memory_id
                WHERE e.kind = 'raw_sha256' AND e.value = ? AND e.status = 'current'
                """,
                (source_sha256,),
            ).fetchone()

            if row is not None:
                conn.commit()
                return _row_to_record(row)

            from .workspace import new_memory_id

            mid = new_memory_id()
            now = time.time()
            conn.execute(
                "INSERT INTO workspaces(memory_id, kind, state, display_name, created_at, last_seen_at)"
                " VALUES(?, 'raw', 'active', ?, ?, ?)",
                (mid, display_name, now, now),
            )
            conn.execute(
                "INSERT INTO identity_evidence(memory_id, kind, value, status, created_at)"
                " VALUES(?, 'raw_sha256', ?, 'current', ?)",
                (mid, source_sha256, now),
            )
            conn.commit()
            return WorkspaceRecord(
                memory_id=mid,
                kind="raw",
                state="active",
                display_name=display_name,
                created_at=now,
                last_seen_at=now,
            )
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Path aliases
    # ------------------------------------------------------------------

    def touch_path_alias(self, memory_id: str, normalized_path: str) -> None:
        """Insert or update a path alias for a workspace."""
        now = time.time()
        conn = self._connect()
        try:
            begin_immediate_with_retry(conn)
            conn.execute(
                "UPDATE path_aliases SET status = 'retired' WHERE memory_id = ? AND status = 'current'",
                (memory_id,),
            )
            conn.execute(
                "INSERT INTO path_aliases(memory_id, normalized_path, status, last_seen_at) VALUES(?, ?, 'current', ?)",
                (memory_id, normalized_path, now),
            )
            conn.commit()
        finally:
            conn.close()

    def find_by_path(self, normalized_path: str) -> str | None:
        """Return the memory_id for the most-recent current path alias."""
        conn = self._connect(read_only=True)
        try:
            row = conn.execute(
                "SELECT memory_id FROM path_aliases"
                " WHERE normalized_path = ? AND status = 'current'"
                " ORDER BY last_seen_at DESC LIMIT 1",
                (normalized_path,),
            ).fetchone()
            return row["memory_id"] if row else None
        finally:
            conn.close()

    def find_by_path_for(self, memory_id: str) -> str | None:
        """Return the current normalized path alias for a workspace."""
        conn = self._connect(read_only=True)
        try:
            row = conn.execute(
                "SELECT normalized_path FROM path_aliases"
                " WHERE memory_id = ? AND status = 'current'"
                " ORDER BY last_seen_at DESC LIMIT 1",
                (memory_id,),
            ).fetchone()
            return row["normalized_path"] if row else None
        finally:
            conn.close()


def _row_to_record(row: Any) -> WorkspaceRecord:
    """Convert a sqlite3.Row to a WorkspaceRecord."""
    return WorkspaceRecord(
        memory_id=row["memory_id"],
        kind=row["kind"],
        state=row["state"],
        display_name=row["display_name"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
    )
