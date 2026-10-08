"""Analysis case schema: relation types, canonicalization, validation.

Defines the five cross-binary relation types and their directionality,
plus validation helpers for case membership and relation creation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Literal


class CaseRelationType(str, Enum):
    """Five predicates for cross-binary case relations."""

    EMBEDS_OR_LOADS = "embeds_or_loads"
    COMMUNICATES_WITH = "communicates_with"
    DERIVED_FROM = "derived_from"
    SAME_FAMILY_AS = "same_family_as"
    SHARES_ARTIFACT_WITH = "shares_artifact_with"

    @property
    def direction(self) -> Literal["directed", "symmetric"]:
        """Whether the predicate is directed or symmetric."""
        if self in (
            CaseRelationType.COMMUNICATES_WITH,
            CaseRelationType.SAME_FAMILY_AS,
            CaseRelationType.SHARES_ARTIFACT_WITH,
        ):
            return "symmetric"
        return "directed"


@dataclass(frozen=True)
class CaseRecord:
    """One case row in the registry."""

    case_id: str
    name: str
    state: Literal["active", "disabled", "deleted"]
    revision: int
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class CaseMember:
    """One case membership row."""

    case_id: str
    memory_id: str
    status: Literal["current", "removed"]
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class PromotionSource:
    """Typed source reference for a promotion."""

    source_memory_id: str
    source_record_id: str
    source_revision: int
    source_hash: str
    namespace_address: str = ""


@dataclass(frozen=True)
class CaseRelation:
    """One cross-binary relation within a case."""

    relation_id: str
    case_id: str
    subject_memory_id: str
    predicate: CaseRelationType
    object_memory_id: str
    confidence: float
    sources: tuple[PromotionSource, ...]
    artifact_ref: str = ""
    revision: int = 1
    state: Literal["current", "inactive"] = "current"


@dataclass(frozen=True)
class CasePromotion:
    """One promotion of a binary fact into a case."""

    promotion_id: str
    case_fact_id: str
    case_id: str
    promotion_kind: str
    source: PromotionSource
    revision: int


def canonicalize_relation_endpoints(
    subject: str,
    predicate: CaseRelationType,
    obj: str,
) -> tuple[str, str]:
    """Canonicalize endpoint order for symmetric predicates.

    Directed predicates preserve the original (subject, object) order.
    Symmetric predicates sort endpoints by ``memory_id`` so the same
    logical relation always produces the same canonical pair.
    """
    if predicate.direction == "symmetric":
        return (subject, obj) if subject <= obj else (obj, subject)
    return subject, obj


def validate_case_relation(
    subject: str,
    predicate: CaseRelationType,
    obj: str,
    *,
    confidence: float = 0.5,
    artifact_ref: str = "",
) -> None:
    """Validate relation invariants. Raises ``ValueError`` on violation."""
    if subject == obj:
        raise ValueError("self-relation is not allowed")
    if predicate is CaseRelationType.SHARES_ARTIFACT_WITH and not artifact_ref:
        raise ValueError("shares_artifact_with requires an artifact reference")
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence must be finite and within [0, 1], got {confidence}")


def utc_now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(UTC).isoformat()
