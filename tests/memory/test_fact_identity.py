"""Tests for the pure fact identity contract.

The ``rikugan.memory.fact_identity`` module is the lowest layer of the
memory durability feature: a pure helper that canonicalizes fact
type/content, produces semantic hashes, and builds deterministic
import record IDs. It must not import from ``workspace_store`` or
``repository`` (those depend on it during the migration window).
"""

from __future__ import annotations

import re

import pytest

from rikugan.memory.fact_identity import (
    canonicalize_fact_content,
    canonicalize_fact_type,
    deterministic_import_record_id,
    semantic_fact_hash,
)


def test_fact_type_normalizes_unicode_whitespace_and_case() -> None:
    assert canonicalize_fact_type("  Function Purpose  ") == "function purpose"


def test_fact_content_normalizes_line_endings_nfc_and_outer_space() -> None:
    assert canonicalize_fact_content("  Café\r\nLine 2\r  ") == "Café\nLine 2"


def test_fact_content_preserves_internal_case_spacing_punctuation_and_order() -> None:
    assert semantic_fact_hash("note", "A  B!") != semantic_fact_hash("note", "a B!")
    assert semantic_fact_hash("note", "first\nsecond") != semantic_fact_hash("note", "second\nfirst")


@pytest.mark.parametrize("value", ["", "   ", "\r\n"])
def test_empty_canonical_values_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        canonicalize_fact_type(value)
    with pytest.raises(ValueError):
        canonicalize_fact_content(value)


def test_semantic_hash_is_stable_lowercase_sha256() -> None:
    first = semantic_fact_hash(" Function Purpose ", " Uses RC4\r\n")
    second = semantic_fact_hash("function   purpose", "Uses RC4\n")
    assert first == second
    assert re.fullmatch(r"[0-9a-f]{64}", first)


def test_import_record_id_is_target_scoped_and_validator_shaped() -> None:
    first = deterministic_import_record_id("mem-" + "a" * 32, "import-1234", "fact", "origin-1")
    again = deterministic_import_record_id("mem-" + "a" * 32, "import-1234", "fact", "origin-1")
    other_target = deterministic_import_record_id("mem-" + "b" * 32, "import-1234", "fact", "origin-1")
    assert first == again
    assert first != other_target
    assert re.fullmatch(r"fact-[0-9a-f]{32}", first)
