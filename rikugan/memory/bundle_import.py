"""Bundle importer: deterministic-ID ZIP import with stage/validate/write.

Imports a validated ZIP bundle into a target workspace using
deterministic destination IDs scoped to ``(target, import_id, type,
origin_id)``. Re-importing the same bundle into the same target is a
no-op: ``imported_count == 0`` and no new revisions or observations
are produced. The wire payload is v1 (unchanged). All untrusted data
passes through :func:`canonicalize_fact_type` /
:func:`canonicalize_fact_content` for payload comparison.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .bundle_schema import BundleLimits, validate_manifest
from .fact_identity import (
    canonicalize_fact_content,
    canonicalize_fact_type,
    deterministic_import_record_id,
)
from .repository import SQLiteKnowledgeRepository
from .schema import KnowledgeEntity, KnowledgeMemory, KnowledgeRelation


class BundleImportConflictError(RuntimeError):
    """A deterministic destination ID already stores different data."""


@dataclass(frozen=True)
class BundleImportResult:
    """Result of a bundle import."""

    import_id: str
    imported_count: int
    target_memory_id: str


RecordType = Literal["fact", "entity", "relation"]


def import_workspace_bundle(
    bundle_path: Path,
    repository: SQLiteKnowledgeRepository,
    *,
    mode: Literal["merge", "restore-as-new"] = "merge",
    limits: BundleLimits | None = None,
) -> BundleImportResult:
    """Import a validated ZIP bundle into the target workspace.

    Re-importing the same bundle into the same target is a no-op. The
    import algorithm is stage / validate / write: it walks the bundle
    twice, validates every record against existing destination rows
    in memory, and only writes the absent subset once validation
    passes — so a late conflict cannot leave a partial replay.

    Parameters
    ----------
    bundle_path:
        Path to the ZIP bundle.
    repository:
        Target workspace repository.
    mode:
        ``merge`` (default) or ``restore-as-new``.
    limits:
        Hard limits for validation.
    """
    lim = limits or BundleLimits()
    target_mid = repository.owner_memory_id

    # Read and validate the bundle
    with zipfile.ZipFile(bundle_path, "r") as zf:
        manifest_data = json.loads(zf.read("manifest.json"))

        # Build manifest object
        from .bundle_schema import ManifestFile, MemoryBundleManifest

        files = tuple(
            ManifestFile(
                name=f["name"],
                sha256=f["sha256"],
                uncompressed_size=f["uncompressed_size"],
                record_count=f.get("record_count", 0),
            )
            for f in manifest_data.get("files", [])
        )
        manifest = MemoryBundleManifest(
            schema_version=manifest_data["schema_version"],
            scope=manifest_data.get("scope", "binary"),
            export_mode=manifest_data.get("export_mode", "portable"),
            origin_memory_id=manifest_data.get("origin_memory_id", ""),
            exported_at=manifest_data.get("exported_at", ""),
            files=files,
            record_counts=manifest_data.get("record_counts", {}),
        )
        validate_manifest(manifest, limits=lim)

        # Compute deterministic import ID
        manifest_hash = hashlib.sha256(json.dumps(manifest_data, sort_keys=True).encode("utf-8")).hexdigest()
        import_id = f"import-{manifest_hash[:16]}"

        # First parse pass: collect every envelope, validate origin_id, build
        # the deterministic id_map for all fact/entity/relation records. We
        # need the full map before importing so relation endpoints can
        # resolve regardless of file ordering, and so a key like
        # ``("fact", "x")`` cannot collide with ``("entity", "x")``.
        envelopes: list[tuple[RecordType, str, dict[str, object]]] = []
        id_map: dict[tuple[str, str], str] = {}
        seen_keys: set[tuple[str, str]] = set()

        for file_info in manifest.files:
            if not file_info.name.startswith("records/"):
                continue
            content = zf.read(file_info.name).decode("utf-8")
            for line in content.strip().split("\n"):
                if not line:
                    continue
                try:
                    envelope = json.loads(line)
                except json.JSONDecodeError:
                    continue

                record_type = envelope.get("record_type", "")
                origin_id = envelope.get("record_id", "")
                payload = envelope.get("payload", {})

                if record_type not in ("fact", "entity", "relation"):
                    continue
                if not isinstance(origin_id, str) or not origin_id:
                    raise ValueError(f"empty or non-string origin_id in {record_type} envelope")
                if not isinstance(payload, dict):
                    raise ValueError(f"non-dict payload in {record_type} envelope")

                key = (record_type, origin_id)
                if key in seen_keys:
                    raise ValueError(f"duplicate bundle envelope for {record_type}:{origin_id}")
                seen_keys.add(key)
                envelopes.append((record_type, origin_id, payload))

                new_id = id_map.setdefault(
                    key,
                    deterministic_import_record_id(target_mid, import_id, record_type, origin_id),
                )
                # ``setdefault`` never overwrites an existing entry; the new_id
                # above is therefore stable for every (type, origin_id) pair.

        # Second pass: build destination lookup dictionaries ONCE, then for
        # each record either skip (idempotent), raise a conflict, or stage
        # for insert. No writes happen until every record is classified.
        existing_facts: dict[str, KnowledgeMemory] = {m.id: m for m in repository.list_memories()}
        existing_entities: dict[str, KnowledgeEntity] = {e.id: e for e in repository.list_entities()}
        existing_relations: dict[str, KnowledgeRelation] = {r.id: r for r in repository.list_relations()}

        staged_facts: list[KnowledgeMemory] = []
        staged_entities: list[KnowledgeEntity] = []
        staged_relations: list[KnowledgeRelation] = []

        for record_type, origin_id, payload in envelopes:
            new_id = id_map[(record_type, origin_id)]
            if record_type == "fact":
                existing = existing_facts.get(new_id)
                if existing is not None:
                    if not _fact_payload_matches(existing, payload):
                        raise BundleImportConflictError(f"import destination conflict: {new_id}")
                    continue
                staged_facts.append(
                    KnowledgeMemory(
                        id=new_id,
                        binary_id=target_mid,
                        type=str(payload.get("type", "general")),
                        title=str(payload.get("title", "")),
                        content=str(payload.get("content", "")),
                        confidence=float(payload.get("confidence", 0.5)),
                    )
                )
            elif record_type == "entity":
                existing = existing_entities.get(new_id)
                if existing is not None:
                    if not _entity_payload_matches(existing, payload):
                        raise BundleImportConflictError(f"import destination conflict: {new_id}")
                    continue
                staged_entities.append(
                    KnowledgeEntity(
                        id=new_id,
                        binary_id=target_mid,
                        type=str(payload.get("type", "unknown")),
                        name=str(payload.get("name", "")),
                        display_name=str(payload.get("display_name", "")),
                        address=str(payload.get("address", "")),
                    )
                )
            elif record_type == "relation":
                existing = existing_relations.get(new_id)
                if existing is not None:
                    if not _relation_payload_matches(existing, payload, id_map):
                        raise BundleImportConflictError(f"import destination conflict: {new_id}")
                    continue
                src_id = id_map[("entity", str(payload.get("src", "")))]
                dst_id = id_map[("entity", str(payload.get("dst", "")))]
                staged_relations.append(
                    KnowledgeRelation(
                        id=new_id,
                        binary_id=target_mid,
                        src=src_id,
                        predicate=str(payload.get("predicate", "related_to")),
                        dst=dst_id,
                        confidence=float(payload.get("confidence", 0.5)),
                    )
                )

        # All validation passed. Apply inserts for the absent subset only.
        for fact in staged_facts:
            repository.upsert_memory(fact)
        for entity in staged_entities:
            repository.upsert_entity(entity)
        for relation in staged_relations:
            repository.upsert_relation(relation)

        imported_count = len(staged_facts) + len(staged_entities) + len(staged_relations)

    return BundleImportResult(
        import_id=import_id,
        imported_count=imported_count,
        target_memory_id=target_mid,
    )


# ---------------------------------------------------------------------------
# Payload comparison helpers
# ---------------------------------------------------------------------------


def _fact_payload_matches(existing: KnowledgeMemory, payload: dict[str, object]) -> bool:
    """Return True iff *existing* carries the same fact payload as *payload*.

    Compares only the fields the v1 wire format serializes. Title and
    confidence are part of bundle replay identity; canonical
    comparison of type/content defends against hash collisions and
    whitespace noise.
    """
    return (
        canonicalize_fact_type(existing.type) == canonicalize_fact_type(str(payload.get("type", "general")))
        and canonicalize_fact_content(existing.content) == canonicalize_fact_content(str(payload.get("content", "")))
        and existing.title == str(payload.get("title", ""))
        and existing.confidence == float(payload.get("confidence", 0.5))
    )


def _entity_payload_matches(existing: KnowledgeEntity, payload: dict[str, object]) -> bool:
    """Return True iff *existing* carries the same entity payload as *payload*.

    Compares only the fields the v1 bundle wire format actually
    serializes (no ``tags``). Exporting ``tags`` is deferred to a
    follow-up interchange-compat tranche.
    """
    return (
        existing.type == str(payload.get("type", "unknown"))
        and existing.name == str(payload.get("name", ""))
        and existing.display_name == str(payload.get("display_name", ""))
        and existing.address == str(payload.get("address", ""))
    )


def _relation_payload_matches(
    existing: KnowledgeRelation,
    payload: dict[str, object],
    id_map: dict[tuple[str, str], str],
) -> bool:
    """Return True iff *existing* carries the same relation payload as *payload*.

    Compares only the graph fields the v1 wire format carries (no
    ``evidence``): endpoints (already remapped through *id_map*),
    predicate, and confidence.
    """
    return (
        existing.src == id_map[("entity", str(payload.get("src", "")))]
        and existing.predicate == str(payload.get("predicate", "related_to"))
        and existing.dst == id_map[("entity", str(payload.get("dst", "")))]
        and existing.confidence == float(payload.get("confidence", 0.5))
    )
