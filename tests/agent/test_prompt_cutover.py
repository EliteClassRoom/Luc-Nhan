"""Tests for agent system_prompt central memory cutover (prompt loading)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from lucnhan.agent.loop import AgentLoop
from lucnhan.core.config import LucNhanConfig
from lucnhan.memory.authority import MemoryAuthorityIssuer
from lucnhan.memory.markdown import MemoryProjector
from lucnhan.memory.repository import SQLiteKnowledgeRepository
from lucnhan.memory.service import BinaryMemoryService
from lucnhan.memory.workspace import MemoryLocator, MemoryRunContext, new_memory_id
from lucnhan.memory.workspace_store import WorkspaceStore
from lucnhan.state.session import SessionState


def _make_loop_with_central_memory(tmp_path: Path) -> tuple[AgentLoop, BinaryMemoryService]:
    """Build a minimal AgentLoop wired with central memory service."""
    memory_id = new_memory_id()
    context = MemoryRunContext(memory_id, "", 1, 0)
    paths = MemoryLocator(tmp_path / "memory").binary(memory_id)
    store = WorkspaceStore.create(paths, owner_memory_id=memory_id)
    repo = SQLiteKnowledgeRepository(store, owner_memory_id=memory_id)
    issuer = MemoryAuthorityIssuer()
    service = BinaryMemoryService(
        context=context,
        paths=paths,
        repository=repo,
        store=store,
        projector=MemoryProjector(),
        authority_issuer=issuer,
    )

    config = LucNhanConfig()
    session = SessionState(idb_path=str(tmp_path / "test.i64"))
    provider = MagicMock()
    tools = MagicMock()

    loop = AgentLoop(provider, tools, config, session)
    loop.memory_service = service
    loop._memory_authority = issuer.issue(context)
    return loop, service


class TestPromptSourceSeparation:
    """When central memory is wired, the system prompt reads from SQLite, not RIKUGAN.md."""

    def test_structured_context_loaded_from_sqlite(self, tmp_path: Path) -> None:
        loop, service = _make_loop_with_central_memory(tmp_path)

        # Save a fact to central memory
        service.save_fact(
            loop._memory_authority,
            category="algorithm",
            fact="Uses RC4 for C2",
            source="save_memory",
        )

        # structured_context should return the fact
        structured = service.structured_context()
        assert "Uses RC4 for C2" in structured
        assert "[algorithm]" in structured

    def test_manual_notes_context_excludes_managed_region(self, tmp_path: Path) -> None:
        loop, service = _make_loop_with_central_memory(tmp_path)

        service.save_fact(
            loop._memory_authority,
            category="protocol",
            fact="Uses HTTP",
            source="save_memory",
        )

        # Add user note to unmanaged region
        content = service.paths.markdown.read_text(encoding="utf-8")
        content += "\n## User Notes\n\nCheck key schedule manually.\n"
        service.paths.markdown.write_text(content, encoding="utf-8")

        manual = service.manual_notes_context()
        assert "Check key schedule manually" in manual
        assert "lucnhan:managed" not in manual
        assert "lucnhan:record" not in manual
        assert "Uses HTTP" not in manual  # managed content excluded

    def test_structured_context_excludes_manual_notes(self, tmp_path: Path) -> None:
        _loop, service = _make_loop_with_central_memory(tmp_path)

        # Add user note only (no facts saved)
        service.paths.markdown.parent.mkdir(parents=True, exist_ok=True)
        service.paths.markdown.write_text(
            "# Memory\n\n## My Notes\n\nThis is a manual note.\n",
            encoding="utf-8",
        )

        structured = service.structured_context()
        manual = service.manual_notes_context()

        assert "This is a manual note" not in structured
        assert "This is a manual note" in manual

    def test_empty_workspace_returns_empty_contexts(self, tmp_path: Path) -> None:
        _loop, service = _make_loop_with_central_memory(tmp_path)

        structured = service.structured_context()
        manual = service.manual_notes_context()

        assert structured == ""
        # MEMORY.md may or may not exist yet — empty is fine
        assert "lucnhan:managed" not in manual

    def test_build_system_prompt_uses_central_memory_when_wired(self, tmp_path: Path) -> None:
        """build_system_prompt should include structured_memory when provided."""
        from lucnhan.agent.system_prompt import build_system_prompt

        prompt = build_system_prompt(
            structured_memory="## Structured Memory\n- [fact] Test: hello",
            manual_memory_notes="## My Notes\nImportant note",
        )

        assert "hello" in prompt
        assert "Important note" in prompt
        # The legacy "## Persistent Memory (RIKUGAN.md)" section should NOT appear
        assert "## Persistent Memory (RIKUGAN.md)" not in prompt

    def test_build_system_prompt_no_memory_section_when_empty(self, tmp_path: Path) -> None:
        """Without structured_memory or manual_memory_notes, no memory section appears."""
        from lucnhan.agent.system_prompt import build_system_prompt

        prompt = build_system_prompt()
        # No memory content, but base prompt is present
        assert "## Persistent Memory (RIKUGAN.md)" not in prompt
        assert "## Structured Memory" not in prompt
