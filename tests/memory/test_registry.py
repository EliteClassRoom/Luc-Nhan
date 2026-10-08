"""Tests for MemoryRegistry: CRUD, evidence uniqueness, first-open serialization."""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

import pytest

from lucnhan.memory.registry import (
    EvidenceConflictError,
    MemoryRegistry,
    WorkspaceRecord,
)
from lucnhan.memory.workspace import new_memory_id


def _make_registry(tmp_path: Path) -> MemoryRegistry:
    """Create an initialized registry under *tmp_path*."""
    registry = MemoryRegistry(tmp_path / "registry.db")
    registry.initialize()
    return registry


class TestRegistryCrud:
    def test_create_workspace(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        mid = new_memory_id()
        record = registry.create_workspace("binary", "a.i64", memory_id=mid)

        assert isinstance(record, WorkspaceRecord)
        assert record.memory_id == mid
        assert record.kind == "binary"
        assert record.display_name == "a.i64"
        assert record.state == "active"

    def test_get_workspace(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        record = registry.create_workspace("binary", "a.i64")
        fetched = registry.get_workspace(record.memory_id)
        assert fetched is not None
        assert fetched.memory_id == record.memory_id

    def test_get_missing_workspace_returns_none(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        assert registry.get_workspace(new_memory_id()) is None


class TestEvidenceUniqueness:
    def test_current_filesystem_evidence_is_unique(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        first = registry.create_workspace("binary", "a.i64", memory_id=new_memory_id())
        second = registry.create_workspace("binary", "b.i64", memory_id=new_memory_id())

        registry.bind_evidence(first.memory_id, "filesystem", "vol:inode1", status="current")

        with pytest.raises(EvidenceConflictError):
            registry.bind_evidence(second.memory_id, "filesystem", "vol:inode1", status="current")

    def test_db_instance_evidence_may_coexist_for_copies(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        first = registry.create_workspace("binary", "a.i64")
        second = registry.create_workspace("binary", "a-copy.i64")

        registry.bind_evidence(first.memory_id, "db_instance", "same-uuid", status="current")
        registry.bind_evidence(second.memory_id, "db_instance", "same-uuid", status="current")

        results = {r.memory_id for r in registry.find_evidence("db_instance", "same-uuid")}
        assert results == {first.memory_id, second.memory_id}

    def test_raw_sha256_is_unique(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        digest = "a" * 64

        ws = registry.resolve_or_create_raw(digest, "sample.bin")
        assert ws is not None

        ws2 = registry.resolve_or_create_raw(digest, "sample.bin")
        assert ws2.memory_id == ws.memory_id

    def test_retire_evidence_allows_rebind(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        first = registry.create_workspace("binary", "a.i64", memory_id=new_memory_id())

        registry.bind_evidence(first.memory_id, "filesystem", "vol:inode1", status="current")
        registry.retire_evidence(first.memory_id, "filesystem", "vol:inode1")

        second = registry.create_workspace("binary", "b.i64", memory_id=new_memory_id())
        registry.bind_evidence(second.memory_id, "filesystem", "vol:inode1", status="current")


class TestPathAlias:
    def test_touch_path_alias(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        ws = registry.create_workspace("binary", "a.i64")

        registry.touch_path_alias(ws.memory_id, "/canonical/a.i64")
        results = registry.find_by_path("/canonical/a.i64")
        assert results == ws.memory_id


class TestMultiprocessFirstOpen:
    def test_two_processes_get_one_raw_workspace(self, tmp_path: Path) -> None:
        """Two processes resolving the same raw SHA must produce exactly one workspace."""

        ctx = mp.get_context("spawn")
        db_path = str(tmp_path / "registry.db")
        digest = "b" * 64

        # Initialize registry in parent before spawning children
        MemoryRegistry(db_path).initialize()

        q: mp.Queue = ctx.Queue()
        procs = [
            ctx.Process(target=_worker_resolve_raw, args=(db_path, digest, q)),
            ctx.Process(target=_worker_resolve_raw, args=(db_path, digest, q)),
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)

        ids = set()
        for _ in procs:
            ids.add(q.get(timeout=5))

        assert len(ids) == 1


def _worker_resolve_raw(db_path: str, digest: str, q: object) -> None:
    """Worker entry: resolve or create a raw workspace and return its ID."""
    registry = MemoryRegistry(db_path)
    ws = registry.resolve_or_create_raw(digest, "sample.bin")
    q.put(ws.memory_id)  # type: ignore[attr-defined]
