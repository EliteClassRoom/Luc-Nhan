"""Tests for MemoryIdentityResolver: ordered copy/move/conflict decision table."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lucnhan.memory.identity import (
    IdentityChoice,
    MemoryIdentityResolver,
    ResolutionStatus,
    get_filesystem_identity,
    hash_raw_binary,
)
from lucnhan.memory.registry import MemoryRegistry
from lucnhan.memory.workspace import FilesystemIdentity, IdentityRequest


def _idb(
    path: Path,
    db_instance_id: str = "",
    filesystem: tuple[str, str] | None = None,
) -> IdentityRequest:
    """Build an IDB-mode IdentityRequest for tests."""
    return IdentityRequest(
        source_kind="idb",
        idb_path=str(path),
        db_instance_id=db_instance_id,
        display_name=path.name,
        filesystem_identity=FilesystemIdentity(*filesystem) if filesystem else None,
    )


def _raw(path: Path, digest: str) -> IdentityRequest:
    return IdentityRequest(
        source_kind="raw",
        idb_path=str(path),
        source_sha256=digest,
        display_name=path.name,
    )


class TestRawResolution:
    def test_raw_sha_resolves_and_reuses(self, tmp_path: Path) -> None:
        registry = MemoryRegistry(tmp_path / "registry.db")
        registry.initialize()
        resolver = MemoryIdentityResolver(registry)

        digest = "a" * 64
        first = resolver.resolve(_raw(tmp_path / "a.bin", digest))
        second = resolver.resolve(_raw(tmp_path / "a.bin", digest))

        assert first.status is ResolutionStatus.CREATED
        assert second.status is ResolutionStatus.RESOLVED
        assert first.binding is not None
        assert second.binding is not None
        assert first.binding.memory_id == second.binding.memory_id

    def test_raw_invalid_digest_rejected(self, tmp_path: Path) -> None:
        registry = MemoryRegistry(tmp_path / "registry.db")
        registry.initialize()
        resolver = MemoryIdentityResolver(registry)

        with pytest.raises(ValueError, match="sha256"):
            resolver.resolve(_raw(tmp_path / "a.bin", "XYZ"))


class TestIdbResolution:
    def test_new_idb_with_filesystem_evidence_creates(self, tmp_path: Path) -> None:
        registry = MemoryRegistry(tmp_path / "registry.db")
        registry.initialize()
        resolver = MemoryIdentityResolver(registry)

        result = resolver.resolve(_idb(tmp_path / "a.i64", "uuid-a", ("vol", "1")))

        assert result.status is ResolutionStatus.CREATED
        assert result.binding is not None
        assert result.binding.memory_id.startswith("mem-")

    def test_same_filesystem_evidence_resolves_existing(self, tmp_path: Path) -> None:
        registry = MemoryRegistry(tmp_path / "registry.db")
        registry.initialize()
        resolver = MemoryIdentityResolver(registry)

        first = resolver.resolve(_idb(tmp_path / "a.i64", "uuid-a", ("vol", "1")))
        # Same file moved to a different path with the same FS identity
        moved = resolver.resolve(_idb(tmp_path / "renamed.i64", "uuid-a", ("vol", "1")))

        assert first.status is ResolutionStatus.CREATED
        assert moved.status is ResolutionStatus.RESOLVED
        assert moved.binding is not None
        assert moved.binding.memory_id == first.binding.memory_id

    def test_path_alone_never_resolves(self, tmp_path: Path) -> None:
        registry = MemoryRegistry(tmp_path / "registry.db")
        registry.initialize()
        resolver = MemoryIdentityResolver(registry)

        result = resolver.resolve(_idb(tmp_path / "a.i64"))

        assert result.binding is not None
        assert result.binding.state == "ephemeral"
        assert result.binding.memory_id == ""

    def test_uuid_filesystem_conflict_returns_conflict(self, tmp_path: Path) -> None:
        """FS identity points to one workspace while UUID points to another."""
        registry = MemoryRegistry(tmp_path / "registry.db")
        registry.initialize()
        resolver = MemoryIdentityResolver(registry)

        # Create workspace A with fs ("vol","1") + uuid "uuid-a"
        first = resolver.resolve(_idb(tmp_path / "a.i64", "uuid-a", ("vol", "1")))
        assert first.binding is not None
        # Create workspace B with fs ("vol","2") (different fs evidence)
        second = resolver.resolve(_idb(tmp_path / "b.i64", "uuid-b", ("vol", "2")))
        assert second.binding is not None

        # Now resolve with fs ("vol","2") but uuid "uuid-a" — fs points
        # to workspace B while uuid points to workspace A → conflict.
        result = resolver.resolve(_idb(tmp_path / "c.i64", "uuid-a", ("vol", "2")))

        assert result.status is ResolutionStatus.CONFLICT
        assert result.binding is None

    def test_uuid_match_with_different_filesystem_links_existing(self, tmp_path: Path) -> None:
        """UUID is durable identity: different file index → link, not create new.

        On Windows, file index can change between opens. The netnode UUID
        survives reopen, so UUID match should link to the existing workspace
        rather than creating duplicates.
        """
        registry = MemoryRegistry(tmp_path / "registry.db")
        registry.initialize()
        resolver = MemoryIdentityResolver(registry)

        original = resolver.resolve(_idb(tmp_path / "a.i64", "uuid-a", ("vol", "7")))
        # Same UUID, different filesystem identity (simulating file index change)
        reopened = resolver.resolve(_idb(tmp_path / "a.i64", "uuid-a", ("vol", "99")))

        assert original.binding is not None
        assert reopened.binding is not None
        assert reopened.status is ResolutionStatus.RESOLVED
        assert reopened.binding.memory_id == original.binding.memory_id

    def test_without_persistence_choice_returns_ephemeral(self, tmp_path: Path) -> None:
        """``without_persistence`` choice returns ephemeral binding."""
        registry = MemoryRegistry(tmp_path / "registry.db")
        registry.initialize()
        resolver = MemoryIdentityResolver(registry)

        offline = resolver.resolve(
            _idb(tmp_path / "a.i64", "uuid-a", ("vol", "1")),
            IdentityChoice.without_persistence(),
        )
        assert offline.status is ResolutionStatus.EPHEMERAL
        assert offline.binding is not None
        assert offline.binding.memory_id == ""


class TestFilesystemIdentityHelpers:
    def test_hash_raw_binary_is_deterministic(self, tmp_path: Path) -> None:
        path = tmp_path / "sample.bin"
        path.write_bytes(b"hello world")
        assert hash_raw_binary(str(path)) == hash_raw_binary(str(path))
        assert len(hash_raw_binary(str(path))) == 64

    def test_hash_raw_binary_detects_mutation(self, tmp_path: Path) -> None:
        path = tmp_path / "sample.bin"
        path.write_bytes(b"hello world")
        digest1 = hash_raw_binary(str(path))
        path.write_bytes(b"hello world!!!")
        digest2 = hash_raw_binary(str(path))
        assert digest1 != digest2

    def test_get_filesystem_identity_returns_none_for_missing(self) -> None:
        assert get_filesystem_identity("/nonexistent/path/i64") is None

    def test_get_filesystem_identity_returns_stable_value(self, tmp_path: Path) -> None:
        path = tmp_path / "a.bin"
        path.write_bytes(b"data")
        ident = get_filesystem_identity(str(path))
        assert ident is not None
        assert ident == get_filesystem_identity(str(path))

    def test_copy_has_different_filesystem_identity(self, tmp_path: Path) -> None:
        original = tmp_path / "a.bin"
        original.write_bytes(b"data")
        copy = tmp_path / "b.bin"
        copy.write_bytes(b"data")

        assert get_filesystem_identity(str(original)) != get_filesystem_identity(str(copy))


class TestRealFilesystemRoundTrip:
    def test_rename_preserves_identity_and_copy_detaches(self, tmp_path: Path) -> None:
        """Integration: rename keeps identity, copy changes it."""
        original = tmp_path / "a.i64"
        original.write_bytes(b"idb-content")

        ident_before = get_filesystem_identity(str(original))

        # Rename
        renamed = tmp_path / "renamed.i64"
        os.rename(original, renamed)
        ident_after_rename = get_filesystem_identity(str(renamed))

        # On POSIX and Windows (file-index), a rename within the same
        # volume should preserve identity.
        if ident_before is not None and ident_after_rename is not None:
            assert ident_before.evidence_value == ident_after_rename.evidence_value

        # Copy
        copied = tmp_path / "copied.i64"
        import shutil

        shutil.copy2(renamed, copied)
        ident_copy = get_filesystem_identity(str(copied))

        if ident_after_rename is not None and ident_copy is not None:
            assert ident_after_rename.evidence_value != ident_copy.evidence_value
