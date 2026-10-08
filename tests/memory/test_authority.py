"""Tests for MemoryWriteAuthority and MemoryCandidateSink protocol."""

from __future__ import annotations

import pickle

import pytest

from lucnhan.memory.authority import (
    CandidateSourceRef,
    MemoryAuthorityIssuer,
    MemoryCandidate,
    MemoryWriteDenied,
)
from lucnhan.memory.workspace import MemoryRunContext, new_memory_id


def _context() -> MemoryRunContext:
    return MemoryRunContext(new_memory_id(), "", 1, 0)


class TestAuthority:
    def test_authority_is_identity_bound_and_not_serializable(self) -> None:
        issuer = MemoryAuthorityIssuer()
        context = _context()
        authority = issuer.issue(context)

        assert issuer.require(authority, context) is authority
        with pytest.raises((pickle.PicklingError, TypeError)):
            pickle.dumps(authority)

    def test_missing_or_wrong_authority_is_rejected(self) -> None:
        issuer = MemoryAuthorityIssuer()
        context = _context()
        with pytest.raises(MemoryWriteDenied):
            issuer.require(None, context)
        with pytest.raises(MemoryWriteDenied):
            issuer.require(issuer.issue(_context()), context)

    def test_authority_rejects_new_attributes(self) -> None:
        """Authority uses __slots__ so new attributes cannot be added."""
        issuer = MemoryAuthorityIssuer()
        context = _context()
        authority = issuer.issue(context)
        with pytest.raises(AttributeError):
            authority.extra = "injected"  # type: ignore[attr-defined]

    def test_wrong_issuer_rejected(self) -> None:
        issuer_a = MemoryAuthorityIssuer()
        issuer_b = MemoryAuthorityIssuer()
        context = _context()
        authority = issuer_a.issue(context)
        with pytest.raises(MemoryWriteDenied):
            issuer_b.require(authority, context)


class TestCandidate:
    def test_candidate_source_ref_defaults(self) -> None:
        ref = CandidateSourceRef(
            source_memory_id="mem-" + "a" * 32,
            source_record_id="fact-" + "b" * 32,
            source_revision=3,
            source_hash="abc123",
        )
        assert ref.namespace_address == ""

    def test_candidate_defaults(self) -> None:
        candidate = MemoryCandidate(
            source="test",
            kind="fact",
            title="Test",
            content="Content",
            confidence=0.8,
        )
        assert candidate.source_refs == ()
