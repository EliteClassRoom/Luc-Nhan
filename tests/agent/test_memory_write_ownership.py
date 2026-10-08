"""Tests for subagent write ownership: subagents cannot persist to central memory."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lucnhan.agent.loop import AgentLoop
from lucnhan.core.config import LucNhanConfig
from lucnhan.core.types import ToolCall
from lucnhan.memory.authority import MemoryAuthorityIssuer, MemoryWriteDenied
from lucnhan.memory.markdown import MemoryProjector
from lucnhan.memory.repository import SQLiteKnowledgeRepository
from lucnhan.memory.service import BinaryMemoryService
from lucnhan.memory.workspace import MemoryLocator, MemoryRunContext, new_memory_id
from lucnhan.memory.workspace_store import WorkspaceStore
from lucnhan.state.session import SessionState


class TestSubagentNoMemoryService:
    """Subagent loops must never receive memory_service or _memory_authority."""

    def test_subagent_loop_has_no_memory_service(self) -> None:
        """A child AgentLoop created without explicit memory wiring has no persistence."""
        config = LucNhanConfig()
        session = SessionState()
        provider = MagicMock()
        tools = MagicMock()

        child = AgentLoop(provider, tools, config, session)
        assert child.memory_service is None
        assert child._memory_authority is None

    def test_subagent_save_memory_without_service_returns_error(self, tmp_path: Path) -> None:
        """When memory_service is None, subagent save_memory reports unavailable (no legacy fallback)."""
        config = LucNhanConfig()
        session = SessionState(idb_path=str(tmp_path / "child.i64"))
        provider = MagicMock()
        tools = MagicMock()

        child = AgentLoop(provider, tools, config, session)
        assert child.memory_service is None

        # save_memory must not crash and must NOT write a legacy RIKUGAN.md file
        tc = ToolCall(id="tc1", name="save_memory", arguments={"category": "test", "fact": "child note"})
        events = list(child._handle_save_memory_tool(tc))

        legacy_path = tmp_path / "RIKUGAN.md"
        assert not legacy_path.exists()
        tr = events[-1] if events else None
        assert tr is not None

    def test_subagent_cannot_use_parent_authority(self, tmp_path: Path) -> None:
        """Even if a parent authority is leaked, the child service check rejects it."""
        memory_id = new_memory_id()
        context = MemoryRunContext(memory_id, "", 1, 0)
        paths = MemoryLocator(tmp_path / "memory").binary(memory_id)
        store = WorkspaceStore.create(paths, owner_memory_id=memory_id)
        repo = SQLiteKnowledgeRepository(store, owner_memory_id=memory_id)
        issuer = MemoryAuthorityIssuer()
        BinaryMemoryService(
            context=context,
            paths=paths,
            repository=repo,
            store=store,
            projector=MemoryProjector(),
            authority_issuer=issuer,
        )

        # Parent authority bound to parent context
        parent_authority = issuer.issue(context)

        # A *different* context (simulating subagent binding change)
        child_context = MemoryRunContext(new_memory_id(), "", 2, 0)
        child_service = BinaryMemoryService(
            context=child_context,
            paths=paths,
            repository=repo,
            store=store,
            projector=MemoryProjector(),
            authority_issuer=issuer,
        )

        # Parent authority cannot write through child service
        with pytest.raises(MemoryWriteDenied):
            child_service.require_write_authority(parent_authority)
