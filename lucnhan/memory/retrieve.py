"""Keyword + graph retrieval over the raw knowledge store.

MVP retrieval strategy:

1. **Exact matches** for hex addresses (``0x401000``) immediately
   promote the corresponding entity, its memory, and any related
   relations + adjacent entities to the top.
2. **Keyword scoring** sums lowercase term hits across IDs, titles,
   content, tags, aliases, predicate, source refs, etc., preferring
   verified/high-confidence records.
3. **One-hop relation expansion** automatically pulls the immediate
   neighborhood of any matched entity so the LLM sees, e.g., "this
   function uses import X" alongside the function fact itself.

Retrieval is read-only and never raises. Callers ask for at most
N items per type (memories, entities, relations, notes) so the
combined pack stays inside the configured prompt budget.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import NamedTuple

from .notes import list_notes
from .paths import (
    KnowledgePaths,
    extract_addresses,
    function_entity_id,
)
from .raw_store import KnowledgeRawStore
from .schema import (
    KnowledgeEntity,
    KnowledgeMemory,
    KnowledgeRelation,
)

# Minimum score required for a memory/entity/relation to make the cut.
# Lowered for entities (low-content records) and raised for memories
# (content-heavy — false positives hurt worse).
_MIN_MEMORY_SCORE = 2.0
_MIN_ENTITY_SCORE = 1.0
_MIN_RELATION_SCORE = 1.0


class NoteExcerpt(NamedTuple):
    """Minimal (title, body) carrier for the ranker's note stage.

    Avoids leaking the heavier :class:`ParsedNote` (frontmatter, sections,
    wiki links) through the ranker boundary. ``body`` is the full markdown
    body; truncation to the prompt budget happens at storage time.
    """

    title: str
    body: str


@dataclass
class RetrievalQuery:
    """Holds all the inputs the agent hands to the retriever."""

    text: str = ""
    address: str = ""
    function_name: str = ""
    active_goal: str = ""
    active_mode: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class RetrievalPack:
    """The selected slice of memory returned to the agent."""

    memories: list[KnowledgeMemory] = field(default_factory=list)
    entities: list[KnowledgeEntity] = field(default_factory=list)
    relations: list[KnowledgeRelation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # markdown excerpts
    counts: dict[str, int] = field(default_factory=dict)
    query_terms: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.memories) + len(self.entities) + len(self.relations) + len(self.notes)


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------


_WORD_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{2,}|\b0x[0-9a-fA-F]{4,16}\b")


def _terms_from(text: str) -> list[str]:
    """Lowercase word + address tokens from arbitrary text."""
    if not text:
        return []
    return [m.group(0).lower() for m in _WORD_RE.finditer(text)]


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------


def _score_memory(mem: KnowledgeMemory, terms: list[str], term_set: set[str]) -> float:
    """Score a memory against query terms.

    Hits in title/tags/verified earn more because they're curated.
    Hypothesis status changes the trust multiplier applied at the
    end: verified claims earn a 1.25x boost, wrong claims are
    explicitly downranked (0.1x) so a future agent doesn't surface a
    rejected hypothesis as a relevant match, and unverified claims
    get no boost.
    """
    if not terms:
        return 0.0
    score = 0.0
    title_l = mem.title.lower()
    content_l = mem.content.lower()
    id_l = mem.id.lower()
    tags_l = [t.lower() for t in mem.tags]
    for term in terms:
        if term in title_l:
            score += 3.0
        if term in id_l:
            score += 1.0
        if term in content_l:
            score += 1.0
        for tag in tags_l:
            if term == tag:
                score += 2.5
            elif term in tag:
                score += 1.0
        if term in mem.entity_refs:
            score += 2.0
    if mem.type == "hypothesis":
        if mem.status == "wrong":
            score *= 0.1
        elif mem.verified:
            score *= 1.25
    elif mem.verified:
        score *= 1.25
    score += mem.confidence * 0.5
    score += mem.importance * 0.3
    return score


def _score_entity(ent: KnowledgeEntity, terms: list[str], addrs: list[str]) -> float:
    score = 0.0
    name_l = ent.name.lower()
    id_l = ent.id.lower()
    for term in terms:
        if term == id_l:
            score += 2.0
        if term in name_l:
            score += 2.0
        for tag in ent.tags:
            if term == tag.lower():
                score += 1.5
    if addrs and ent.address and ent.address in addrs:
        score += 6.0
    return score


def _score_relation(rel: KnowledgeRelation, terms: list[str]) -> float:
    score = 0.0
    pred_l = rel.predicate.lower()
    src_l = rel.src.lower()
    dst_l = rel.dst.lower()
    for term in terms:
        if term in pred_l:
            score += 1.5
        if term in src_l:
            score += 0.5
        if term in dst_l:
            score += 0.5
        if term in rel.evidence.lower():
            score += 1.0
    score += rel.confidence * 0.5
    return score


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def retrieve_from_records(
    memories: list[KnowledgeMemory],
    entities: list[KnowledgeEntity],
    relations: list[KnowledgeRelation],
    notes: list[NoteExcerpt],
    query: RetrievalQuery,
    *,
    max_memories: int = 12,
    max_entities: int = 8,
    max_relations: int = 15,
    max_notes: int = 3,
    expand_relations: bool = True,
) -> RetrievalPack:
    """Rank a fixed set of records against the *query*.

    Pure ranking body extracted from :func:`retrieve` so both the JSONL
    store and the SQLite repository can feed it the same dataclass
    lists. Notes are passed in as ``(title, body)`` excerpts; filesystem
    parsing is the caller's responsibility.
    """
    pack = RetrievalPack()

    terms = _terms_from(query.text) + _terms_from(query.function_name) + _terms_from(query.active_goal)
    terms += [t.lower() for t in query.tags]
    terms = _dedup(terms)
    pack.query_terms = terms

    addresses = list(extract_addresses(query.text or "") or [])
    if query.address:
        addresses.append(query.address.lower())
    addresses = _dedup(addresses)

    if not (terms or addresses):
        # Empty query — return the newest few records of each type so
        # the LLM still gets useful context (e.g., "what do we know?").
        pack.memories = memories[:max_memories]
        pack.entities = entities[:max_entities]
        pack.relations = relations[:max_relations]
        pack.counts = {
            "memories": len(pack.memories),
            "entities": len(pack.entities),
            "relations": len(pack.relations),
            "notes": 0,
        }
        return pack

    term_set = set(terms)

    # --- Memories ---
    addr_func_ids = {function_entity_id(a) for a in addresses}
    scored: list[tuple[float, KnowledgeMemory]] = []
    for m in memories:
        s = _score_memory(m, terms, term_set)
        # Bonus when the memory references a known address-entity.
        if addr_func_ids and any(eid in addr_func_ids for eid in m.entity_refs):
            s += 2.0
        if s >= _MIN_MEMORY_SCORE:
            scored.append((s, m))
    scored.sort(key=lambda x: x[0], reverse=True)
    pack.memories = [m for _, m in scored[:max_memories]]

    # --- Entities (also bring in address-matches first) ---
    ent_scored: list[tuple[float, KnowledgeEntity]] = []
    for e in entities:
        s = _score_entity(e, terms, addresses)
        if s >= _MIN_ENTITY_SCORE:
            ent_scored.append((s, e))
    ent_scored.sort(key=lambda x: x[0], reverse=True)
    pack.entities = [e for _, e in ent_scored[:max_entities]]

    # --- Relations (with one-hop expansion) ---
    matched_entity_ids = {e.id for e in pack.entities}
    if expand_relations:
        # Pick up entities reached via relations we plan to include.
        for e in pack.entities:
            for r in relations:
                if r.src == e.id or r.dst == e.id:
                    matched_entity_ids.add(r.src)
                    matched_entity_ids.add(r.dst)

    rel_scored: list[tuple[float, KnowledgeRelation]] = []
    for r in relations:
        s = _score_relation(r, terms)
        # Boost relations touching any matched entity.
        if r.src in matched_entity_ids or r.dst in matched_entity_ids:
            s += 2.5
        if s >= _MIN_RELATION_SCORE:
            rel_scored.append((s, r))
    rel_scored.sort(key=lambda x: x[0], reverse=True)
    pack.relations = [r for _, r in rel_scored[:max_relations]]

    # --- Notes (parse on-demand for selected memory titles) ---
    if pack.memories:
        # Select notes whose title matches a memory title (cheap proxy).
        memory_titles = {m.title.lower() for m in pack.memories}
        scored_notes: list[tuple[int, str]] = []
        for pn in notes:
            title_l = pn.title.lower()
            body_l = pn.body.lower()
            if pn.title and title_l in memory_titles:
                scored_notes.append((10, _note_excerpt(pn)))
            else:
                # Generic keyword hits in title/body
                hits = sum(1 for t in terms if t in title_l or t in body_l)
                if hits:
                    scored_notes.append((hits, _note_excerpt(pn)))
        scored_notes.sort(key=lambda x: x[0], reverse=True)
        pack.notes = [body for _, body in scored_notes[:max_notes]]

    pack.counts = {
        "memories": len(pack.memories),
        "entities": len(pack.entities),
        "relations": len(pack.relations),
        "notes": len(pack.notes),
    }
    return pack


def retrieve(
    store: KnowledgeRawStore,
    paths: KnowledgePaths,
    query: RetrievalQuery,
    *,
    max_memories: int = 12,
    max_entities: int = 8,
    max_relations: int = 15,
    max_notes: int = 3,
    expand_relations: bool = True,
) -> RetrievalPack:
    """Build a ranked slice from the JSONL store.

    Loads records from *store* + filesystem notes, then delegates to
    :func:`retrieve_from_records` for the actual ranking.
    """
    memories = store.list_memories()
    entities = store.list_entities()
    relations = store.list_relations()
    notes: list[NoteExcerpt] = []
    if paths.notes_dir:
        try:
            parsed_notes = list_notes(paths.notes_dir)
        except Exception:
            parsed_notes = []
        notes = [NoteExcerpt(title=pn.title or "", body=pn.body or "") for pn in parsed_notes]
    return retrieve_from_records(
        memories,
        entities,
        relations,
        notes,
        query,
        max_memories=max_memories,
        max_entities=max_entities,
        max_relations=max_relations,
        max_notes=max_notes,
        expand_relations=expand_relations,
    )


def _note_excerpt(parsed_note, limit: int = 600) -> str:
    body = parsed_note.body or ""
    if len(body) <= limit:
        return body
    return body[: limit - 1].rstrip() + "…"


def _dedup(items: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for it in items:
        if it and it not in seen:
            seen[it] = None
    return list(seen.keys())


def search_all(
    store: KnowledgeRawStore,
    query: str,
    *,
    max_results: int = 50,
) -> dict[str, object]:
    """Lightweight search used by the ``/knowledge`` command.

    Returns a dict with ``memories``, ``entities``, ``relations``,
    ``notes`` lists sorted by relevance.
    """
    if not query.strip():
        return {"memories": [], "entities": [], "relations": [], "notes": []}
    rq = RetrievalQuery(text=query)
    pack = retrieve(
        store,
        # ``paths`` not used here — the only consumer is /knowledge in
        # the headless / non-IDA case where notes_dir may be missing.
        store.paths,
        rq,
        max_memories=max_results,
        max_entities=max_results,
        max_relations=max_results,
        max_notes=max_results,
    )
    return {
        "memories": pack.memories,
        "entities": pack.entities,
        "relations": pack.relations,
        "notes": pack.notes,
        "counts": dict(pack.counts),
    }


__all__ = [
    "NoteExcerpt",
    "RetrievalPack",
    "RetrievalQuery",
    "retrieve",
    "retrieve_from_records",
    "search_all",
]
