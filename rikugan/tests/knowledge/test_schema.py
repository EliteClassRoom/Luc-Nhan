"""Tests for rikugan.memory.schema dataclass contracts.

Covers the legacy ``verified=True`` round-trip invariant: records
written before the ``/verify`` flow (which carry ``verified=True``
without an explicit ``status`` field) MUST NOT be auto-promoted to
``status="verified"`` on load. Only an explicit ``/verify`` verdict
may surface a hypothesis in ``/report``.

Regression test for the auto-promote bug where
``KnowledgeMemory.__post_init__`` re-derived ``status="verified"``
from a legacy ``verified=True`` flag, contradicting the documented
"only explicit /verify verdict may authorize /report" contract.
"""

from __future__ import annotations

from rikugan.memory.schema import KnowledgeMemory


def _memory_dict(**overrides) -> dict:
    """Build a minimal hypothesis memory dict for from_dict round-trips."""
    base: dict = {
        "id": "mem:hyp:test",
        "binary_id": "fake-123",
        "type": "hypothesis",
        "title": "Test hypothesis",
        "content": "Some claim",
        "confidence": 0.7,
    }
    base.update(overrides)
    return base


def test_legacy_verified_true_does_not_auto_promote_status() -> None:
    """A legacy record with ``verified=True`` and no ``status`` field
    must stay ``status="unverified"`` after round-trip. Auto-promotion
    would let pre-/verify records surface in ``/report``, violating
    the explicit-verdict-only contract.
    """
    legacy = KnowledgeMemory.from_dict(_memory_dict(verified=True))
    assert legacy.type == "hypothesis"
    assert legacy.status == "unverified", (
        f"BUG: legacy record auto-promoted to status={legacy.status!r}; must stay unverified until /verify runs"
    )
    assert legacy.verified is False, (
        f"BUG: legacy.verified={legacy.verified!r}; must be derived from "
        "status and equal False when status='unverified'"
    )
    assert legacy.verdict_claim == ""
    assert legacy.verification_citations == []


def test_explicit_verified_status_round_trip() -> None:
    """A record with explicit ``status="verified"`` from /verify stays
    verified after round-trip.
    """
    mem = KnowledgeMemory.from_dict(
        _memory_dict(
            status="verified",
            verdict_claim="confirmed by independent verifier",
            verification_citations=["function:sub_401000"],
        )
    )
    assert mem.status == "verified"
    assert mem.verified is True
    assert mem.verdict_claim == "confirmed by independent verifier"
    assert mem.verification_citations == ["function:sub_401000"]


def test_explicit_wrong_status_round_trip() -> None:
    """A record with explicit ``status="wrong"`` from /verify stays
    unverified (verified derived False).
    """
    mem = KnowledgeMemory.from_dict(
        _memory_dict(
            status="wrong",
            verdict_claim="claim contradicted by decompile",
            verification_citations=["address:0x401000"],
        )
    )
    assert mem.status == "wrong"
    assert mem.verified is False


def test_non_hypothesis_type_ignores_status_logic() -> None:
    """Non-hypothesis records (fact, function_purpose, ...) must not
    be affected by the hypothesis status invariant. ``verified=True``
    is preserved verbatim for report-style or ingest paths.
    """
    rec = KnowledgeMemory.from_dict(
        {
            "id": "mem:report:test",
            "binary_id": "fake-123",
            "type": "report",
            "title": "Generated report",
            "content": "body",
            "verified": True,
            "confidence": 0.9,
        }
    )
    assert rec.type == "report"
    assert rec.verified is True  # not hypothesis; no auto-promotion logic applies
    assert rec.status == "unverified"  # field default still applies


def test_invalid_status_raises() -> None:
    """An out-of-vocabulary ``status`` value is rejected at construction."""
    try:
        KnowledgeMemory(
            id="mem:hyp:bad",
            binary_id="fake-123",
            type="hypothesis",
            title="t",
            content="c",
            status="maybe",  # type: ignore[arg-type]
        )
    except ValueError as exc:
        assert "invalid hypothesis status" in str(exc), f"unexpected error message: {exc}"
    else:
        raise AssertionError("expected ValueError for invalid status")


def test_constructor_verified_is_always_derived_from_status() -> None:
    """Direct constructor: passing ``verified=True`` with
    ``status="unverified"`` still derives ``verified=False`` after
    ``__post_init__`` — the legacy-promotion code path is gone.
    """
    mem = KnowledgeMemory(
        id="mem:hyp:construct",
        binary_id="fake-123",
        type="hypothesis",
        title="t",
        content="c",
        status="unverified",
        verified=True,  # caller mistake; __post_init__ corrects this
    )
    assert mem.verified is False, (
        "BUG: direct constructor still allowed verified=True with "
        "status='unverified'; invariant says verified is derived"
    )
    assert mem.status == "unverified"
