"""Tests for case schema: relation types, canonicalization, validation."""

from __future__ import annotations

import pytest

from lucnhan.memory.case_schema import (
    CaseRelationType,
    canonicalize_relation_endpoints,
    validate_case_relation,
)


class TestCaseRelationType:
    def test_five_predicates_exist(self) -> None:
        assert CaseRelationType.EMBEDS_OR_LOADS
        assert CaseRelationType.COMMUNICATES_WITH
        assert CaseRelationType.DERIVED_FROM
        assert CaseRelationType.SAME_FAMILY_AS
        assert CaseRelationType.SHARES_ARTIFACT_WITH

    def test_symmetric_predicates(self) -> None:
        assert CaseRelationType.COMMUNICATES_WITH.direction == "symmetric"
        assert CaseRelationType.SAME_FAMILY_AS.direction == "symmetric"
        assert CaseRelationType.SHARES_ARTIFACT_WITH.direction == "symmetric"

    def test_directed_predicates(self) -> None:
        assert CaseRelationType.EMBEDS_OR_LOADS.direction == "directed"
        assert CaseRelationType.DERIVED_FROM.direction == "directed"


class TestCanonicalization:
    def test_symmetric_endpoints_are_canonicalized(self) -> None:
        a = "mem-" + "a" * 32
        b = "mem-" + "b" * 32

        result = canonicalize_relation_endpoints(b, CaseRelationType.COMMUNICATES_WITH, a)
        assert result == (a, b)

    def test_directed_endpoints_preserve_order(self) -> None:
        a = "mem-" + "a" * 32
        b = "mem-" + "b" * 32

        result = canonicalize_relation_endpoints(b, CaseRelationType.DERIVED_FROM, a)
        assert result == (b, a)


class TestValidateCaseRelation:
    def test_shares_artifact_requires_artifact_ref(self) -> None:
        a = "mem-" + "a" * 32
        b = "mem-" + "b" * 32
        with pytest.raises(ValueError, match="artifact"):
            validate_case_relation(a, CaseRelationType.SHARES_ARTIFACT_WITH, b, artifact_ref="")

    def test_self_relation_rejected(self) -> None:
        a = "mem-" + "a" * 32
        with pytest.raises(ValueError, match="self"):
            validate_case_relation(a, CaseRelationType.COMMUNICATES_WITH, a, artifact_ref="")

    def test_confidence_out_of_range_rejected(self) -> None:
        a = "mem-" + "a" * 32
        b = "mem-" + "b" * 32
        for bad in (-0.1, 1.5):
            with pytest.raises(ValueError, match="confidence"):
                validate_case_relation(a, CaseRelationType.COMMUNICATES_WITH, b, confidence=bad)
