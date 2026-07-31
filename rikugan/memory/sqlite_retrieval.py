"""Adapter feeding SQLite repository records into the existing ranker.

The shared ranker (:func:`rikugan.memory.retrieve.retrieve_from_records`)
operates on lists of dataclasses. This module is the bridge that loads
records from :class:`SQLiteKnowledgeRepository` and wraps them in a
:class:`RetrievalQuery` derived from the caller's session context.

Notes remain filesystem-backed at the moment, so the adapter passes an
empty notes list.
"""

from __future__ import annotations

from .context import NORMAL_BUDGET, ContextBudget
from .repository import SQLiteKnowledgeRepository
from .retrieve import (
    NoteExcerpt,
    RetrievalPack,
    RetrievalQuery,
    retrieve_from_records,
)


def repository_to_retrieval_pack(
    repo: SQLiteKnowledgeRepository,
    *,
    current_address: str | None,
    current_function: str | None,
    active_mode: str,
    active_goal: str,
    budget: ContextBudget | None = None,
) -> RetrievalPack:
    """Read SQLite records and rank them via the shared ranker."""
    memories = repo.list_memories()
    entities = repo.list_entities()
    relations = repo.list_relations()
    notes: list[NoteExcerpt] = []  # Notes remain filesystem-backed; empty here.

    effective = budget or NORMAL_BUDGET
    query = RetrievalQuery(
        text="",
        function_name=current_function or "",
        address=current_address or "",
        active_mode=active_mode,
        active_goal=active_goal,
    )
    return retrieve_from_records(
        memories,
        entities,
        relations,
        notes,
        query,
        max_memories=effective.max_memories,
        max_entities=effective.max_entities,
        max_relations=effective.max_relations,
        max_notes=effective.max_notes,
    )


__all__ = ["repository_to_retrieval_pack"]
