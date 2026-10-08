"""Tests for workspace identity contracts, generated IDs, and locator paths."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from lucnhan.memory.workspace import (
    FilesystemIdentity,
    IdentityRequest,
    MemoryLocator,
    MemoryRunContext,
    WorkspaceBinding,
    new_case_id,
    new_memory_id,
    new_record_id,
    validate_case_id,
    validate_memory_id,
    validate_record_id,
)

# ---------------------------------------------------------------------------
# Generated IDs
# ---------------------------------------------------------------------------


class TestGeneratedIds:
    def test_memory_id_format(self) -> None:
        mid = new_memory_id()
        assert re.fullmatch(r"mem-[0-9a-f]{32}", mid)
        assert validate_memory_id(mid) == mid

    def test_case_id_format(self) -> None:
        cid = new_case_id()
        assert re.fullmatch(r"case-[0-9a-f]{32}", cid)
        assert validate_case_id(cid) == cid

    def test_record_id_format_and_kind_prefix(self) -> None:
        fid = new_record_id("fact")
        assert re.fullmatch(r"fact-[0-9a-f]{32}", fid)
        assert validate_record_id("fact", fid) == fid

    def test_invalid_record_kind_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid record kind"):
            new_record_id("bogus")

    def test_deterministic_id_rejected_as_record_id(self) -> None:
        with pytest.raises(ValueError):
            validate_record_id("fact", "func:0x401000")

    def test_invalid_memory_id_cannot_become_path_component(self) -> None:
        with pytest.raises(ValueError, match="memory_id"):
            validate_memory_id("../../outside")

    def test_invalid_case_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="case_id"):
            validate_case_id("../escape")

    def test_two_ids_are_distinct(self) -> None:
        assert new_memory_id() != new_memory_id()
        assert new_case_id() != new_case_id()
        assert new_record_id("fact") != new_record_id("fact")


# ---------------------------------------------------------------------------
# Frozen dataclasses
# ---------------------------------------------------------------------------


class TestFrozenContracts:
    def test_filesystem_identity_evidence_value(self) -> None:
        fs = FilesystemIdentity("vol123", "inode456")
        assert fs.evidence_value == "vol123:inode456"

    def test_identity_request_defaults(self) -> None:
        req = IdentityRequest(source_kind="idb", idb_path="/tmp/a.i64")
        assert req.db_instance_id == ""
        assert req.source_sha256 == ""
        assert req.display_name == ""
        assert req.filesystem_identity is None

    def test_workspace_binding_states(self) -> None:
        binding = WorkspaceBinding(
            memory_id="mem-" + "a" * 32,
            state="active",
            display_name="a.i64",
        )
        assert binding.uuid_write_pending is False
        assert binding.warning == ""

    def test_run_context_is_immutable(self) -> None:
        context = MemoryRunContext(
            binary_memory_id=new_memory_id(),
            active_case_id=new_case_id(),
            database_generation=4,
            case_binding_generation=2,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            context.database_generation = 5  # type: ignore[misc]


# ---------------------------------------------------------------------------
# MemoryLocator
# ---------------------------------------------------------------------------


class TestMemoryLocator:
    def test_binary_locator_never_uses_display_name(self, tmp_path: Path) -> None:
        memory_id = new_memory_id()
        paths = MemoryLocator(tmp_path).binary(memory_id)

        assert paths.root == tmp_path / "binaries" / memory_id
        assert paths.database == paths.root / "memory.db"
        assert paths.markdown == paths.root / "MEMORY.md"
        assert paths.reports == paths.root / "notes" / "reports"
        assert paths.lock == paths.root / ".workspace.lock"

    def test_case_locator_uses_case_id(self, tmp_path: Path) -> None:
        case_id = new_case_id()
        paths = MemoryLocator(tmp_path).case(case_id)

        assert paths.root == tmp_path / "cases" / case_id
        assert paths.database == paths.root / "memory.db"
        assert paths.markdown == paths.root / "MEMORY.md"

    def test_registry_database_path(self, tmp_path: Path) -> None:
        locator = MemoryLocator(tmp_path)
        assert locator.registry_database() == tmp_path / "registry.db"

    def test_invalid_memory_id_rejected_by_locator(self, tmp_path: Path) -> None:
        locator = MemoryLocator(tmp_path)
        with pytest.raises(ValueError):
            locator.binary("../escape")

    def test_invalid_case_id_rejected_by_locator(self, tmp_path: Path) -> None:
        locator = MemoryLocator(tmp_path)
        with pytest.raises(ValueError):
            locator.case("../escape")
