"""SQLite knowledge repository adapter.

Bridges the existing :class:`KnowledgeMemory` / :class:`KnowledgeEntity` /
:class:`KnowledgeRelation` / :class:`KnowledgeObservation` dataclasses
from :mod:`rikugan.memory.schema` to the :class:`WorkspaceStore` SQLite
backend.

This adapter is the read/write interface consumed by retrieval, context,
and service layers. It preserves the current dataclass shapes so existing
retrieval and sanitize code works unchanged during the cutover.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, Protocol

from .fact_identity import canonicalize_fact_content, canonicalize_fact_type, semantic_fact_hash
from .schema import (
    KnowledgeEntity,
    KnowledgeMemory,
    KnowledgeObservation,
    KnowledgeRelation,
)
from .workspace_store import WorkspaceStore


@dataclass(frozen=True)
class SavedKnowledgeMemory:
    """Result of :meth:`SQLiteKnowledgeRepository.save_memory_fact`.

    ``record`` carries the persisted (or existing) :class:`KnowledgeMemory`,
    and ``outcome`` distinguishes first-write from exact-dedup.
    """

    record: KnowledgeMemory
    outcome: Literal["created", "deduplicated"]


_FACT_WRITE_CONFIDENCE = 0.7


class KnowledgeRepository(Protocol):
    """Read/write interface for workspace knowledge records."""

    owner_memory_id: str

    def list_memories(self) -> list[KnowledgeMemory]: ...

    def list_entities(self) -> list[KnowledgeEntity]: ...

    def list_relations(self) -> list[KnowledgeRelation]: ...

    def count_observations(self) -> int: ...

    def upsert_memory(self, value: KnowledgeMemory) -> None: ...

    def upsert_entity(self, value: KnowledgeEntity) -> None: ...

    def upsert_relation(self, value: KnowledgeRelation) -> None: ...

    def append_observation(self, value: KnowledgeObservation) -> None: ...


def _validate_owner(record_owner: str, expected: str) -> None:
    """Raise ValueError if *record_owner* does not match *expected*."""
    if record_owner != expected:
        raise ValueError(f"owner_memory_id mismatch: record has {record_owner!r}, workspace has {expected!r}")


class SQLiteKnowledgeRepository:
    """Adapter that maps knowledge dataclasses onto WorkspaceStore tables."""

    def __init__(self, store: WorkspaceStore, *, owner_memory_id: str) -> None:
        self._store = store
        self.owner_memory_id = owner_memory_id

    # ------------------------------------------------------------------
    # Memories → facts
    # ------------------------------------------------------------------

    def upsert_memory(self, value: KnowledgeMemory) -> None:
        """Insert or update a memory as a fact record."""
        _validate_owner(value.binary_id, self.owner_memory_id)
        # Determine the current revision (0 for new, current for update)
        existing = self._store.get_fact(value.id)
        expected_revision = existing.revision if existing else 0
        self._store.put_fact(
            value.id,
            value.type,
            value.title,
            value.content,
            value.confidence,
            expected_revision=expected_revision,
        )

    def list_memories(self) -> list[KnowledgeMemory]:
        """List all current memories."""
        facts = self._store.list_facts()
        return [
            KnowledgeMemory(
                id=f.fact_id,
                binary_id=self.owner_memory_id,
                type=f.fact_type,
                title=f.title,
                content=f.content,
                confidence=f.confidence,
            )
            for f in facts
        ]

    # ------------------------------------------------------------------
    # Entities
    # ------------------------------------------------------------------

    def upsert_entity(self, value: KnowledgeEntity) -> None:
        """Insert or update an entity."""
        _validate_owner(value.binary_id, self.owner_memory_id)
        existing = self._store.get_entity(value.id)
        metadata = {
            "display_name": value.display_name,
            "address": value.address,
        }
        if existing:
            # Merge with existing metadata
            metadata = {**existing.metadata, **metadata}
        self._store.put_entity(
            value.id,
            value.type,
            value.name,
            metadata,
            tags=list(value.tags),
        )

    def list_entities(self) -> list[KnowledgeEntity]:
        """List all current entities."""
        # WorkspaceStore doesn't have list_entities yet — use raw query
        rows = self._store._conn.execute("SELECT * FROM entities ORDER BY created_at").fetchall()
        return [
            KnowledgeEntity(
                id=row["entity_id"],
                binary_id=self.owner_memory_id,
                type=row["entity_type"],
                name=row["name"],
                display_name=json.loads(row["metadata"]).get("display_name", ""),
                address=json.loads(row["metadata"]).get("address", ""),
                tags=json.loads(row["tags"]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Relations
    # ------------------------------------------------------------------

    def upsert_relation(self, value: KnowledgeRelation) -> None:
        """Insert or update a relation."""
        _validate_owner(value.binary_id, self.owner_memory_id)
        self._store.put_relation(
            value.id,
            value.src,
            value.predicate,
            value.dst,
            value.confidence,
        )

    def list_relations(self) -> list[KnowledgeRelation]:
        """List all current relations."""
        rels = self._store.list_relations()
        return [
            KnowledgeRelation(
                id=r.relation_id,
                binary_id=self.owner_memory_id,
                src=r.subject_id,
                predicate=r.predicate,
                dst=r.object_id,
                confidence=r.confidence,
            )
            for r in rels
        ]

    # ------------------------------------------------------------------
    # Observations
    # ------------------------------------------------------------------

    def append_observation(self, value: KnowledgeObservation) -> None:
        """Append an immutable observation."""
        _validate_owner(value.binary_id, self.owner_memory_id)
        self._store.append_observation(
            value.id,
            value.kind,
            json.dumps(value.payload, ensure_ascii=False, sort_keys=True),
        )

    def count_observations(self) -> int:
        """Count all observations."""
        return self._store.count_observations()

    # ------------------------------------------------------------------
    # Convenience: allocate-and-append
    # ------------------------------------------------------------------

    def save_memory_fact(
        self,
        category: str,
        fact: str,
        source: str,
    ) -> SavedKnowledgeMemory:
        """Save a fact using exact-semantic dedup (no category overwrite).

        Canonicalizes *category* and *fact* via the fact identity helpers,
        computes a stable SHA-256 digest, and delegates to
        :meth:`WorkspaceStore.save_fact_if_semantically_absent` so lookup,
        optional insert, and the observation append run inside one
        ``BEGIN IMMEDIATE`` transaction.

        Returns a :class:`SavedKnowledgeMemory` whose ``outcome`` is
        ``"created"`` for a new fact and ``"deduplicated"`` when an exact
        semantic match already existed. In both branches the caller-
        provided category is recorded as taxonomy only and never selects
        an implicit update target.
        """
        from .workspace import new_record_id

        canonical_type = canonicalize_fact_type(category)
        canonical_content = canonicalize_fact_content(fact)
        digest = semantic_fact_hash(canonical_type, canonical_content)
        record, outcome = self._store.save_fact_if_semantically_absent(
            fact_id=new_record_id("fact"),
            fact_type=canonical_type,
            title=canonical_type,
            content=canonical_content,
            semantic_hash=digest,
            confidence=_FACT_WRITE_CONFIDENCE,
            observation_id=new_record_id("observation"),
            observation_type=source,
            observation_payload=json.dumps(
                {"category": canonical_type, "semantic_hash": digest},
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        return SavedKnowledgeMemory(
            record=KnowledgeMemory(
                id=record.fact_id,
                binary_id=self.owner_memory_id,
                type=record.fact_type,
                title=record.title,
                content=record.content,
                confidence=record.confidence,
            ),
            outcome=outcome,
        )

    def save_exploration_finding(
        self,
        category: str,
        fact: str,
        source: str,
        *,
        entity_refs: list[str] | None = None,
        tags: list[str] | None = None,
        title: str | None = None,
        confidence: float = _FACT_WRITE_CONFIDENCE,
    ) -> SavedKnowledgeMemory:
        """Save an exploration finding with graph metadata.

        Mirrors :meth:`save_memory_fact` but additionally records
        *entity_refs* and *tags* on the underlying ``facts`` row via
        :meth:`WorkspaceStore.save_fact_if_semantically_absent`. The
        optional *title* defaults to the canonicalized *category* when
        not supplied.

        Returns a :class:`SavedKnowledgeMemory`; ``outcome`` is
        ``"created"`` for a new fact and ``"deduplicated"`` for an
        exact semantic match. Semantic identity is keyed only on
        ``(fact_type, content)`` — entity_refs and tags are recorded
        as first-write graph metadata and are not part of dedup.
        """
        from .workspace import new_record_id

        canonical_type = canonicalize_fact_type(category)
        canonical_content = canonicalize_fact_content(fact)
        digest = semantic_fact_hash(canonical_type, canonical_content)
        resolved_title = title or canonical_type
        record, outcome = self._store.save_fact_if_semantically_absent(
            fact_id=new_record_id("fact"),
            fact_type=canonical_type,
            title=resolved_title,
            content=canonical_content,
            semantic_hash=digest,
            confidence=confidence,
            observation_id=new_record_id("observation"),
            observation_type=source,
            observation_payload=json.dumps(
                {"category": canonical_type, "semantic_hash": digest, "source": source},
                ensure_ascii=False,
                sort_keys=True,
            ),
            entity_refs=entity_refs,
            tags=tags,
        )
        return SavedKnowledgeMemory(
            record=KnowledgeMemory(
                id=record.fact_id,
                binary_id=self.owner_memory_id,
                type=record.fact_type,
                title=record.title,
                content=record.content,
                confidence=record.confidence,
            ),
            outcome=outcome,
        )
