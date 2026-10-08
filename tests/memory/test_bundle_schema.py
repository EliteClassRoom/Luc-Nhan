"""Tests for bundle schema validation."""

from __future__ import annotations

import pytest

from lucnhan.memory.bundle_schema import (
    ManifestFile,
    MemoryBundleManifest,
    validate_manifest,
    validate_member_name,
)


def _valid_manifest(**overrides) -> MemoryBundleManifest:
    defaults = dict(
        schema_version=1,
        scope="binary",
        export_mode="portable",
        origin_memory_id="mem-" + "a" * 32,
        exported_at="2025-01-01T00:00:00Z",
        files=(
            ManifestFile(name="manifest.json", sha256="a" * 64, uncompressed_size=100),
            ManifestFile(name="records/facts.jsonl", sha256="b" * 64, uncompressed_size=500, record_count=5),
        ),
        record_counts={"facts": 5},
    )
    defaults.update(overrides)
    return MemoryBundleManifest(**defaults)


class TestValidateMemberName:
    def test_valid_name(self) -> None:
        assert validate_member_name("records/facts.jsonl") == "records/facts.jsonl"

    def test_backslash_rejected(self) -> None:
        with pytest.raises(ValueError, match="backslash"):
            validate_member_name("records\\facts.jsonl")

    def test_absolute_rejected(self) -> None:
        with pytest.raises(ValueError, match="absolute"):
            validate_member_name("/etc/passwd")

    def test_traversal_rejected(self) -> None:
        with pytest.raises(ValueError, match="traversal"):
            validate_member_name("../escape.jsonl")

    def test_nul_rejected(self) -> None:
        with pytest.raises(ValueError, match="NUL"):
            validate_member_name("file\x00.jsonl")


class TestValidateManifest:
    def test_valid_manifest_accepted(self) -> None:
        validate_manifest(_valid_manifest())

    def test_wrong_schema_version_rejected(self) -> None:
        with pytest.raises(ValueError, match="schema version"):
            validate_manifest(_valid_manifest(schema_version=99))

    def test_invalid_scope_rejected(self) -> None:
        with pytest.raises(ValueError, match="scope"):
            validate_manifest(_valid_manifest(scope="bogus"))

    def test_duplicate_member_rejected(self) -> None:
        m = _valid_manifest(
            files=(
                ManifestFile(name="a.jsonl", sha256="a" * 64, uncompressed_size=1),
                ManifestFile(name="a.jsonl", sha256="a" * 64, uncompressed_size=1),
            ),
        )
        with pytest.raises(ValueError, match="duplicate"):
            validate_manifest(m)

    def test_invalid_sha256_rejected(self) -> None:
        m = _valid_manifest(
            files=(ManifestFile(name="a.jsonl", sha256="short", uncompressed_size=1),),
        )
        with pytest.raises(ValueError, match="sha256"):
            validate_manifest(m)

    def test_size_limit_overflow_rejected(self) -> None:
        m = _valid_manifest(
            files=(ManifestFile(name="big.jsonl", sha256="a" * 64, uncompressed_size=999 * 1024 * 1024),),
        )
        with pytest.raises(ValueError, match="uncompressed size"):
            validate_manifest(m)
