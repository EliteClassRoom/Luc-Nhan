"""Tests for BinaryMemoryService: prompt source separation, save, projection."""

from __future__ import annotations

from pathlib import Path

import pytest

from rikugan.memory.authority import MemoryAuthorityIssuer, MemoryWriteDenied
from rikugan.memory.markdown import MemoryProjector
from rikugan.memory.repository import SQLiteKnowledgeRepository
from rikugan.memory.service import BinaryMemoryService, SaveMemoryResult, StaleMemoryContext
from rikugan.memory.workspace import MemoryLocator, MemoryRunContext, new_memory_id
from rikugan.memory.workspace_store import WorkspaceStore


def _create_service(tmp_path: Path) -> tuple[BinaryMemoryService, MemoryAuthorityIssuer, MemoryRunContext]:
    """Create a service with store, repo, projector, and authority."""
    memory_id = new_memory_id()
    context = MemoryRunContext(memory_id, "", 1, 0)
    paths = MemoryLocator(tmp_path).binary(memory_id)
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
    return service, issuer, context


class TestPromptSourceSeparation:
    def test_structured_context_contains_sqlite_facts_not_markdown(self, tmp_path: Path) -> None:
        service, issuer, context = _create_service(tmp_path)
        service.save_fact(
            issuer.issue(context),
            category="algorithm",
            fact="Uses RC4",
            source="save_memory",
        )

        # Add user note to unmanaged region
        content = service.paths.markdown.read_text(encoding="utf-8")
        content += "\n## User Notes\n\nCheck key schedule.\n"
        service.paths.markdown.write_text(content, encoding="utf-8")

        structured = service.structured_context(query="RC4")
        manual = service.manual_notes_context()

        assert "Uses RC4" in structured
        assert "Check key schedule" not in structured
        assert "Check key schedule" in manual
        assert "rikugan:record" not in manual
        assert "rikugan:managed" not in manual

    def test_empty_store_returns_empty_contexts(self, tmp_path: Path) -> None:
        service, _, _ = _create_service(tmp_path)
        structured = service.structured_context(query="nothing")
        manual = service.manual_notes_context()

        assert "rikugan:managed" not in structured
        assert manual.strip() == ""


class TestSaveFact:
    def test_save_fact_creates_fact_and_projects(self, tmp_path: Path) -> None:
        service, issuer, context = _create_service(tmp_path)
        result = service.save_fact(
            issuer.issue(context),
            category="protocol",
            fact="Uses HTTP",
            source="save_memory",
        )

        assert isinstance(result, SaveMemoryResult)
        assert result.projection_dirty is False
        assert result.warning == ""

        # Verify SQLite has the fact
        facts = service.repository.list_memories()
        assert len(facts) == 1
        assert facts[0].content == "Uses HTTP"
        assert facts[0].type == "protocol"

        # Verify MEMORY.md has the managed projection
        md = service.paths.markdown.read_text(encoding="utf-8")
        assert "Uses HTTP" in md
        assert "rikugan:managed:start" in md

    def test_save_fact_without_authority_raises(self, tmp_path: Path) -> None:
        service, _, _ = _create_service(tmp_path)
        with pytest.raises(MemoryWriteDenied):
            service.save_fact(
                None,  # type: ignore[arg-type]
                category="protocol",
                fact="test",
                source="save_memory",
            )

    def test_save_fact_with_wrong_authority_raises(self, tmp_path: Path) -> None:
        service, issuer, _ = _create_service(tmp_path)
        wrong_context = MemoryRunContext(new_memory_id(), "", 2, 0)
        wrong_authority = issuer.issue(wrong_context)
        with pytest.raises(MemoryWriteDenied):
            service.save_fact(
                wrong_authority,
                category="protocol",
                fact="test",
                source="save_memory",
            )

    def test_save_fact_empty_category_rejected(self, tmp_path: Path) -> None:
        service, issuer, context = _create_service(tmp_path)
        with pytest.raises(ValueError):
            service.save_fact(
                issuer.issue(context),
                category="",
                fact="test",
                source="save_memory",
            )

    def test_save_fact_reports_created_then_deduplicated(self, tmp_path: Path) -> None:
        service, issuer, context = _create_service(tmp_path)
        authority = issuer.issue(context)
        created = service.save_fact(
            authority,
            category="protocol",
            fact="Uses HTTP",
            source="save_memory",
        )
        duplicate = service.save_fact(
            authority,
            category=" Protocol ",
            fact="Uses HTTP\r\n",
            source="save_memory",
        )
        assert created.outcome == "created"
        assert duplicate.outcome == "deduplicated"
        assert duplicate.record_id == created.record_id
        assert len(service.repository.list_memories()) == 1


class TestSavePlan:
    def test_save_plan_creates_structured_fact(self, tmp_path: Path) -> None:
        service, issuer, context = _create_service(tmp_path)
        result = service.save_plan(
            issuer.issue(context),
            goal="Identify C2 protocol",
            steps=["Decompile main", "Trace network calls"],
        )

        assert result.projection_dirty is False
        facts = service.repository.list_memories()
        assert any("Identify C2 protocol" in f.title or "Identify C2 protocol" in f.content for f in facts)


class TestContextValidation:
    def test_stale_context_rejected_on_save(self, tmp_path: Path) -> None:
        service, issuer, _ = _create_service(tmp_path)
        stale_context = MemoryRunContext(new_memory_id(), "", 99, 0)
        authority = issuer.issue(stale_context)
        with pytest.raises((MemoryWriteDenied, StaleMemoryContext)):
            service.save_fact(
                authority,
                category="test",
                fact="x",
                source="test",
            )
