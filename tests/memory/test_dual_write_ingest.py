"""Tests for the dual-write behavior of ingest_exploration_finding.

These tests verify that ``lucnhan.memory.ingest.ingest_exploration_finding``
honors the ``_LEGACY_JSONL_DUAL_WRITE`` module flag and the optional
``memory_service`` keyword argument:

- When the flag is ``True`` and ``memory_service`` is supplied, the
  function writes to **both** the SQLite service and the JSONL raw store.
- When the flag is ``False`` and ``memory_service`` is supplied, the
  function writes **only** to the SQLite service.
- A failure in one write path never blocks the other.
- When ``memory_service`` is ``None``, the function falls back to the
  legacy JSONL-only behavior regardless of the flag (this is the path
  exercised by ``tests/knowledge/test_ingest.py``).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from lucnhan.memory import ingest
from lucnhan.memory.paths import KnowledgePaths, derive_binary_id
from lucnhan.memory.raw_store import KnowledgeRawStore


def _make_paths(tmp_path: Path) -> KnowledgePaths:
    return KnowledgePaths(
        idb_path=str(tmp_path / "test.i64"),
        notes_dir=str(tmp_path / "notes"),
        kb_dir=str(tmp_path / "kb"),
        reports_dir=str(tmp_path / "notes" / "reports"),
        binary_id=derive_binary_id(str(tmp_path / "test.i64")),
    )


def test_dual_write_flag_on_writes_both_stores(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ingest, "_LEGACY_JSONL_DUAL_WRITE", True)
    paths = _make_paths(tmp_path)
    paths.ensure()
    store = KnowledgeRawStore(paths)

    memory_service = MagicMock()
    memory_service.save_exploration_finding.return_value = MagicMock(outcome="created")

    ingest.ingest_exploration_finding(
        store,
        paths,
        category="function_purpose",
        summary="main parses config",
        address=0x401000,
        relevance="high",
        memory_service=memory_service,
    )
    memory_service.save_exploration_finding.assert_called_once()
    assert len(store.list_memories()) == 1


def test_dual_write_flag_off_skips_jsonl(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ingest, "_LEGACY_JSONL_DUAL_WRITE", False)
    paths = _make_paths(tmp_path)
    paths.ensure()
    store = KnowledgeRawStore(paths)

    memory_service = MagicMock()
    memory_service.save_exploration_finding.return_value = MagicMock(outcome="created")

    ingest.ingest_exploration_finding(
        store,
        paths,
        category="function_purpose",
        summary="main parses config",
        address=0x401000,
        relevance="high",
        memory_service=memory_service,
    )
    memory_service.save_exploration_finding.assert_called_once()
    assert len(store.list_memories()) == 0


def test_sqlite_failure_does_not_block_jsonl(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ingest, "_LEGACY_JSONL_DUAL_WRITE", True)
    paths = _make_paths(tmp_path)
    paths.ensure()
    store = KnowledgeRawStore(paths)

    memory_service = MagicMock()
    memory_service.save_exploration_finding.side_effect = RuntimeError("sqlite boom")

    ingest.ingest_exploration_finding(
        store,
        paths,
        category="function_purpose",
        summary="main parses config",
        address=0x401000,
        relevance="high",
        memory_service=memory_service,
    )
    assert len(store.list_memories()) == 1


def test_jsonl_failure_does_not_block_sqlite(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ingest, "_LEGACY_JSONL_DUAL_WRITE", True)
    paths = _make_paths(tmp_path)
    paths.ensure()
    store = KnowledgeRawStore(paths)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("jsonl boom")

    monkeypatch.setattr(store, "upsert_memory", _boom)

    memory_service = MagicMock()
    memory_service.save_exploration_finding.return_value = MagicMock(outcome="created")

    ingest.ingest_exploration_finding(
        store,
        paths,
        category="function_purpose",
        summary="main parses config",
        address=0x401000,
        relevance="high",
        memory_service=memory_service,
    )
    memory_service.save_exploration_finding.assert_called_once()


def test_no_memory_service_skips_sqlite_path(tmp_path: Path, monkeypatch) -> None:
    """Without ``memory_service`` the function must not call any SQLite path."""
    monkeypatch.setattr(ingest, "_LEGACY_JSONL_DUAL_WRITE", True)
    paths = _make_paths(tmp_path)
    paths.ensure()
    store = KnowledgeRawStore(paths)

    ingest.ingest_exploration_finding(
        store,
        paths,
        category="function_purpose",
        summary="main parses config",
        address=0x401000,
        relevance="high",
    )
    # Pure JSONL write — at least one memory must land.
    assert len(store.list_memories()) == 1


def _boom(message: str):
    def _raise(*_args, **_kwargs):
        raise RuntimeError(message)

    return _raise


def test_make_store_logs_path_build_failure(tmp_path: Path) -> None:
    from unittest.mock import patch

    with (
        patch("lucnhan.memory.paths.knowledge_paths", side_effect=OSError("bad path")),
        patch("lucnhan.memory.ingest.log_error") as log_error_mock,
    ):
        store, paths = ingest.make_store(str(tmp_path / "x.i64"))

    assert store is None and paths is None
    assert log_error_mock.called


def test_make_store_never_raises_on_unexpected_error(tmp_path: Path) -> None:
    from unittest.mock import patch

    with (
        patch("lucnhan.memory.paths.knowledge_paths", side_effect=KeyError("odd")),
        patch("lucnhan.memory.ingest.log_error"),
    ):
        assert ingest.make_store(str(tmp_path / "x.i64")) == (None, None)


def test_ingest_save_memory_logs_jsonl_failure(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import patch

    paths = _make_paths(tmp_path)
    paths.ensure()
    store = KnowledgeRawStore(paths)
    monkeypatch.setattr(store, "upsert_memory", _boom("jsonl boom"))

    with patch("lucnhan.memory.ingest.log_error") as log_error_mock:
        ingest.ingest_save_memory(store, paths, fact="uses RC4", category="crypto")

    assert log_error_mock.called


def test_exploration_entity_write_failure_is_logged(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import patch

    paths = _make_paths(tmp_path)
    paths.ensure()
    store = KnowledgeRawStore(paths)
    monkeypatch.setattr(store, "upsert_entity", _boom("entity boom"))

    with patch("lucnhan.memory.ingest.log_error") as log_error_mock:
        ingest._write_exploration_to_jsonl(
            store,
            paths,
            category="function_purpose",
            summary="main parses config",
            address=0x401000,
            relevance="high",
        )

    assert log_error_mock.called
    # The memory write still lands: a failed entity write must not abort the rest.
    assert len(store.list_memories()) == 1


def _research_note_kwargs(tmp_path: Path) -> dict:
    return {
        "note_path": str(tmp_path / "notes" / "n.md"),
        "genre": "general",
        "title": "T",
        "content": "# T\nbody",
        "related": [],
        "review_passed": True,
    }


def test_research_note_memory_write_failure_is_logged(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import patch

    paths = _make_paths(tmp_path)
    paths.ensure()
    store = KnowledgeRawStore(paths)
    monkeypatch.setattr(store, "upsert_memory", _boom("memory boom"))

    with patch("lucnhan.memory.ingest.log_error") as log_error_mock:
        ingest._write_research_to_jsonl(store, paths, **_research_note_kwargs(tmp_path))

    assert log_error_mock.called


def test_research_note_entity_write_failure_is_logged(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import patch

    paths = _make_paths(tmp_path)
    paths.ensure()
    store = KnowledgeRawStore(paths)
    monkeypatch.setattr(store, "upsert_entity", _boom("entity boom"))

    with patch("lucnhan.memory.ingest.log_error") as log_error_mock:
        ingest._write_research_to_jsonl(store, paths, **_research_note_kwargs(tmp_path))

    assert log_error_mock.called
    assert len(store.list_memories()) == 1


def test_research_note_observation_failure_is_logged(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import patch

    paths = _make_paths(tmp_path)
    paths.ensure()
    store = KnowledgeRawStore(paths)
    monkeypatch.setattr(store, "append_observation", _boom("observation boom"))

    with patch("lucnhan.memory.ingest.log_error") as log_error_mock:
        ingest._write_research_to_jsonl(store, paths, **_research_note_kwargs(tmp_path))

    assert log_error_mock.called


def test_research_note_related_write_failure_is_logged(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import patch

    paths = _make_paths(tmp_path)
    paths.ensure()
    store = KnowledgeRawStore(paths)
    monkeypatch.setattr(store, "upsert_relation_from", _boom("relation boom"))
    kwargs = _research_note_kwargs(tmp_path)
    kwargs["related"] = ["other-note"]

    with patch("lucnhan.memory.ingest.log_error") as log_error_mock:
        ingest._write_research_to_jsonl(store, paths, **kwargs)

    assert log_error_mock.called


def test_exploration_memory_write_failure_is_logged(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import patch

    paths = _make_paths(tmp_path)
    paths.ensure()
    store = KnowledgeRawStore(paths)
    monkeypatch.setattr(store, "append_observation", _boom("observation boom"))

    with patch("lucnhan.memory.ingest.log_error") as log_error_mock:
        ingest._write_exploration_to_jsonl(
            store,
            paths,
            category="function_purpose",
            summary="main parses config",
            address=0x401000,
            relevance="high",
        )

    assert log_error_mock.called
    assert len(store.list_memories()) == 1


def test_research_note_function_relation_write_failure_is_logged(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import patch

    paths = _make_paths(tmp_path)
    paths.ensure()
    store = KnowledgeRawStore(paths)
    monkeypatch.setattr(store, "upsert_relation_from", _boom("relation boom"))
    kwargs = _research_note_kwargs(tmp_path)
    kwargs["content"] = "# T\nsee 0x401000"

    with patch("lucnhan.memory.ingest.log_error") as log_error_mock:
        ingest._write_research_to_jsonl(store, paths, **kwargs)

    assert log_error_mock.called
    assert len(store.list_memories()) == 1
