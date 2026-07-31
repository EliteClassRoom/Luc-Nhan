"""Auto-ingestion of structured events into the raw knowledge store.

The agent already emits a few well-shaped events during its normal
operation:

* ``save_memory`` — a fact with a category.
* ``exploration_report`` — a finding with category/address/relevance.
* ``research_note`` — a finished Obsidian-style Markdown note.

Auto-ingest turns each of these into idempotent writes to the JSONL
store. The functions here are **best-effort**: they never raise, never
block the agent loop, and silently skip writes when no IDB path is
available. Failure to write a memory does not undo what already landed
on disk.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime
from typing import Any

from .notes import extract_inline_addresses, extract_inline_tags, parse_note
from .paths import (
    KnowledgePaths,
    extract_addresses,
    function_entity_id,
    import_entity_id,
    normalize_address,
    note_entity_id,
    relation_id,
    report_entity_id,
    string_entity_id,
)
from .raw_store import KnowledgeRawStore
from .schema import (
    KnowledgeEntity,
    KnowledgeMemory,
    KnowledgeObservation,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """ISO 8601 UTC timestamp with ``+00:00`` suffix."""
    return datetime.now(UTC).isoformat()


def _stable_hash(*parts: Any, length: int = 8) -> str:
    """Short stable hash over arbitrary parts for deterministic IDs."""
    h = hashlib.sha256()
    for p in parts:
        if p is None:
            h.update(b"\x00")
        else:
            h.update(str(p).encode("utf-8", errors="replace"))
            h.update(b"\x01")
    return h.hexdigest()[:length]


def make_store(idb_path: str, db_instance_id: str = "") -> tuple[KnowledgeRawStore, KnowledgePaths] | tuple[None, None]:
    """Best-effort store construction; returns (None, None) when no IDB."""
    if not idb_path:
        return (None, None)
    try:
        from .paths import knowledge_paths

        paths = knowledge_paths(idb_path, db_instance_id)
        return (KnowledgeRawStore(paths), paths)
    except Exception:
        return (None, None)


def _importance_from_relevance(relevance: str) -> float:
    rel = (relevance or "medium").lower()
    if rel == "high":
        return 0.85
    if rel == "low":
        return 0.25
    return 0.5


# ---------------------------------------------------------------------------
# save_memory ingestion
# ---------------------------------------------------------------------------


def ingest_save_memory(
    store: KnowledgeRawStore,
    paths: KnowledgePaths,
    fact: str,
    category: str,
) -> None:
    """Ingest one ``save_memory`` event into memories + observations."""
    if not store or not fact:
        return
    fact = (fact or "").strip()
    if not fact:
        return
    addresses = extract_addresses(fact)
    addr_part = ":".join(addresses) if addresses else "nofact"
    mem_id = f"mem:{category}:{addr_part}:{_stable_hash(category, fact)}"

    memory = KnowledgeMemory(
        id=mem_id,
        binary_id=paths.binary_id,
        type=str(category or "general"),
        title=_memory_title(fact),
        content=fact,
        entity_refs=[function_entity_id(a) for a in addresses],
        source_refs=[f"save_memory:{mem_id}"],
        tags=_memory_tags_for(category),
        confidence=0.7,
        importance=0.5,
        verified=False,
        created_at=_now_iso(),
        updated_at=_now_iso(),
    )
    try:
        store.upsert_memory(memory)
        store.append_observation(
            KnowledgeObservation(
                id=f"obs:{uuid.uuid4().hex[:12]}",
                binary_id=paths.binary_id,
                ts=_now_iso(),
                kind="save_memory",
                payload={
                    "memory_id": mem_id,
                    "category": category,
                    "addresses": addresses,
                },
            )
        )
    except Exception:
        # Memory subsystem is best-effort — never fail the agent on it.
        pass


def _memory_title(fact: str, limit: int = 80) -> str:
    first = fact.splitlines()[0].strip()
    if len(first) > limit:
        first = first[: limit - 1].rstrip() + "…"
    return first or "memory"


def _memory_tags_for(category: str) -> list[str]:
    cat = (category or "general").strip().lower()
    return [cat] if cat else ["general"]


# ---------------------------------------------------------------------------
# exploration_report ingestion
# ---------------------------------------------------------------------------


_CATEGORY_TO_ENTITY_TYPE = {
    "function_purpose": "function",
    "data_structure": "struct",
    "constant": "global",
    "hypothesis": "concept",
    "string_ref": "string",
    "import_usage": "import",
    "patch_result": "function",
    "general": "concept",
}


def ingest_exploration_finding(
    store: KnowledgeRawStore,
    paths: KnowledgePaths,
    category: str,
    summary: str,
    address: int | None,
    relevance: str,
    evidence: str = "",
    function_name: str = "",
    memory_id: str | None = None,
    title: str | None = None,
    confidence: float | None = None,
) -> None:
    """Upsert a memory + entity/relation record for a single finding.

    ``memory_id`` lets the caller supply a precomputed id; when
    omitted the function falls back to the deterministic id derived
    from the (corrected) ``summary`` and ``address``.

    ``title`` and ``confidence`` override the derived values so the
    caller can persist reviewer-corrected fields (e.g. a renamed
    function title or a recalibrated confidence after a correction
    cycle). When omitted, ``title`` is the first summary line and
    ``confidence`` follows the legacy ``relevance`` map.
    """
    if not store:
        return
    summary = (summary or "").strip()
    if not summary:
        return
    cat = (category or "general").strip().lower()
    addr_norm = normalize_address(address)
    if addr_norm:
        addr_part = addr_norm
    else:
        addr_part = "noaddr"
    if memory_id:
        mem_id = memory_id
    else:
        mem_id = f"mem:explore:{cat}:{addr_part}:{_stable_hash(cat, summary, address)}"
    importance = _importance_from_relevance(relevance)
    resolved_title = title if title is not None and title.strip() else _memory_title(summary)
    resolved_confidence = (
        confidence
        if confidence is not None and 0.0 <= float(confidence) <= 1.0
        else (0.8 if relevance == "high" else 0.6)
    )

    entity_refs: list[str] = []
    if addr_norm:
        entity_type = _CATEGORY_TO_ENTITY_TYPE.get(cat, "concept")
        eid = _entity_id_for(category=cat, entity_type=entity_type, address=address, name=function_name or summary[:40])
    else:
        # No address → synthesize a stable concept entity so the memory
        # still has something to point at.
        entity_type = "concept"
        eid = _entity_id_for(category=cat, entity_type=entity_type, address=None, name=function_name or summary[:60])
    ent = KnowledgeEntity(
        id=eid,
        binary_id=paths.binary_id,
        type=entity_type,
        name=function_name or eid.split(":", 1)[-1],
        address=addr_norm,
        tags=[cat],
        source_refs=[f"finding:{mem_id}"],
    )
    try:
        store.upsert_entity(ent)
    except Exception:
        pass
    entity_refs.append(eid)

    memory = KnowledgeMemory(
        id=mem_id,
        binary_id=paths.binary_id,
        type=cat,
        title=resolved_title,
        content=summary,
        entity_refs=entity_refs,
        source_refs=[f"exploration_report:{mem_id}"],
        tags=[cat, "exploration"],
        confidence=resolved_confidence,
        importance=importance,
        verified=relevance == "high",
        created_at=_now_iso(),
        updated_at=_now_iso(),
    )
    try:
        store.upsert_memory(memory)
        store.append_observation(
            KnowledgeObservation(
                id=f"obs:{uuid.uuid4().hex[:12]}",
                binary_id=paths.binary_id,
                ts=_now_iso(),
                kind="exploration_finding",
                payload={
                    "memory_id": mem_id,
                    "category": cat,
                    "address": addr_norm,
                    "summary": summary[:200],
                },
            )
        )
    except Exception:
        pass


def _normalize_addr_for_id(address: int | None) -> str:
    """Deprecated: thin wrapper kept for back-compat.

    New code should call :func:`rikugan.memory.paths.normalize_address`
    directly so ID formatting is uniform across the module.
    """
    return normalize_address(address)


def _entity_id_for(category: str, entity_type: str, address: int | None, name: str) -> str:
    if address is not None:
        if entity_type == "string":
            return string_entity_id(address)
        if entity_type == "import":
            # Use the canonical import_entity_id helper so odd
            # characters in the import name are sanitized consistently
            # with the rest of the codebase (the old hard-coded
            # "import:unknown:{name}" skipped that step).
            return import_entity_id("unknown", name or "unknown")
        return function_entity_id(address)
    # No address — synthesize a stable slug-derived ID.
    safe = _stable_hash(category, name) if name else _stable_hash(category)
    return f"concept:{safe}"


# ---------------------------------------------------------------------------
# research_note ingestion
# ---------------------------------------------------------------------------


def ingest_research_note(
    store: KnowledgeRawStore,
    paths: KnowledgePaths,
    *,
    note_path: str,
    genre: str,
    title: str,
    content: str,
    related: list[str] | None = None,
    review_passed: bool = False,
) -> None:
    """Ingest a finished research note into memories, entities, and relations."""
    if not store:
        return
    try:
        parsed = parse_note(content or "", path=note_path)
    except Exception:
        parsed = None
    slug = os.path.splitext(os.path.basename(note_path or ""))[0] or (title or "untitled")
    note_eid = note_entity_id(slug)

    # Note entity
    try:
        store.upsert_entity(
            KnowledgeEntity(
                id=note_eid,
                binary_id=paths.binary_id,
                type="note",
                name=slug,
                display_name=(parsed.title if parsed else title) or slug,
                tags=[g for g in ([parsed.genre if parsed else genre] + (parsed.tags if parsed else [])) if g],
                source_refs=[f"note:{note_path}"],
            )
        )
    except Exception:
        pass

    # Mentioned function entities + relations
    addresses = (parsed.addresses if parsed else []) or extract_inline_addresses(content or "")
    func_eids: list[str] = []
    for addr in addresses:
        eid = function_entity_id(addr)
        func_eids.append(eid)
        try:
            store.upsert_entity(
                KnowledgeEntity(
                    id=eid,
                    binary_id=paths.binary_id,
                    type="function",
                    name=eid.split(":", 1)[-1],
                    address=addr,
                    source_refs=[f"note:{note_path}"],
                )
            )
            store.upsert_relation_from(
                note_eid,
                "mentions",
                eid,
                confidence=0.7 if review_passed else 0.5,
                source_refs=[f"note:{note_path}"],
            )
        except Exception:
            pass

    # Memory summarising the note
    mem_id = f"mem:note:{genre or (parsed.genre if parsed else 'general')}:{slug}"
    memory = KnowledgeMemory(
        id=mem_id,
        binary_id=paths.binary_id,
        type="note",
        title=(parsed.title if parsed else title) or slug,
        content=_memory_excerpt(content or ""),
        entity_refs=[note_eid, *func_eids],
        source_refs=[f"note:{note_path}"],
        tags=_dedup_nonempty(
            [
                genre or "",
                parsed.genre if parsed else "",
                *(parsed.tags if parsed else []),
                *extract_inline_tags(content or ""),
            ],
        ),
        confidence=0.8 if review_passed else 0.5,
        importance=0.7,
        verified=review_passed,
        created_at=_now_iso(),
        updated_at=_now_iso(),
    )
    try:
        store.upsert_memory(memory)
    except Exception:
        pass

    # Related notes → mentioned_in_note relations. Also create
    # placeholder entities for related slugs that haven't been indexed
    # yet so the relation isn't dangling in the knowledge graph.
    related = related or (parsed.related_notes if parsed else [])
    for slug_or_title in related:
        rel_eid = note_entity_id(slug_or_title)
        try:
            if store.get_entity(rel_eid) is None:
                store.upsert_entity(
                    KnowledgeEntity(
                        id=rel_eid,
                        binary_id=paths.binary_id,
                        type="note",
                        name=slug_or_title,
                        source_refs=[f"note:{note_path}"],
                    )
                )
            store.upsert_relation_from(
                note_eid,
                "related_to",
                rel_eid,
                confidence=0.6,
                source_refs=[f"note:{note_path}"],
            )
        except Exception:
            pass

    # Observation (timeline entry)
    try:
        store.append_observation(
            KnowledgeObservation(
                id=f"obs:{uuid.uuid4().hex[:12]}",
                binary_id=paths.binary_id,
                ts=_now_iso(),
                kind="research_note_saved",
                payload={
                    "memory_id": mem_id,
                    "path": note_path,
                    "genre": genre,
                    "title": title,
                    "review_passed": review_passed,
                    "addresses": addresses,
                    "related": related or [],
                },
            )
        )
    except Exception:
        pass


def _memory_excerpt(content: str, limit: int = 400) -> str:
    body = (content or "").strip()
    if len(body) <= limit:
        return body
    return body[: limit - 1].rstrip() + "…"


def _dedup_nonempty(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        if not it:
            continue
        s = str(it).strip().lower()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# /report ingestion
# ---------------------------------------------------------------------------


def ingest_report(
    store: KnowledgeRawStore,
    paths: KnowledgePaths,
    *,
    report_path: str,
    slug: str,
    scope: str,
    body_excerpt: str,
    raise_on_error: bool = False,
) -> None:
    """Store a memory + observation for a generated report.

    When ``raise_on_error`` is True, storage failures propagate so
    callers like :func:`save_report` can report them to the user.
    Default is False to preserve the best-effort contract for older
    best-effort callers.
    """
    if not store:
        return
    eid = relation_id("report", scope, slug)
    # Use the canonical report_entity_id helper so hostile slugs (e.g.
    # one containing path separators or newlines) are sanitized the
    # same way as notes/structs/algos. The raw f-string "report:{slug}"
    # previously skipped that sanitization.
    report_eid = report_entity_id(slug)
    try:
        store.upsert_entity(
            KnowledgeEntity(
                id=report_eid,
                binary_id=paths.binary_id,
                type="report",
                name=slug,
                display_name=slug,
                source_refs=[f"report:{report_path}"],
            )
        )
        store.upsert_memory(
            KnowledgeMemory(
                id=f"mem:report:{scope}:{slug}",
                binary_id=paths.binary_id,
                type="report",
                title=f"Report: {slug}",
                content=_memory_excerpt(body_excerpt, limit=1200),
                entity_refs=[report_eid],
                source_refs=[f"report:{report_path}"],
                tags=["report", scope],
                confidence=0.9,
                importance=0.95,
                verified=True,
                created_at=_now_iso(),
                updated_at=_now_iso(),
            )
        )
        store.append_observation(
            KnowledgeObservation(
                id=f"obs:{uuid.uuid4().hex[:12]}",
                binary_id=paths.binary_id,
                ts=_now_iso(),
                kind="report_generated",
                payload={
                    "report_id": report_eid,
                    "path": report_path,
                    "scope": scope,
                    "relation_anchor": eid,
                },
            )
        )
    except Exception as exc:
        if raise_on_error:
            raise
        # Best-effort: a write error should not undo a successful file.
        # Callers that need strictness pass ``raise_on_error=True``.
        return


__all__ = [
    "ingest_exploration_finding",
    "ingest_report",
    "ingest_research_note",
    "ingest_save_memory",
    "make_store",
]
