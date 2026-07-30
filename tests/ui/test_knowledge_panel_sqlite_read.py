"""Knowledge panel read-path migration tests.

Verifies the contract added by Task 8: ``_refresh_knowledge_panel``
prefers the SQLite repository exposed by ``SessionControllerBase.memory_service``
and falls back to the JSONL ``make_store`` path when the service is
``None``.  The tests are written against the panel widget and the panel
core helper that drives it.
"""

from __future__ import annotations

import os
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# ---------------------------------------------------------------------------
# Fixture factories
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _ensure_qapp() -> None:
    """Make sure a ``QApplication`` exists for the lifetime of the test
    session.  ``KnowledgePanel()`` construction hangs on offscreen Qt
    platforms without a running application instance, so we create one
    once here and let every test share it.
    """
    try:
        from rikugan.ui.qt_compat import QApplication
    except Exception:
        from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])


class _StubMemoryService:
    """Lightweight stand-in for ``BinaryMemoryService``.

    Exposes ``repository`` (MagicMock with ``list_memories`` /
    ``list_entities`` / ``list_relations`` / ``count_observations``)
    and ``paths.notes`` (a Path) — exactly the surface that
    ``_refresh_knowledge_panel`` reads.
    """

    def __init__(self, notes_path: Path) -> None:
        self.paths = types.SimpleNamespace(notes=notes_path)
        self.repository = MagicMock()
        self.repository.list_memories.return_value = []
        self.repository.list_entities.return_value = []
        self.repository.list_relations.return_value = []
        self.repository.count_observations.return_value = 0


def _make_panel_core():
    """Build a bare ``RikuganPanelCore`` via ``__new__`` so the heavy
    ``__init__`` (which would touch every dependency) is bypassed.
    """
    from rikugan.ui.panel_core import RikuganPanelCore

    panel = RikuganPanelCore.__new__(RikuganPanelCore)
    panel._is_shutdown = False
    panel._ctrl = MagicMock()
    panel._config = MagicMock()
    panel._config.knowledge_enabled = True
    panel._knowledge_panel = MagicMock()
    return panel


@pytest.fixture
def panel_core_with_widget():
    """Yield a bare ``RikuganPanelCore`` whose ``_knowledge_panel`` is
    a MagicMock.  Tests can inspect the panel's call history.
    """
    return _make_panel_core()


# ---------------------------------------------------------------------------
# Contract tests for the panel widget (verbatim from the brief)
# ---------------------------------------------------------------------------


def test_refresh_panel_reads_sqlite_when_service_wired(tmp_path: Path) -> None:
    """When ``memory_service`` is wired, the panel populates from the
    repository's ``list_*`` outputs without touching ``make_store``.
    """
    from rikugan.ui.knowledge_panel import KnowledgePanel

    panel = KnowledgePanel()
    service = MagicMock()
    service.repository.list_memories.return_value = []
    service.repository.list_entities.return_value = []
    service.repository.list_relations.return_value = []
    service.repository.count_observations.return_value = 0
    service.paths.notes = tmp_path / "notes"

    # The panel's host calls populate directly; we simulate the refresh.
    # In production, _refresh_knowledge_panel calls service.repository.list_*().
    panel.populate(
        memories=service.repository.list_memories(),
        entities=service.repository.list_entities(),
        relations=service.repository.list_relations(),
    )
    panel.set_counts({"memories": 0, "entities": 0, "relations": 0, "observations": 0})
    # The test verifies the contract: panel accepts SQLite-sourced data.
    assert panel._table.rowCount() == 0


def test_refresh_panel_falls_back_to_jsonl_when_service_none(tmp_path: Path) -> None:
    """When ``memory_service`` is ``None``, the panel renders the
    ``No IDB path`` placeholder cleanly.
    """
    from rikugan.ui.knowledge_panel import KnowledgePanel

    panel = KnowledgePanel()
    panel.set_disabled_message("No IDB path is set.")
    assert panel._table.rowCount() == 0


# ---------------------------------------------------------------------------
# Behavioural tests for ``_refresh_knowledge_panel`` (SQLite path)
# ---------------------------------------------------------------------------


def test_sqlite_path_calls_repository_and_skips_make_store(panel_core_with_widget, tmp_path: Path) -> None:
    """When ``memory_service`` is wired, ``_refresh_knowledge_panel`` reads
    from ``service.repository.list_*`` and ``service.paths.notes`` and
    never falls back to ``make_store``.
    """
    panel = panel_core_with_widget
    service = _StubMemoryService(tmp_path / "notes")
    panel._ctrl.memory_service = service
    panel._ctrl.session = MagicMock()
    panel._ctrl.session.idb_path = "/tmp/some-binary.i64"

    with patch("rikugan.memory.ingest.make_store") as make_store:
        panel._refresh_knowledge_panel()

    # SQLite path was taken: repository.list_*() were called.
    service.repository.list_memories.assert_called_once()
    service.repository.list_entities.assert_called_once()
    service.repository.list_relations.assert_called_once()
    service.repository.count_observations.assert_called_once()
    # The panel was populated with the SQLite-sourced data.
    panel._knowledge_panel.populate.assert_called_once()
    # The legacy JSONL fallback must NOT have been entered.
    make_store.assert_not_called()


