"""Tests for MEMORY.md managed-region parser, deterministic render, and locking."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from rikugan.memory.markdown import (
    ManagedEntry,
    ManagedRegionError,
    MemoryProjector,
    parse_memory_document,
    render_memory_document,
)
from rikugan.memory.repository import SQLiteKnowledgeRepository
from rikugan.memory.schema import KnowledgeMemory
from rikugan.memory.workspace import MemoryLocator, new_memory_id, new_record_id
from rikugan.memory.workspace_store import WorkspaceStore


class TestParseMemoryDocument:
    def test_empty_document_has_no_managed_region(self) -> None:
        content = "# Memory\n\nSome user notes.\n"
        doc = parse_memory_document(content)

        assert doc.managed == ""
        assert doc.prefix == content
        assert doc.suffix == ""
        assert doc.managed_hash != ""

    def test_well_formed_managed_region(self) -> None:
        content = (
            "# Memory\n\n"
            "<!-- rikugan:managed:start -->\n"
            "## Confirmed Facts\n\n"
            "- [protocol] Uses RC4.\n"
            "<!-- rikugan:managed:end -->\n\n"
            "## User Notes\n\n"
            "Check key schedule.\n"
        )
        doc = parse_memory_document(content)

        assert "Uses RC4" in doc.managed
        assert "Check key schedule" in doc.suffix
        assert "rikugan:managed:start" not in doc.managed

    def test_nested_or_reversed_markers_are_conflicts(self) -> None:
        # Reversed order
        content = "<!-- rikugan:managed:end -->\n<!-- rikugan:managed:start -->\n"
        with pytest.raises(ManagedRegionError):
            parse_memory_document(content)

    def test_missing_end_marker_is_conflict(self) -> None:
        content = "<!-- rikugan:managed:start -->\nSome content\n"
        with pytest.raises(ManagedRegionError):
            parse_memory_document(content)

    def test_missing_start_marker_is_conflict(self) -> None:
        content = "Some content\n<!-- rikugan:managed:end -->\n"
        with pytest.raises(ManagedRegionError):
            parse_memory_document(content)

    def test_double_start_is_conflict(self) -> None:
        content = "<!-- rikugan:managed:start -->\n<!-- rikugan:managed:start -->\n<!-- rikugan:managed:end -->\n"
        with pytest.raises(ManagedRegionError):
            parse_memory_document(content)


class TestRenderMemoryDocument:
    def test_render_preserves_unmanaged_text(self) -> None:
        original = "# Memory\n\n## User Notes\n\nImportant note.\n"
        doc = parse_memory_document(original)
        rendered = render_memory_document(doc, managed_block="## Facts\n\n- fact1\n")

        assert "Important note." in rendered
        assert "fact1" in rendered
        assert rendered.count("<!-- rikugan:managed:start -->") == 1
        assert rendered.count("<!-- rikugan:managed:end -->") == 1

    def test_render_empty_managed_creates_section(self) -> None:
        doc = parse_memory_document("# Memory\n\nUser note.\n")
        rendered = render_memory_document(doc, managed_block="## Facts\n\n- A\n")

        assert "<!-- rikugan:managed:start -->" in rendered
        assert "<!-- rikugan:managed:end -->" in rendered

    def test_render_includes_record_markers(self) -> None:
        """Managed entries carry hidden stable record ID/revision markers."""

        doc = parse_memory_document("# Memory\n")
        entries = [
            ManagedEntry(
                fact_id="fact-aaa",
                fact_type="protocol",
                title="RC4",
                content="Uses RC4",
                revision=3,
            )
        ]
        rendered = render_memory_document(doc, managed_block="", entries=entries)

        assert "rikugan:record" in rendered
        assert "fact-aaa" in rendered
        assert "rev=3" in rendered


class TestMemoryProjector:
    def test_project_creates_markdown_from_facts(self, tmp_path: Path) -> None:
        memory_id = new_memory_id()
        paths = MemoryLocator(tmp_path).binary(memory_id)
        store = WorkspaceStore.create(paths, owner_memory_id=memory_id)

        fid = new_record_id("fact")
        store.put_fact(fid, "algorithm", "RC4", "Uses RC4 for C2", 0.8, expected_revision=0)

        projector = MemoryProjector()
        projector.project(paths, store)

        content = paths.markdown.read_text(encoding="utf-8")
        assert "Uses RC4 for C2" in content
        assert content.count("<!-- rikugan:managed:start -->") == 1
        assert content.count("<!-- rikugan:managed:end -->") == 1

        state = store.projection_state()
        assert state.projection_dirty is False
        assert state.managed_hash != ""
        store.close()

    def test_project_preserves_unmanaged_edits(self, tmp_path: Path) -> None:
        memory_id = new_memory_id()
        paths = MemoryLocator(tmp_path).binary(memory_id)
        store = WorkspaceStore.create(paths, owner_memory_id=memory_id)

        fid = new_record_id("fact")
        store.put_fact(fid, "algorithm", "RC4", "Uses RC4", 0.8, expected_revision=0)

        # First projection
        projector = MemoryProjector()
        projector.project(paths, store)

        # Add user note to the unmanaged region
        content = paths.markdown.read_text(encoding="utf-8")
        content += "\n## User Notes\n\nCheck key schedule.\n"
        paths.markdown.write_text(content, encoding="utf-8")

        # Re-project
        projector.project(paths, store)

        content = paths.markdown.read_text(encoding="utf-8")
        assert "Check key schedule" in content
        assert "Uses RC4" in content
        store.close()

    def test_project_overwrites_stale_managed_region(self, tmp_path: Path) -> None:
        """Stale managed content in MEMORY.md is always overwritten from SQLite."""
        memory_id = new_memory_id()
        paths = MemoryLocator(tmp_path).binary(memory_id)
        store = WorkspaceStore.create(paths, owner_memory_id=memory_id)

        fid = new_record_id("fact")
        store.put_fact(fid, "algorithm", "RC4", "Uses RC4", 0.8, expected_revision=0)

        projector = MemoryProjector()

        # Write an initial file with stale managed content
        paths.markdown.parent.mkdir(parents=True, exist_ok=True)
        paths.markdown.write_text(
            "# Memory\n\n<!-- rikugan:managed:start -->\n## OLD\n\n- old content\n<!-- rikugan:managed:end -->\n",
            encoding="utf-8",
        )

        projector.project(paths, store)

        content = paths.markdown.read_text(encoding="utf-8")
        assert "old content" not in content
        assert "Uses RC4" in content

        state = store.projection_state()
        assert state.projection_dirty is False
        store.close()

    def test_project_works_when_portalocker_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A user with portalocker not installed must not see
        ``ModuleNotFoundError`` during projection. The projector
        falls back to a process-local lock and still produces the
        expected MEMORY.md contents.
        """
        # Simulate portalocker being absent.
        import builtins as _bi
        orig_import = _bi.__import__

        def _fake_import(name, *args, **kwargs):
            if name.startswith("portalocker"):
                raise ModuleNotFoundError(f"No module named '{name}'")
            return orig_import(name, *args, **kwargs)

        monkeypatch.setattr(_bi, "__import__", _fake_import)
        # Reload the module so the projector re-probes portalocker.
        import importlib
        from rikugan.memory import markdown as _md
        importlib.reload(_md)
        try:
            memory_id = new_memory_id()
            paths = MemoryLocator(tmp_path).binary(memory_id)
            store = WorkspaceStore.create(
                paths, owner_memory_id=memory_id
            )
            fid = new_record_id("fact")
            store.put_fact(
                fid, "algorithm", "RC4", "Uses RC4 for C2", 0.8, expected_revision=0
            )

            projector = _md.MemoryProjector()
            # Sanity: fallback path was selected.
            assert projector._lock_module is None

            projector.project(paths, store)
            content = paths.markdown.read_text(encoding="utf-8")
            assert "Uses RC4 for C2" in content
            store.close()
        finally:
            importlib.reload(_md)

    def test_lock_contention_marks_dirty_and_raises(self, tmp_path: Path) -> None:
        """Cross-process lock contention must mark the projection dirty
        and raise instead of silently degrading to an in-process lock."""
        memory_id = new_memory_id()
        paths = MemoryLocator(tmp_path).binary(memory_id)
        store = WorkspaceStore.create(paths, owner_memory_id=memory_id)
        fid = new_record_id("fact")
        store.put_fact(fid, "algorithm", "RC4", "Uses RC4", 0.8, expected_revision=0)

        class _LockError(Exception):
            pass

        class _FakePortalocker:
            class exceptions:
                LockError = _LockError

            @staticmethod
            def Lock(*_args, **_kwargs):
                raise _LockError("locked by another process")

        projector = MemoryProjector()
        projector._lock_module = _FakePortalocker
        projector._lock_exc_type = _LockError
        with pytest.raises(_LockError):
            projector.project(paths, store)
        assert store.projection_state().projection_dirty is True
        store.close()

    def test_project_creates_markdown_for_empty_store(self, tmp_path: Path) -> None:
        memory_id = new_memory_id()
        paths = MemoryLocator(tmp_path).binary(memory_id)
        store = WorkspaceStore.create(paths, owner_memory_id=memory_id)

        projector = MemoryProjector()
        projector.project(paths, store)

        content = paths.markdown.read_text(encoding="utf-8")
        assert "# Memory" in content
        store.close()

    def test_projection_preserves_multiple_same_category_facts_in_existing_order(self, tmp_path: Path) -> None:
        """Two independent ``function_purpose`` facts both appear in the projection."""
        memory_id = new_memory_id()
        paths = MemoryLocator(tmp_path).binary(memory_id)
        store = WorkspaceStore.create(paths, owner_memory_id=memory_id)
        repo = SQLiteKnowledgeRepository(store, owner_memory_id=memory_id)

        first_id = new_record_id("fact")
        second_id = new_record_id("fact")
        first_content = "Initializes the RC4 key schedule"
        second_content = "XORs the plaintext against the keystream"
        first_title = "RC4 init"
        second_title = "RC4 crypt"
        repo.upsert_memory(
            KnowledgeMemory(
                id=first_id,
                binary_id=memory_id,
                type="function_purpose",
                title=first_title,
                content=first_content,
                confidence=0.7,
            )
        )
        repo.upsert_memory(
            KnowledgeMemory(
                id=second_id,
                binary_id=memory_id,
                type="function_purpose",
                title=second_title,
                content=second_content,
                confidence=0.7,
            )
        )

        projector = MemoryProjector()
        projector.project(paths, store)

        content = paths.markdown.read_text(encoding="utf-8")
        # Each fact ID and content must appear exactly once in the projection.
        assert content.count(first_id) == 1
        assert content.count(second_id) == 1
        assert content.count(first_content) == 1
        assert content.count(second_content) == 1

        # Parse managed entries and assert deterministic sort uses
        # (fact_type, title, fact_id). The two facts share fact_type, so
        # the sort resolves on title: "RC4 crypt" < "RC4 init".
        doc = parse_memory_document(content)
        record_re = re.compile(r"<!-- rikugan:record id=([A-Za-z0-9._:-]+) rev=([1-9][0-9]*) -->")
        fact_ids = [match.group(1) for match in record_re.finditer(doc.managed)]
        assert len(fact_ids) == 2
        # The two facts share fact_type, so the title sort is the active key:
        # ``RC4 crypt`` < ``RC4 init`` → second_id appears before first_id.
        assert fact_ids == [second_id, first_id]
        # And the order is NOT the insertion order (which would put
        # first_id before second_id, since the IDs are random UUIDs).
        assert fact_ids != [first_id, second_id]
        store.close()
