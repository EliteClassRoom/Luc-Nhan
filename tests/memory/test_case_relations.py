"""Tests for case relations: creation, listing, membership enforcement."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from rikugan.memory.case_repository import CaseRepository
from rikugan.memory.case_schema import (
    CaseRelationType,
)
from rikugan.memory.registry import MemoryRegistry
from rikugan.memory.sqlite_backend import SchemaMigrationRequired
from rikugan.memory.workspace import MemoryLocator

from .test_workspace_migration_v2 import _create_v1_database


def _setup(tmp_path: Path) -> tuple[CaseRepository, MemoryRegistry, str, str]:
    """Create registry + case + two member binaries."""
    locator = MemoryLocator(tmp_path / "memory")
    registry = MemoryRegistry(locator.registry_database())
    registry.initialize()
    cases = CaseRepository(registry, locator)
    binary_a = registry.create_workspace("binary", "loader.exe")
    binary_b = registry.create_workspace("binary", "payload.dll")
    case = cases.create_case("Malware Campaign")
    cases.add_member(case.case_id, binary_a.memory_id, expected_case_revision=case.revision)
    current = cases.get_case(case.case_id)
    cases.add_member(case.case_id, binary_b.memory_id, expected_case_revision=current.revision)
    return cases, registry, binary_a.memory_id, binary_b.memory_id


class TestCaseRelations:
    def test_put_directed_relation(self, tmp_path: Path) -> None:
        cases, _, mid_a, mid_b = _setup(tmp_path)
        case = cases.list_cases()[0]

        rel = cases.put_case_relation(
            case.case_id,
            mid_a,
            CaseRelationType.EMBEDS_OR_LOADS,
            mid_b,
            confidence=0.9,
        )
        assert rel.predicate is CaseRelationType.EMBEDS_OR_LOADS
        assert rel.subject_memory_id == mid_a
        assert rel.object_memory_id == mid_b

    def test_put_symmetric_relation_canonicalized(self, tmp_path: Path) -> None:
        cases, _, mid_a, mid_b = _setup(tmp_path)
        case = cases.list_cases()[0]

        # Pass in reverse order — symmetric predicates sort endpoints
        rel = cases.put_case_relation(
            case.case_id,
            mid_b,
            CaseRelationType.COMMUNICATES_WITH,
            mid_a,
            confidence=0.8,
        )
        # Endpoints are canonicalized (sorted) regardless of input order
        assert {rel.subject_memory_id, rel.object_memory_id} == {mid_a, mid_b}

    def test_self_relation_rejected(self, tmp_path: Path) -> None:
        cases, _, mid_a, _ = _setup(tmp_path)
        case = cases.list_cases()[0]
        with pytest.raises(ValueError, match="self"):
            cases.put_case_relation(
                case.case_id,
                mid_a,
                CaseRelationType.COMMUNICATES_WITH,
                mid_a,
            )

    def test_shares_artifact_requires_ref(self, tmp_path: Path) -> None:
        cases, _, mid_a, mid_b = _setup(tmp_path)
        case = cases.list_cases()[0]
        with pytest.raises(ValueError, match="artifact"):
            cases.put_case_relation(
                case.case_id,
                mid_a,
                CaseRelationType.SHARES_ARTIFACT_WITH,
                mid_b,
            )

    def test_shares_artifact_succeeds_with_ref(self, tmp_path: Path) -> None:
        cases, _, mid_a, mid_b = _setup(tmp_path)
        case = cases.list_cases()[0]
        rel = cases.put_case_relation(
            case.case_id,
            mid_a,
            CaseRelationType.SHARES_ARTIFACT_WITH,
            mid_b,
            artifact_ref="hash:abc123",
        )
        assert rel.artifact_ref == "hash:abc123"

    def test_list_relations(self, tmp_path: Path) -> None:
        cases, _, mid_a, mid_b = _setup(tmp_path)
        case = cases.list_cases()[0]
        cases.put_case_relation(
            case.case_id,
            mid_a,
            CaseRelationType.DERIVED_FROM,
            mid_b,
            confidence=0.7,
        )
        cases.put_case_relation(
            case.case_id,
            mid_a,
            CaseRelationType.COMMUNICATES_WITH,
            mid_b,
            confidence=0.6,
        )
        rels = cases.list_case_relations(case.case_id)
        assert len(rels) == 2

    def test_nonmember_endpoint_rejected(self, tmp_path: Path) -> None:
        cases, registry, mid_a, _ = _setup(tmp_path)
        case = cases.list_cases()[0]
        outsider = registry.create_workspace("binary", "unrelated.exe")
        with pytest.raises(ValueError, match="member"):
            cases.put_case_relation(
                case.case_id,
                mid_a,
                CaseRelationType.COMMUNICATES_WITH,
                outsider.memory_id,
            )

    def test_put_relation_routes_through_backup_aware_open(self, tmp_path: Path) -> None:
        """put_case_relation on an existing case DB must use open_workspace_for_write."""
        from rikugan.memory import workspace_open
        from rikugan.memory.workspace_store import WorkspaceStore

        cases, _, mid_a, mid_b = _setup(tmp_path)
        case = cases.list_cases()[0]
        case_paths = cases.locator.case(case.case_id)

        # Seed a v2 case workspace so we hit the existing-DB branch.
        WorkspaceStore.create(case_paths, owner_memory_id=case.case_id, workspace_kind="case").close()

        calls: list[tuple[object, str, object]] = []
        real_open = workspace_open.open_workspace_for_write

        def tracking_open(paths_arg, owner_arg, backup_dir_arg):
            calls.append((paths_arg, owner_arg, backup_dir_arg))
            return real_open(paths_arg, owner_arg, backup_dir_arg)

        with patch.object(workspace_open, "open_workspace_for_write", tracking_open):
            cases.put_case_relation(
                case.case_id,
                mid_a,
                CaseRelationType.EMBEDS_OR_LOADS,
                mid_b,
                confidence=0.9,
            )

        assert len(calls) == 1
        assert calls[0][1] == case.case_id


def test_list_relations_does_not_migrate_stale_workspace(tmp_path: Path) -> None:
    """Reading case relations on a stale v1 DB must not silently migrate."""
    cases, _registry, _mid_a, _mid_b = _setup(tmp_path)
    case = cases.list_cases()[0]
    case_paths = cases.locator.case(case.case_id)
    _create_v1_database(case_paths.database, case.case_id)
    with pytest.raises(SchemaMigrationRequired):
        cases.list_case_relations(case.case_id)
    with sqlite3.connect(case_paths.database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
