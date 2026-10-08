"""Write authority and candidate protocol for central memory persistence.

The controller issues an opaque :class:`MemoryWriteAuthority` for the main
agent. This token is bound to a frozen :class:`MemoryRunContext` and cannot
be serialized — it must be passed explicitly through the call stack, never
accepted from LLM or subagent arguments.

Subagents and background workers emit :class:`MemoryCandidate` records via
a :class:`MemoryCandidateSink`; the main agent or UI decides whether to
persist them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .workspace import MemoryRunContext


class MemoryWriteDenied(RuntimeError):
    """Raised when a persistent write lacks valid authority."""


@dataclass(frozen=True)
class CandidateSourceRef:
    """Typed source reference for a candidate or promotion."""

    source_memory_id: str
    source_record_id: str
    source_revision: int
    source_hash: str
    namespace_address: str = ""


class MemoryWriteAuthority:
    """Opaque, non-serializable authority bound to a run context.

    The ``_nonce`` is a private sentinel object shared with the issuing
    :class:`MemoryAuthorityIssuer`; it cannot be forged by external code.
    """

    __slots__ = ("_context", "_nonce")

    def __init__(self, context: MemoryRunContext, nonce: object) -> None:
        self._context = context
        self._nonce = nonce

    def __reduce__(self) -> object:
        raise TypeError("MemoryWriteAuthority cannot be serialized")


class MemoryAuthorityIssuer:
    """Controller-owned issuer for write authorities.

    Do not pass this object into AgentLoop or subagents — it is the single
    trusted source of authorities for a controller lifetime.
    """

    __slots__ = ("_nonce",)

    def __init__(self) -> None:
        self._nonce = object()

    def issue(self, context: MemoryRunContext) -> MemoryWriteAuthority:
        """Issue a new authority bound to *context*."""
        return MemoryWriteAuthority(context, self._nonce)

    def require(
        self,
        authority: MemoryWriteAuthority | None,
        context: MemoryRunContext,
    ) -> MemoryWriteAuthority:
        """Validate that *authority* matches *context* and this issuer."""
        if authority is None or authority._nonce is not self._nonce or authority._context != context:
            raise MemoryWriteDenied("persistent memory write authority required")
        return authority


@dataclass(frozen=True)
class MemoryCandidate:
    """Bounded candidate record submitted by a subagent for explicit review."""

    source: str
    kind: str
    title: str
    content: str
    confidence: float
    source_refs: tuple[CandidateSourceRef, ...] = ()


class MemoryCandidateSink(Protocol):
    """Protocol for queuing bounded candidates for main-agent review."""

    def submit_candidate(self, candidate: MemoryCandidate) -> None:
        """Queue a bounded candidate for explicit main-agent review."""
        ...
