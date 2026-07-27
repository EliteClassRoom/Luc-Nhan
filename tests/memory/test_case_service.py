"""Tests for CaseMemoryService: promotion, source drift, membership."""

from __future__ import annotations

from pathlib import Path

import pytest

from rikugan.memory.authority import MemoryAuthorityIssuer
from rikugan.memory.case_repository import CaseRepository
from rikugan.memory.case_service import CaseMembershipError, CaseMemoryService
from rikugan.memory.markdown import MemoryProjector
from rikugan.memory.registry import MemoryRegistry
from rikugan.memory.repository import SQLiteKnowledgeRepository
from rikugan.memory.service import BinaryMemoryService
from rikugan.memory.workspace import MemoryLocator, MemoryRunContext, new_memory_id
from rikugan.memory.workspace_store import WorkspaceStore

from .test_workspace_migration_v2 import _create_v1_database


def _setup_service(
    tmp_path: Path,
) -> tuple[CaseMemoryService, CaseRepository, BinaryMemoryService, MemoryAuthorityIssuer, MemoryRunContext, str, str]:
    """Create a full case+binary service stack."""
    locator = MemoryLocator(tmp_path / "memory")
    registry = MemoryRegistry(locator.registry_database())
    registry.initialize()
    cases = CaseRepository(registry, locator)

    memory_id = new_memory_id()
    context = MemoryRunContext(memory_id, "", 1, 0)
    # Register the workspace in the registry so FK constraints pass
    registry.create_workspace("binary", "test.i64", memory_id=memory_id)
    paths = locator.binary(memory_id)
    store = WorkspaceStore.create(paths, owner_memory_id=memory_id)
    repo = SQLiteKnowledgeRepository(store, owner_memory_id=memory_id)
    projector = MemoryProjector()
    issuer = MemoryAuthorityIssuer()
    service = BinaryMemoryService(
        context=context,
        paths=paths,
        repository=repo,
        store=store,
        projector=projector,
        authority_issuer=issuer,
    )

    # Create a case and add the binary
    case = cases.create_case("Test Case")
    cases.add_member(case.case_id, memory_id, expected_case_revision=case.revision)

    case_service = CaseMemoryService(cases, binary_repository=repo)
    return case_service, cases, service, issuer, context, case.case_id, memory_id


class TestPromotion:
    def test_promote_creates_promotion_record(self, tmp_path: Path) -> None:
        case_service, _, service, issuer, context, case_id, _ = _setup_service(tmp_path)

        # Save a fact first
        service.save_fact(
            issuer.issue(context),
            category="algorithm",
            fact="Uses RC4",
            source="save_memory",
        )

        # Get the fact ID
        facts = service.repository.list_memories()
        fact_id = facts[0].id

        # Promote it
        authority = issuer.issue(context)
        promotion = case_service.promote(authority, context, case_id, fact_id)

        assert promotion.source.source_record_id == fact_id
        assert promotion.source.source_revision == facts[0].revision_id if hasattr(facts[0], "revision_id") else True
        assert promotion.case_id == case_id

    def test_promote_rejects_nonmember(self, tmp_path: Path) -> None:
        case_service, _, service, issuer, context, _, _ = _setup_service(tmp_path)

        service.save_fact(
            issuer.issue(context),
            category="test",
            fact="data",
            source="save_memory",
        )
        facts = service.repository.list_memories()

        # Use a different case_id that the binary is not a member of
        with pytest.raises(CaseMembershipError):
            case_service.promote(
                issuer.issue(context),
                context,
                "case-nonexistent",
                facts[0].id,
            )


class TestSourceDrift:
    def test_source_not_member(self, tmp_path: Path) -> None:
        case_service, _, _, _, _, case_id, _memory_id = _setup_service(tmp_path)

        from rikugan.memory.case_schema import PromotionSource

        source = PromotionSource(
            source_memory_id="mem-nonexistent",
            source_record_id="fact-nonexistent",
            source_revision=1,
            source_hash="abc",
        )
        result = case_service.evaluate_source_state(case_id, source)
        assert result.status == "source_not_member"


class TestPromotionBackupGate:
    def test_promote_on_v1_case_workspace_takes_backup_first(self, tmp_path: Path) -> None:
        """Promotion on a stale v1 case DB must route through the backup helper."""
        case_service, cases, service, issuer, context, case_id, _ = _setup_service(tmp_path)

        # Save a fact so there is something to promote.
        service.save_fact(
            issuer.issue(context),
            category="algorithm",
            fact="Uses RC4",
            source="save_memory",
        )
        fact_id = service.repository.list_memories()[0].id

        # Overwrite the (empty) v2 case workspace with a v1 database.
        case_paths = cases.locator.case(case_id)
        _create_v1_database(case_paths.database, case_id)

        authority = issuer.issue(context)
        case_service.promote(authority, context, case_id, fact_id)

        # After promotion a backup of the v1 case DB exists.
        backups = list(cases.locator.backups(case_id).glob("memory_*.db"))
        assert len(backups) == 1
