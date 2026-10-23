"""BinaryMemoryService: façade for prompt read, structured retrieval, and writes.

This service owns the prompt-source separation: structured facts come from
SQLite, manual notes come from unmanaged ``MEMORY.md``. All writes require
a valid :class:`MemoryWriteAuthority` from the controller-owned issuer.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from ..core.sanitize import strip_injection_markers, strip_lone_surrogates
from .authority import MemoryAuthorityIssuer, MemoryWriteAuthority
from .markdown import MemoryProjector, extract_unmanaged_markdown
from .repository import SQLiteKnowledgeRepository
from .workspace import MemoryRunContext, WorkspacePaths
from .workspace_store import WorkspaceStore


class StaleMemoryContext(RuntimeError):
    """Raised when the run context has changed since authority was issued."""


@dataclass(frozen=True)
class SaveMemoryResult:
    """Result of a successful fact save."""

    record_id: str
    revision: int
    outcome: Literal["created", "deduplicated"]
    projection_dirty: bool
    warning: str


class BinaryMemoryService:
    """Façade for one binary workspace's prompt/retrieval/write operations.

    Parameters
    ----------
    context:
        Frozen run context for the current agent run.
    paths:
        Workspace filesystem paths.
    repository:
        SQLite knowledge repository.
    store:
        Underlying workspace store (for projection state).
    projector:
        MEMORY.md projector.
    authority_issuer:
        Controller-owned authority issuer (never passed into subagents).
    context_validator:
        Callable that returns True if a candidate context matches the
        current binding. Defaults to exact equality.
    """

    def __init__(
        self,
        *,
        context: MemoryRunContext,
        paths: WorkspacePaths,
        repository: SQLiteKnowledgeRepository,
        store: WorkspaceStore,
        projector: MemoryProjector,
        authority_issuer: MemoryAuthorityIssuer,
        context_validator: Callable[[MemoryRunContext], bool] | None = None,
    ) -> None:
        self.context = context
        self.paths = paths
        self.repository = repository
        self.store = store
        self.projector = projector
        self._authority_issuer = authority_issuer
        self._context_validator = context_validator or (lambda c: c == context)
        # (mtime_ns, size) -> sanitized notes. MEMORY.md is re-read + fully
        # sanitized on every turn; the file only changes when the agent or
        # user writes notes, which bumps mtime/size.
        self._manual_notes_cache: tuple[tuple[int, int], str] | None = None
        # Serializes reads/writes of ``_manual_notes_cache`` so the service
        # can be called concurrently (agent background thread + UI thread)
        # without racing on the cache tuple.
        self._manual_notes_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Authority
    # ------------------------------------------------------------------

    def require_write_authority(self, authority: MemoryWriteAuthority | None) -> None:
        """Validate authority + context freshness."""
        self._authority_issuer.require(authority, self.context)
        if not self._context_validator(self.context):
            raise StaleMemoryContext("database binding changed since authority was issued")

    # ------------------------------------------------------------------
    # Prompt sources
    # ------------------------------------------------------------------

    def structured_context(self, query: str = "", *, mode: str = "normal", max_chars: int = 0) -> str:
        """Return structured facts from SQLite, filtered by *query*.

        Never reads from MEMORY.md. Output contains only current facts.

        *max_chars* caps the returned string (~line boundary).  The per-turn
        prompt path builds hundreds of KB of formatted facts and then drops
        all but ``MEMORY_MAX_CHARS`` — stop formatting early instead of
        building (and sanitizing) the whole corpus.
        """
        facts = self.repository.list_memories()
        if query:
            query_lower = query.lower()
            facts = [
                f
                for f in facts
                if query_lower in f.title.lower() or query_lower in f.content.lower() or query_lower in f.type.lower()
            ]

        if not facts:
            return ""

        lines = ["## Structured Memory"]
        total = len(lines[0])
        for fact in sorted(facts, key=lambda f: (f.type, f.title)):
            line = f"- [{fact.type}] {_sanitize_prompt_text(fact.title)}: {_sanitize_prompt_text(fact.content)}"
            if max_chars and total + len(line) + 1 > max_chars:
                break
            lines.append(line)
            total += len(line) + 1
        return "\n".join(lines)

    def manual_notes_context(self) -> str:
        """Return sanitized unmanaged Markdown notes from MEMORY.md.

        Never includes the managed region or hidden record markers.

        The unmanaged body is run through :func:`strip_lone_surrogates` +
        :func:`strip_injection_markers` (the same core sanitization as
        :func:`lucnhan.core.sanitize.sanitize_memory`) *before* returning, so
        prompt-injection markers never survive even when a caller embeds the
        text directly (e.g. the ``/show memory`` command) rather than through
        the system-prompt layer. We deliberately do NOT re-apply the
        ``<persistent_memory>`` wrapper + preamble here — that is done once by
        :func:`lucnhan.agent.system_prompt.build_system_prompt`.
        """
        markdown = self.paths.markdown
        if not markdown.exists():
            return ""
        # Stat-only until the signature changes: the file is re-read only
        # when its mtime/size moves, so a long session pays one read per
        # write instead of one read per turn.
        sig = (markdown.stat().st_mtime_ns, markdown.stat().st_size)
        with self._manual_notes_lock:
            if self._manual_notes_cache is not None and self._manual_notes_cache[0] == sig:
                return self._manual_notes_cache[1]
        content = markdown.read_text(encoding="utf-8")
        unmanaged = extract_unmanaged_markdown(content)
        sanitized = _sanitize_manual_notes(unmanaged)
        result = sanitized.strip()
        with self._manual_notes_lock:
            self._manual_notes_cache = (sig, result)
        return result

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def save_fact(
        self,
        authority: MemoryWriteAuthority | None,
        *,
        category: str,
        fact: str,
        source: str,
    ) -> SaveMemoryResult:
        """Save a structured fact and project it to MEMORY.md."""
        self.require_write_authority(authority)
        normalized_category = _sanitize_category(category)
        normalized_fact = _sanitize_fact(fact)
        if not normalized_category:
            raise ValueError("category must not be empty after sanitization")
        if not normalized_fact:
            raise ValueError("fact must not be empty after sanitization")

        saved = self.repository.save_memory_fact(
            normalized_category,
            normalized_fact,
            source,
        )
        record = saved.record
        # ``projection_dirty`` is derived from ``store.projection_state()``
        # rather than local exception flags — the projector + store are
        # the single source of truth.
        try:
            self.projector.project(self.paths, self.store)
            return SaveMemoryResult(
                record_id=record.id,
                revision=getattr(record, "revision", 1),
                outcome=saved.outcome,
                projection_dirty=self.store.projection_state().projection_dirty,
                warning="",
            )
        except Exception as exc:
            from ..core.logging import log_error as _le

            _le(f"MEMORY.md projection failed: {exc!r}")
            self.store.mark_projection_dirty()
            return SaveMemoryResult(
                record_id=record.id,
                revision=getattr(record, "revision", 1),
                outcome=saved.outcome,
                projection_dirty=self.store.projection_state().projection_dirty,
                warning=str(exc),
            )

    def save_plan(
        self,
        authority: MemoryWriteAuthority | None,
        *,
        goal: str,
        steps: list[str],
    ) -> SaveMemoryResult:
        """Save an approved plan as a structured plan fact."""
        self.require_write_authority(authority)
        safe_goal = _sanitize_fact(goal)
        if not safe_goal:
            raise ValueError("goal must not be empty")
        content = "\n".join(f"{i + 1}. {_sanitize_fact(s)}" for i, s in enumerate(steps))
        return self.save_fact(
            authority,
            category="plan",
            fact=f"{safe_goal}\n{content}",
            source="approved_plan",
        )

    def save_exploration_finding(
        self,
        authority: MemoryWriteAuthority | None,
        *,
        category: str,
        title: str,
        content: str,
        confidence: float,
        entity_refs: list[str],
        tags: list[str],
        source: str,
        status: str = "unverified",
        verdict_claim: str = "",
        verification_citations: list[str] | None = None,
    ) -> SaveMemoryResult:
        """Save an exploration finding with graph metadata and project it.

        Mirrors :meth:`save_fact` but forwards *entity_refs* and *tags* to
        :meth:`SQLiteKnowledgeRepository.save_exploration_finding` so the
        graph metadata columns on the underlying ``facts`` row are
        populated on first write. *title* is preserved verbatim so the
        caller can choose a human-readable label distinct from the
        canonical *category*.

        Verifies the fact was committed before returning and surfaces
        projection failures via :attr:`SaveMemoryResult.projection_dirty`
        + :attr:`SaveMemoryResult.warning` without rolling back the
        SQLite write.
        """
        self.require_write_authority(authority)
        normalized_category = _sanitize_category(category)
        normalized_content = _sanitize_fact(content)
        if not normalized_category:
            raise ValueError("category must not be empty after sanitization")
        if not normalized_content:
            raise ValueError("content must not be empty after sanitization")

        # A finding's metadata (title, entity/tag refs, hypothesis verdict) is
        # persisted verbatim into SQLite and projected into MEMORY.md, which is
        # loaded into every later prompt. The title defaults to the first line
        # of the (binary/MCP-derived) summary, so it can carry injection
        # markers just like the body. Run every user/agent-authored field
        # through ``strip_injection_markers`` — mirroring ``save_fact`` —
        # before it reaches storage.
        normalized_title = _sanitize_short_text(title)
        normalized_entity_refs = [_sanitize_short_text(r) for r in entity_refs]
        normalized_tags = [_sanitize_short_text(t) for t in tags]
        normalized_verdict = _sanitize_fact(verdict_claim) if normalized_category == "hypothesis" else ""
        normalized_citations = (
            [_sanitize_short_text(c) for c in (verification_citations or [])]
            if normalized_category == "hypothesis"
            else None
        )

        saved = self.repository.save_exploration_finding(
            normalized_category,
            normalized_content,
            source,
            entity_refs=normalized_entity_refs,
            tags=normalized_tags,
            title=normalized_title,
            confidence=confidence,
            status=status if normalized_category == "hypothesis" else "unverified",
            verdict_claim=normalized_verdict,
            verification_citations=normalized_citations,
        )
        record = saved.record
        # ``projection_dirty`` mirrors the store's view; the projector
        # and the store are the single source of truth.
        try:
            self.projector.project(self.paths, self.store)
            return SaveMemoryResult(
                record_id=record.id,
                revision=getattr(record, "revision", 1),
                outcome=saved.outcome,
                projection_dirty=self.store.projection_state().projection_dirty,
                warning="",
            )
        except Exception as exc:
            from ..core.logging import log_error as _le

            _le(f"MEMORY.md projection failed: {exc!r}")
            self.store.mark_projection_dirty()
            return SaveMemoryResult(
                record_id=record.id,
                revision=getattr(record, "revision", 1),
                outcome=saved.outcome,
                projection_dirty=self.store.projection_state().projection_dirty,
                warning=str(exc),
            )


# ---------------------------------------------------------------------------
# Sanitization helpers
# ---------------------------------------------------------------------------

_PROMPT_INJECTION_RE = re.compile(r"<!--\s*lucnhan:|-->|</?(?:system|user|assistant)>", re.IGNORECASE)


def _sanitize_category(value: str) -> str:
    """Sanitize a fact category for storage."""
    return strip_injection_markers(value).strip()[:100]


def _sanitize_fact(value: str) -> str:
    """Sanitize a fact body for storage."""
    cleaned = strip_injection_markers(value)
    cleaned = _PROMPT_INJECTION_RE.sub("", cleaned)
    return cleaned.strip()[:8000]


def _sanitize_manual_notes(value: str) -> str:
    """Sanitize unmanaged MEMORY.md notes for prompt/display.

    Applies the same core sanitization as
    :func:`lucnhan.core.sanitize.sanitize_memory` (lone-surrogate + injection
    marker stripping + explicit ``_PROMPT_INJECTION_RE``) but returns the
    *bare* sanitized body — the ``<persistent_memory>`` wrapper + preamble is
    added once by the system-prompt layer. See
    :meth:`BinaryMemoryService.manual_notes_context`.
    """
    cleaned = strip_lone_surrogates(value)
    cleaned = strip_injection_markers(cleaned)
    cleaned = _PROMPT_INJECTION_RE.sub("", cleaned)
    return cleaned.strip()


def _sanitize_short_text(value: str) -> str:
    """Sanitize a short metadata string (title, tags, refs, citations)."""
    cleaned = strip_injection_markers(value)
    cleaned = _PROMPT_INJECTION_RE.sub("", cleaned)
    return cleaned.strip()[:500]


def _sanitize_prompt_text(value: str) -> str:
    """Sanitize text before including it in a prompt context block."""
    cleaned = _PROMPT_INJECTION_RE.sub("", value)
    return cleaned.strip()
