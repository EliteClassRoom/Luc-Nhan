"""Tests for raw-binary memory identity transport in headless mode.

The CLI hashes the original binary before launching IDA and carries the
SHA-256 through the bootstrap config. The bootstrap validates it into a
typed :class:`IdentityRequest` (not a raw dict).
"""

from __future__ import annotations

import re
from pathlib import Path

from lucnhan.cli.headless import _build_memory_source
from lucnhan.ida.headless_bootstrap import validate_bootstrap_memory_source
from lucnhan.memory.workspace import IdentityRequest


class TestBuildMemorySource:
    def test_raw_input_carries_sha256_before_ida_launch(self, tmp_path: Path) -> None:
        sample = tmp_path / "sample.bin"
        sample.write_bytes(b"abc")

        source = _build_memory_source(str(sample))

        assert source is not None
        assert source.source_kind == "raw"
        assert source.source_sha256 != ""
        assert re.fullmatch(r"[0-9a-f]{64}", source.source_sha256)
        assert source.display_name == "sample.bin"

    def test_missing_binary_returns_none(self, tmp_path: Path) -> None:
        source = _build_memory_source(str(tmp_path / "nonexistent.bin"))
        assert source is None

    def test_idb_input_has_no_hash(self, tmp_path: Path) -> None:
        idb = tmp_path / "sample.i64"
        idb.write_bytes(b"database")

        source = _build_memory_source(str(idb))

        assert source is not None
        assert source.source_kind == "idb"
        assert source.source_sha256 == ""


class TestValidateBootstrapMemorySource:
    def test_raw_source_validates_into_identity_request(self) -> None:
        digest = "a" * 64
        result = validate_bootstrap_memory_source({"kind": "raw", "original_path": "/tmp/sample.bin", "sha256": digest})

        assert result is not None
        assert isinstance(result, IdentityRequest)
        assert result.source_kind == "raw"
        assert result.source_sha256 == digest
        assert result.display_name == "sample.bin"

    def test_idb_source_without_hash_accepted(self) -> None:
        result = validate_bootstrap_memory_source({"kind": "idb", "original_path": "/tmp/sample.i64"})

        assert result is not None
        assert result.source_kind == "idb"
        assert result.source_sha256 == ""

    def test_non_dict_returns_none(self) -> None:
        assert validate_bootstrap_memory_source("not a dict") is None
        assert validate_bootstrap_memory_source(None) is None
        assert validate_bootstrap_memory_source(42) is None

    def test_invalid_kind_returns_none(self) -> None:
        result = validate_bootstrap_memory_source({"kind": "bogus", "original_path": "/tmp/sample.bin"})
        assert result is None

    def test_raw_source_missing_sha256_returns_none(self) -> None:
        result = validate_bootstrap_memory_source({"kind": "raw", "original_path": "/tmp/sample.bin"})
        assert result is None

    def test_raw_source_uppercase_sha256_returns_none(self) -> None:
        result = validate_bootstrap_memory_source(
            {"kind": "raw", "original_path": "/tmp/sample.bin", "sha256": "A" * 64}
        )
        assert result is None

    def test_raw_source_short_sha256_returns_none(self) -> None:
        result = validate_bootstrap_memory_source(
            {"kind": "raw", "original_path": "/tmp/sample.bin", "sha256": "a" * 10}
        )
        assert result is None

    def test_raw_source_non_string_sha256_returns_none(self) -> None:
        result = validate_bootstrap_memory_source({"kind": "raw", "original_path": "/tmp/sample.bin", "sha256": 12345})
        assert result is None
