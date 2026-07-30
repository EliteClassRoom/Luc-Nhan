"""Case repository: CRUD, membership, and revision-safe operations for analysis cases.

Operates on the central ``registry.db``. Each membership mutation
increments the case revision within the same ``BEGIN IMMEDIATE``
transaction so concurrent writers cannot create lost updates.
"""

from __future__ import annotations

import time
from typing import Any

from .case_schema import (
    CaseMember,
    CaseRecord,
    CaseRelation,
    CaseRelationType,
    PromotionSource,
    canonicalize_relation_endpoints,
    validate_case_relation,
)
from .registry import MemoryRegistry
from .sqlite_backend import begin_immediate_with_retry
from .workspace import MemoryLocator, new_case_id, new_record_id


class CaseRepository:
    """Repository for analysis case CRUD and membership."""

    def __init__(self, registry: MemoryRegistry, locator: MemoryLocator) -> None:
        self._registry = registry
        self._locator = locator

    @property
    def locator(self) -> MemoryLocator:
        """Memory locator for case workspace path resolution."""
        return self._locator

    def _connect(self, *, read_only: bool = False) -> Any:
        return self._registry._connect(read_only=read_only)

    # ------------------------------------------------------------------
    # Case CRUD
    # ------------------------------------------------------------------

    def create_case(self, name: str) -> CaseRecord:
        """Create a new analysis case."""
        cid = new_case_id()
        now = time.time()
        conn = self._connect()
        try:
            begin_immediate_with_retry(conn)
            conn.execute(
                "INSERT INTO cases(case_id, name, state, revision, created_at, updated_at)"
                " VALUES(?, ?, 'active', 1, ?, ?)",
                (cid, name, now, now),
            )
            conn.commit()
        finally:
            conn.close()
        return CaseRecord(
            case_id=cid,
            name=name,
            state="active",
            revision=1,
            created_at=now,
            updated_at=now,
        )

    def get_case(self, case_id: str) -> CaseRecord | None:
        """Get a case by ID."""
        conn = self._connect(read_only=True)
        try:
            row = conn.execute(
                "SELECT * FROM cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
            return _row_to_case_record(row) if row else None
        finally:
            conn.close()

    def list_cases(self, include_deleted: bool = False) -> list[CaseRecord]:
        """List all cases."""
        conn = self._connect(read_only=True)
        try:
            if include_deleted:
                rows = conn.execute("SELECT * FROM cases ORDER BY created_at").fetchall()
            else:
                rows = conn.execute("SELECT * FROM cases WHERE state != 'deleted' ORDER BY created_at").fetchall()
            return [_row_to_case_record(r) for r in rows]
        finally:
            conn.close()

    def rename_case(
        self,
        case_id: str,
        name: str,
        expected_case_revision: int,
    ) -> CaseRecord:
        """Rename a case, incrementing its revision."""
        return self._mutate_case(case_id, expected_case_revision, name=name)

    def soft_delete_case(self, case_id: str, expected_case_revision: int) -> CaseRecord:
        """Soft-delete a case (sets state='deleted', increments revision)."""
        return self._mutate_case(case_id, expected_case_revision, state="deleted")

    def _mutate_case(
        self,
        case_id: str,
        expected_revision: int,
        name: str | None = None,
        state: str | None = None,
    ) -> CaseRecord:
        now = time.time()
        conn = self._connect()
        try:
            begin_immediate_with_retry(conn)
            current = conn.execute(
                "SELECT revision FROM cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
            if current is None:
                raise ValueError(f"case not found: {case_id}")
            if int(current["revision"]) != expected_revision:
                raise ValueError(f"stale case revision: expected {expected_revision}, found {current['revision']}")
            new_rev = expected_revision + 1
            sets = ["revision = ?", "updated_at = ?"]
            params: list[Any] = [new_rev, now]
            if name is not None:
                sets.append("name = ?")
                params.append(name)
            if state is not None:
                sets.append("state = ?")
                params.append(state)
            params.extend([case_id, expected_revision])
            conn.execute(
                f"UPDATE cases SET {', '.join(sets)} WHERE case_id = ? AND revision = ?",
                params,
            )
            conn.commit()
        finally:
            conn.close()

        result = self.get_case(case_id)
        assert result is not None
        return result

    # ------------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------------

    def add_member(
        self,
        case_id: str,
        memory_id: str,
        expected_case_revision: int,
    ) -> CaseMember:
        """Add a binary to a case. Reactivates removed members."""
        now = time.time()
        conn = self._connect()
        try:
            begin_immediate_with_retry(conn)

            # Bump case revision atomically with membership change
            current = conn.execute(
                "SELECT revision FROM cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
            if current is None:
                raise ValueError(f"case not found: {case_id}")
            if int(current["revision"]) != expected_case_revision:
                raise ValueError("stale case revision")

            new_rev = expected_case_revision + 1
            conn.execute(
                "UPDATE cases SET revision = ?, updated_at = ? WHERE case_id = ?",
                (new_rev, now, case_id),
            )

            # Check if member already exists
            existing = conn.execute(
                "SELECT status FROM case_members WHERE case_id = ? AND memory_id = ?",
                (case_id, memory_id),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO case_members(case_id, memory_id, status, created_at, updated_at)"
                    " VALUES(?, ?, 'current', ?, ?)",
                    (case_id, memory_id, now, now),
                )
            elif existing["status"] != "current":
                conn.execute(
                    "UPDATE case_members SET status = 'current', updated_at = ? WHERE case_id = ? AND memory_id = ?",
                    (now, case_id, memory_id),
                )
            conn.commit()
        finally:
            conn.close()
        return CaseMember(
            case_id=case_id,
            memory_id=memory_id,
            status="current",
            created_at=now,
            updated_at=now,
        )

    def remove_member(
        self,
        case_id: str,
        memory_id: str,
        expected_case_revision: int,
    ) -> CaseMember:
        """Remove a binary from a case (marks status='removed', does not delete row)."""
        now = time.time()
        conn = self._connect()
        try:
            begin_immediate_with_retry(conn)
            current = conn.execute(
                "SELECT revision FROM cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
            if current is None:
                raise ValueError(f"case not found: {case_id}")
            if int(current["revision"]) != expected_case_revision:
                raise ValueError("stale case revision")

            new_rev = expected_case_revision + 1
            conn.execute(
                "UPDATE cases SET revision = ?, updated_at = ? WHERE case_id = ?",
                (new_rev, now, case_id),
            )
            conn.execute(
                "UPDATE case_members SET status = 'removed', updated_at = ? WHERE case_id = ? AND memory_id = ?",
                (now, case_id, memory_id),
            )
            conn.commit()
        finally:
            conn.close()
        return CaseMember(
            case_id=case_id,
            memory_id=memory_id,
            status="removed",
            created_at=now,
            updated_at=now,
        )

    def list_members(self, case_id: str, *, current_only: bool = True) -> list[CaseMember]:
        """List members of a case."""
        conn = self._connect(read_only=True)
        try:
            if current_only:
                rows = conn.execute(
                    "SELECT * FROM case_members WHERE case_id = ? AND status = 'current' ORDER BY created_at",
                    (case_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM case_members WHERE case_id = ? ORDER BY created_at",
                    (case_id,),
                ).fetchall()
            return [_row_to_case_member(r) for r in rows]
        finally:
            conn.close()

    def is_current_member(self, case_id: str, memory_id: str) -> bool:
        """Check if a binary is a current member of a case."""
        conn = self._connect(read_only=True)
        try:
            row = conn.execute(
                "SELECT 1 FROM case_members WHERE case_id = ? AND memory_id = ? AND status = 'current'",
                (case_id, memory_id),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def list_cases_for_memory(self, memory_id: str, include_deleted: bool = False) -> list[CaseRecord]:
        """List all cases a binary is a member of."""
        conn = self._connect(read_only=True)
        try:
            if include_deleted:
                rows = conn.execute(
                    """
                    SELECT c.* FROM cases c
                    JOIN case_members m ON m.case_id = c.case_id
                    WHERE m.memory_id = ? AND m.status = 'current'
                    ORDER BY c.created_at
                    """,
                    (memory_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT c.* FROM cases c
                    JOIN case_members m ON m.case_id = c.case_id
                    WHERE m.memory_id = ? AND m.status = 'current' AND c.state != 'deleted'
                    ORDER BY c.created_at
                    """,
                    (memory_id,),
                ).fetchall()
            return [_row_to_case_record(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Case relations (stored in case workspace DB via WorkspaceStore)
    # ------------------------------------------------------------------

    def put_case_relation(
        self,
        case_id: str,
        subject_memory_id: str,
        predicate: CaseRelationType,
        object_memory_id: str,
        confidence: float = 0.7,
        artifact_ref: str = "",
        sources: tuple[PromotionSource, ...] = (),
    ) -> CaseRelation:
        """Create or update a cross-binary relation within a case.

        Both endpoints must be current members of the case. The relation
        is stored in the case's workspace database as an entity-relation pair.
        """
        validate_case_relation(
            subject_memory_id,
            predicate,
            object_memory_id,
            confidence=confidence,
            artifact_ref=artifact_ref,
        )
        # Canonicalize symmetric endpoints
        subj, obj = canonicalize_relation_endpoints(subject_memory_id, predicate, object_memory_id)

        # Require both endpoints as current members
        if not self.is_current_member(case_id, subj) or not self.is_current_member(case_id, obj):
            raise ValueError("both endpoints must be current case members")

        # Store relation in case workspace DB
        from .workspace_open import open_workspace_for_write
        from .workspace_store import WorkspaceStore

        case_paths = self._locator.case(case_id)
        if case_paths.database.exists():
            store = open_workspace_for_write(case_paths, case_id, self._locator.backups(case_id))
        else:
            store = WorkspaceStore.create(case_paths, owner_memory_id=case_id, workspace_kind="case")

        relation_id = new_record_id("relation")
        # Store as entity pair + relation
        subj_entity = new_record_id("entity")
        obj_entity = new_record_id("entity")
        store.put_entity(subj_entity, "binary_ref", subj, {"memory_id": subj, "artifact_ref": artifact_ref})
        store.put_entity(obj_entity, "binary_ref", obj, {"memory_id": obj})
        store.put_relation(relation_id, subj_entity, predicate.value, obj_entity, confidence)
        store.close()

        return CaseRelation(
            relation_id=relation_id,
            case_id=case_id,
            subject_memory_id=subj,
            predicate=predicate,
            object_memory_id=obj,
            confidence=confidence,
            sources=sources,
            artifact_ref=artifact_ref,
        )

    def list_case_relations(self, case_id: str) -> list[CaseRelation]:
        """List all relations in a case workspace.

        Opens the case workspace in read-only mode so that a stale v1
        database raises ``SchemaMigrationRequired`` instead of
        silently migrating during a list operation.
        """
        from .workspace_store import WorkspaceStore

        case_paths = self._locator.case(case_id)
        if not case_paths.database.exists():
            return []

        store = WorkspaceStore.open(case_paths, owner_memory_id=case_id, read_only=True)
        try:
            raw_relations = store.list_relations()
            entities = {
                e.entity_id: e
                for e in [store.get_entity(r.subject_id) for r in raw_relations]
                + [store.get_entity(r.object_id) for r in raw_relations]
                if e is not None
            }

            relations: list[CaseRelation] = []
            for r in raw_relations:
                subj_entity = entities.get(r.subject_id)
                obj_entity = entities.get(r.object_id)
                if subj_entity is None or obj_entity is None:
                    continue
                try:
                    pred = CaseRelationType(r.predicate)
                except ValueError:
                    continue
                relations.append(
                    CaseRelation(
                        relation_id=r.relation_id,
                        case_id=case_id,
                        subject_memory_id=subj_entity.metadata.get("memory_id", subj_entity.name),
                        predicate=pred,
                        object_memory_id=obj_entity.metadata.get("memory_id", obj_entity.name),
                        confidence=r.confidence,
                        sources=(),
                        artifact_ref=subj_entity.metadata.get("artifact_ref", ""),
                    )
                )
            return relations
        finally:
            store.close()


def _row_to_case_record(row: Any) -> CaseRecord:
    return CaseRecord(
        case_id=row["case_id"],
        name=row["name"],
        state=row["state"],
        revision=row["revision"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_case_member(row: Any) -> CaseMember:
    return CaseMember(
        case_id=row["case_id"],
        memory_id=row["memory_id"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