def test_sqlite_path_passes_notes_dir_as_string(panel_core_with_widget, tmp_path: Path) -> None:
    """``list_notes`` expects ``notes_dir: str``; ``_refresh_knowledge_panel``
    must coerce ``service.paths.notes`` (a Path) to ``str`` before
    passing it in.
    """
    panel = panel_core_with_widget
    service = _StubMemoryService(tmp_path / "notes")
    panel._ctrl.memory_service = service
    panel._ctrl.session = MagicMock()
    panel._ctrl.session.idb_path = "/tmp/some-binary.i64"

    # Stub ``rikugan.memory.notes.list_notes`` so we can inspect the
    # argument type without touching the filesystem.
    with patch("rikugan.memory.notes.list_notes", return_value=[]) as list_notes:
        panel._refresh_knowledge_panel()

    list_notes.assert_called_once()
    arg = list_notes.call_args[0][0]
    assert isinstance(arg, str)
    assert arg == str(tmp_path / "notes")


def test_sqlite_path_populates_with_set_counts(panel_core_with_widget, tmp_path: Path) -> None:
    """The SQLite branch must populate the counts label with the
    SQLite-sourced counts, including the observation count.
    """
    panel = panel_core_with_widget
    service = _StubMemoryService(tmp_path / "notes")
    # Provide non-empty counts so we can verify them in the call.
    service.repository.list_memories.return_value = ["m1", "m2"]
    service.repository.list_entities.return_value = ["e1"]
    service.repository.list_relations.return_value = ["r1", "r2", "r3"]
    service.repository.count_observations.return_value = 4
    panel._ctrl.memory_service = service
    panel._ctrl.session = MagicMock()
    panel._ctrl.session.idb_path = "/tmp/some-binary.i64"

    with patch("rikugan.memory.notes.list_notes", return_value=[]):
        panel._refresh_knowledge_panel()

    panel._knowledge_panel.set_counts.assert_called_once_with(
        {
            "memories": 2,
            "entities": 1,
            "relations": 3,
            "observations": 4,
        }
    )


def test_sqlite_path_failure_falls_back_to_jsonl(panel_core_with_widget, tmp_path: Path) -> None:
    """When the SQLite path raises (e.g. corrupted repository), the
    panel must transparently fall back to the JSONL store.
    """
    panel = panel_core_with_widget
    service = _StubMemoryService(tmp_path / "notes")
    service.repository.list_memories.side_effect = RuntimeError("sqlite boom")
    panel._ctrl.memory_service = service
    panel._ctrl.session = MagicMock()
    panel._ctrl.session.idb_path = "/tmp/some-binary.i64"

    # ``make_store`` returns a usable store; both ``list_memories``
    # etc. return empty lists so the panel ends up in a clean
    # empty state.
    fake_store = MagicMock()
    fake_store.list_memories.return_value = []
    fake_store.list_entities.return_value = []
    fake_store.list_relations.return_value = []
    fake_store.count_observations.return_value = 0
    fake_paths = MagicMock()
    fake_paths.notes_dir = str(tmp_path / "notes")
    with (
        patch(
            "rikugan.memory.ingest.make_store",
            return_value=(fake_store, fake_paths),
        ),
        patch("rikugan.memory.notes.list_notes", return_value=[]),
    ):
        panel._refresh_knowledge_panel()

    # JSONL fallback took over: the panel was populated.
    panel._knowledge_panel.populate.assert_called_once()
    # And the disabled-message branch was NOT entered.
    panel._knowledge_panel.set_disabled_message.assert_not_called()


# ---------------------------------------------------------------------------
# Behavioural tests for ``_refresh_knowledge_panel`` (JSONL fallback)
# ---------------------------------------------------------------------------


def test_jsonl_path_used_when_memory_service_is_none(panel_core_with_widget, tmp_path: Path) -> None:
    """When ``memory_service`` is ``None``, the JSONL ``make_store`` path
    is used (the legacy behaviour from before Task 8).
    """
    panel = panel_core_with_widget
    panel._ctrl.memory_service = None
    panel._ctrl.session = MagicMock()
    panel._ctrl.session.idb_path = str(tmp_path / "fake.i64")

    fake_store = MagicMock()
    fake_store.list_memories.return_value = []
    fake_store.list_entities.return_value = []
    fake_store.list_relations.return_value = []
    fake_store.count_observations.return_value = 0
    fake_paths = MagicMock()
    fake_paths.notes_dir = str(tmp_path / "notes")
    with (
        patch(
            "rikugan.memory.ingest.make_store",
            return_value=(fake_store, fake_paths),
        ) as make_store,
        patch("rikugan.memory.notes.list_notes", return_value=[]),
    ):
        panel._refresh_knowledge_panel()

    # The JSONL fallback was entered.
    make_store.assert_called_once()
    # The panel was populated.
    panel._knowledge_panel.populate.assert_called_once()


def test_jsonl_path_disabled_when_store_init_fails(panel_core_with_widget, tmp_path: Path) -> None:
    """When ``make_store`` returns ``(None, None)``, the panel falls into
    the ``set_disabled_message`` branch and never populates.
    """
    panel = panel_core_with_widget
    panel._ctrl.memory_service = None
    panel._ctrl.session = MagicMock()
    panel._ctrl.session.idb_path = str(tmp_path / "fake.i64")

    with patch(
        "rikugan.memory.ingest.make_store",
        return_value=(None, None),
    ):
        panel._refresh_knowledge_panel()

    panel._knowledge_panel.set_disabled_message.assert_called_once()
    panel._knowledge_panel.populate.assert_not_called()
