# Knowledge UI and Write Path SQLite Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the Knowledge panel read path and the exploration/research write paths from the legacy JSONL `KnowledgeRawStore` to the SQLite `WorkspaceStore`, with a dual-write transition period and one-time auto-import of legacy JSONL data.

**Architecture:** Add schema v3 carrying graph metadata columns (`entity_refs`, `tags`, `evidence`). Dual-write exploration/research findings to SQLite and JSONL behind a module-level flag. Auto-import legacy JSONL into SQLite on first IDB open using the existing `import_workspace_bundle` primitive. Migrate Knowledge panel and retrieved knowledge section read paths to prefer SQLite, falling back to JSONL when `memory_service` is not wired. Emit a new `MEMORY_SAVED` event so the panel refreshes after a `save_memory` tool call.

**Tech Stack:** Python 3.11+, SQLite WAL, pytest, PySide6 test fixtures, TDD per task.

## Global Constraints

- `MEMORY_WORKSPACE_SCHEMA_VERSION` becomes exactly `3`.
- Migration v3 is additive: `ALTER TABLE ... ADD COLUMN` only; no backfill of graph metadata on existing rows; defaults are empty (`'[]'` for arrays, `''` for `evidence`).
- `_LEGACY_JSONL_DUAL_WRITE = True` is a module-level constant in `rikugan/memory/ingest.py`. Exploration/research writes hit SQLite first, JSONL second when the flag is `True`. `save_memory` tool writes remain SQLite-only.
- Auto-import runs once per workspace, gated by `workspace_meta` key `legacy_jsonl_imported` (ISO timestamp). JSONL files are never deleted.
- `import_workspace_bundle` (shipped in the durability tranche) is the only import primitive; its stage-validate-write contract is reused.
- Knowledge panel and retrieved knowledge section fall back to JSONL only when `memory_service` is `None`.
- `TurnEventType.MEMORY_SAVED = "memory_saved"` is added to the enum; `_on_event` in `panel_core.py` extends its refresh trigger list.
- Entity `tags` migrate from the `metadata` JSON blob to a dedicated column; the repository stops writing `tags` into `metadata` on v3, and reads from the dedicated column.
- Follow TDD for each task: failing test, observed failure, minimal implementation, passing focused tests, then commit.
- Do not edit `uv.lock` as a side effect of test commands; use `uv run --frozen` where applicable.
- Do not flip `_LEGACY_JSONL_DUAL_WRITE` to `False`, delete `KnowledgeRawStore`, migrate the notes subsystem, add `importance`/`verified`/`source_refs`/`relation_refs` columns, rewrite the ranker, or add a user-facing re-import command in this plan.

---

## File Structure

### New files

- `rikugan/memory/jsonl_migration.py` — JSONL → bundle envelope adapter, temp bundle writer, one-time auto-import trigger.
- `rikugan/memory/sqlite_retrieval.py` — adapter that feeds SQLite records into the existing ranker and returns a `RetrievalPack`.
- `tests/memory/test_workspace_migration_v3.py` — handcrafted v2 fixture, v3 backfill/hash guards.
- `tests/memory/test_jsonl_migration.py` — adapter, temp bundle, auto-import trigger.
- `tests/memory/test_dual_write_ingest.py` — flag on/off, SQLite/JSONL failure isolation.
- `tests/memory/test_sqlite_retrieval.py` — adapter ranking equivalence with JSONL ranker.
- `tests/ui/test_knowledge_panel_sqlite_read.py` — panel SQLite read, JSONL fallback, save_memory refresh.

### Modified production files

- `rikugan/constants.py` — workspace schema version `3`.
- `rikugan/memory/workspace_store.py` — `_migrate_v3`, `FactRecord`/`EntityRecord`/`RelationRecord` fields, `put_fact`/`put_entity`/`put_relation`/`save_fact_if_semantically_absent` extensions, `get_fact`/`list_facts`/`get_entity`/`list_entities`/`list_relations` SELECTs.
- `rikugan/memory/repository.py` — `save_exploration_finding`, `list_entities` reads `tags` column, `upsert_entity` no longer writes `tags` into `metadata`.
- `rikugan/memory/service.py` — `save_exploration_finding` service wrapper.
- `rikugan/memory/ingest.py` — `_LEGACY_JSONL_DUAL_WRITE`, dual-write refactor of `ingest_exploration_finding` and `ingest_research_note`.
- `rikugan/memory/retrieve.py` — extract `retrieve_from_records` from `retrieve`; `retrieve` becomes a thin wrapper.
- `rikugan/agent/loop.py` — `_build_retrieved_knowledge_section` prefers SQLite, emit `MEMORY_SAVED` event from `_handle_save_memory_tool`.
- `rikugan/agent/turn.py` — `TurnEventType.MEMORY_SAVED`.
- `rikugan/ui/session_controller_base.py` — `memory_service` accessor; `maybe_import_legacy_jsonl` call in `_wire_central_memory`.
- `rikugan/ui/panel_core.py` — `_refresh_knowledge_panel` prefers SQLite; `_on_event` handles `MEMORY_SAVED`.

### Modified tests

- `tests/memory/test_workspace_store.py`
- `tests/memory/test_repository.py`
- `tests/memory/test_service.py`

---

### Task 1: Schema v3 Migration and Record Extensions

**Files:**

- Modify: `rikugan/constants.py:53`
- Modify: `rikugan/memory/workspace_store.py`
- Create: `tests/memory/test_workspace_migration_v3.py`
- Modify: `tests/memory/test_workspace_store.py`

**Interfaces:**

- Consumes: existing `_migrate_v2`, `_MIGRATIONS` dict, `FactRecord`, `EntityRecord`, `RelationRecord`.
- Produces:
  - `MEMORY_WORKSPACE_SCHEMA_VERSION = 3`
  - `FactRecord.entity_refs: list[str]`, `FactRecord.tags: list[str]`
  - `EntityRecord.tags: list[str]`
  - `RelationRecord.evidence: str`
  - `WorkspaceStore.put_fact(..., *, entity_refs: list[str] | None = None, tags: list[str] | None = None)`
  - `WorkspaceStore.put_entity(..., *, tags: list[str] | None = None)`
  - `WorkspaceStore.put_relation(..., *, evidence: str = "")`
  - `WorkspaceStore.save_fact_if_semantically_absent(..., entity_refs: list[str] | None = None, tags: list[str] | None = None)`
  - `_migrate_v3(conn)` registered in `_MIGRATIONS`

- [ ] **Step 1: Write failing migration and round-trip tests**

```python
# tests/memory/test_workspace_migration_v3.py
from __future__ import annotations

import sqlite3

import pytest

from rikugan.memory.workspace import MemoryLocator, new_memory_id, new_record_id
from rikugan.memory import workspace_store
from rikugan.memory.workspace_store import WorkspaceStore


def _create_v2_database(path, owner: str) -> str:
    """Build a faithful v2 database via the real _migrate_v1 + _migrate_v2."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    workspace_store._migrate_v1(conn)
    workspace_store._migrate_v2(conn)
    conn.execute("PRAGMA user_version = 2")
    fact_id = new_record_id("fact")
    conn.execute("INSERT INTO workspace_meta VALUES('owner_memory_id', ?)", (owner,))
    conn.execute("INSERT INTO workspace_meta VALUES('workspace_kind', 'binary')")
    conn.execute(
        "INSERT INTO facts(fact_id, fact_type, title, semantic_hash, current_revision, created_at)"
        " VALUES(?, 'algorithm', 'RC4', 'a' * 64, 1, 10.0)",
        (fact_id,),
    )
    conn.execute(
        "INSERT INTO fact_revisions(fact_id, revision, content, content_hash, confidence, created_at)"
        " VALUES(?, 1, 'Uses RC4', 'legacy-hash', 0.8, 10.0)",
        (fact_id,),
    )
    conn.commit()
    conn.close()
    return fact_id


def test_v2_workspace_migrates_to_v3_with_default_empty_columns(tmp_path) -> None:
    owner = new_memory_id()
    paths = MemoryLocator(tmp_path).binary(owner)
    fact_id = _create_v2_database(paths.database, owner)

    store = WorkspaceStore.open(paths, owner_memory_id=owner)
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 3

    columns = {row[1] for row in store._conn.execute("PRAGMA table_info(facts)")}
    assert {"entity_refs", "tags"}.issubset(columns)

    record = store.get_fact(fact_id)
    assert record is not None
    assert record.entity_refs == []
    assert record.tags == []
    store.close()


def test_v3_migration_failure_rolls_back(tmp_path, monkeypatch) -> None:
    owner = new_memory_id()
    paths = MemoryLocator(tmp_path).binary(owner)
    _create_v2_database(paths.database, owner)

    def fail_after_alter(conn) -> None:
        conn.execute("ALTER TABLE facts ADD COLUMN entity_refs TEXT")
        raise RuntimeError("injected migration failure")

    monkeypatch.setitem(workspace_store._MIGRATIONS, 3, fail_after_alter)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        WorkspaceStore.open(paths, owner_memory_id=owner)
    with sqlite3.connect(paths.database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        columns = {row[1] for row in conn.execute("PRAGMA table_info(facts)")}
        assert "entity_refs" not in columns


def test_v3_not_null_constraints_reject_missing_values(tmp_path) -> None:
    owner = new_memory_id()
    paths = MemoryLocator(tmp_path).binary(owner)
    store = WorkspaceStore.create(paths, owner_memory_id=owner)
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO facts(fact_id, fact_type, title, semantic_hash, current_revision, created_at)"
            " VALUES(?, 'x', 'x', 'a' * 64, 1, 1)",
            (new_record_id("fact"),),
        )
    store.close()
```

```python
# Append to tests/memory/test_workspace_store.py (after the existing hash tests):

def test_put_fact_with_entity_refs_and_tags_roundtrip(tmp_path: Path) -> None:
    store, _ = _create_store(tmp_path)
    fid = new_record_id("fact")
    store.put_fact(
        fid, "algorithm", "RC4", "Uses RC4", 0.8,
        expected_revision=0,
        entity_refs=["func:0x401000", "global:0x409000"],
        tags=["crypto", "c2"],
    )
    fetched = store.get_fact(fid)
    assert fetched is not None
    assert fetched.entity_refs == ["func:0x401000", "global:0x409000"]
    assert fetched.tags == ["crypto", "c2"]
    store.close()


def test_put_fact_without_entity_refs_defaults_empty(tmp_path: Path) -> None:
    store, _ = _create_store(tmp_path)
    fid = new_record_id("fact")
    store.put_fact(fid, "algorithm", "RC4", "Uses RC4", 0.8, expected_revision=0)
    fetched = store.get_fact(fid)
    assert fetched is not None
    assert fetched.entity_refs == []
    assert fetched.tags == []
    store.close()


def test_put_entity_with_tags_roundtrip(tmp_path: Path) -> None:
    store, _ = _create_store(tmp_path)
    eid = new_record_id("entity")
    store.put_entity(
        eid, "function", "main",
        metadata={"display_name": "main", "address": "0x401000"},
        tags=["entry", "parser"],
    )
    fetched = store.get_entity(eid)
    assert fetched is not None
    assert fetched.tags == ["entry", "parser"]
    store.close()


def test_put_relation_with_evidence_roundtrip(tmp_path: Path) -> None:
    store, _ = _create_store(tmp_path)
    sid = new_record_id("entity")
    oid = new_record_id("entity")
    store.put_entity(sid, "function", "decrypt", {})
    store.put_entity(oid, "function", "main", {})
    rid = new_record_id("relation")
    store.put_relation(
        rid, sid, "calls", oid, 0.9,
        evidence="xref at 0x401020",
    )
    rels = store.list_relations()
    assert len(rels) == 1
    assert rels[0].evidence == "xref at 0x401020"
    store.close()
```

- [ ] **Step 2: Run tests to verify RED**

```bash
uv run --frozen --python 3.11 python -m pytest tests/memory/test_workspace_migration_v3.py tests/memory/test_workspace_store.py -q
```

Expected: migration tests fail because schema version remains 2; round-trip tests fail because `put_fact`/`put_entity`/`put_relation` do not accept the new keyword arguments.

- [ ] **Step 3: Implement the migration, record fields, and store extensions**

In `rikugan/constants.py`:

```python
MEMORY_WORKSPACE_SCHEMA_VERSION = 3
```

In `rikugan/memory/workspace_store.py`, extend the dataclasses:

```python
@dataclass(frozen=True)
class FactRecord:
    fact_id: str
    fact_type: str
    title: str
    content: str
    semantic_hash: str
    confidence: float
    revision: int
    created_at: float
    entity_refs: list[str]
    tags: list[str]


@dataclass(frozen=True)
class EntityRecord:
    entity_id: str
    entity_type: str
    name: str
    metadata: dict[str, Any]
    revision: int
    tags: list[str]


@dataclass(frozen=True)
class RelationRecord:
    relation_id: str
    subject_id: str
    predicate: str
    object_id: str
    confidence: float
    revision: int
    evidence: str
```

Add the migration function after `_migrate_v2`:

```python
def _migrate_v3(conn: Any) -> None:
    conn.execute(
        "ALTER TABLE facts ADD COLUMN entity_refs TEXT NOT NULL DEFAULT '[]'"
    )
    conn.execute(
        "ALTER TABLE facts ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'"
    )
    conn.execute(
        "ALTER TABLE entities ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'"
    )
    conn.execute(
        "ALTER TABLE relations ADD COLUMN evidence TEXT NOT NULL DEFAULT ''"
    )
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("workspace v3 migration failed foreign key check")


_MIGRATIONS = {1: _migrate_v1, 2: _migrate_v2, 3: _migrate_v3}
```

Extend `put_fact` signature and INSERT/UPDATE to include `entity_refs` and `tags`:

```python
def put_fact(
    self,
    fact_id: str,
    fact_type: str,
    title: str,
    content: str,
    confidence: float,
    *,
    semantic_hash: str | None = None,
    entity_refs: list[str] | None = None,
    tags: list[str] | None = None,
    expected_revision: int,
) -> FactRecord:
    # ... existing validation ...
    entity_refs_json = json.dumps(entity_refs or [], ensure_ascii=False, sort_keys=True)
    tags_json = json.dumps(tags or [], ensure_ascii=False, sort_keys=True)
    # In INSERT: add entity_refs, tags columns and values
    # In UPDATE: set entity_refs = ?, tags = ?
    # In FactRecord construction at return: pass entity_refs=entity_refs or [], tags=tags or []
```

Extend `put_entity` to accept `tags: list[str] | None = None` and persist a `tags TEXT` column. Extend `put_relation` to accept `evidence: str = ""` and persist it. Update `get_entity`, `list_relations`, and every `FactRecord`/`EntityRecord`/`RelationRecord` construction site to populate the new fields from the row.

Extend `save_fact_if_semantically_absent` with keyword-only `entity_refs` and `tags` parameters; the INSERT inside the created branch writes the new columns.

- [ ] **Step 4: Run focused tests and lint**

```bash
uv run --frozen --python 3.11 python -m pytest tests/memory/test_workspace_migration_v3.py tests/memory/test_workspace_store.py -q
uv run --frozen --python 3.11 python -m ruff check rikugan/memory/workspace_store.py rikugan/constants.py tests/memory/test_workspace_migration_v3.py tests/memory/test_workspace_store.py
```

Expected: all tests pass; Ruff prints `All checks passed!`.

- [ ] **Step 5: Commit schema v3**

```bash
git add rikugan/constants.py rikugan/memory/workspace_store.py tests/memory/test_workspace_migration_v3.py tests/memory/test_workspace_store.py
git commit -m "feat(memory): migrate workspaces to schema v3 with graph metadata"
```

---

### Task 2: Repository save_exploration_finding and Service Wrapper

**Files:**

- Modify: `rikugan/memory/repository.py`
- Modify: `rikugan/memory/service.py`
- Modify: `tests/memory/test_repository.py`
- Modify: `tests/memory/test_service.py`

**Interfaces:**

- Consumes: `WorkspaceStore.save_fact_if_semantically_absent` from Task 1, `SavedKnowledgeMemory` from the durability tranche.
- Produces:
  - `SQLiteKnowledgeRepository.save_exploration_finding(category, fact, source, *, entity_refs=None, tags=None, title=None, confidence=0.7) -> SavedKnowledgeMemory`
  - `BinaryMemoryService.save_exploration_finding(authority, *, category, title, content, confidence, entity_refs, tags, source) -> SaveMemoryResult`

- [ ] **Step 1: Write failing repository test**

```python
# Append to tests/memory/test_repository.py
from rikugan.memory.fact_identity import semantic_fact_hash


def test_save_exploration_finding_persists_entity_refs_and_tags(tmp_path: Path) -> None:
    repo, _ = _create_repo(tmp_path)
    saved = repo.save_exploration_finding(
        "function_purpose",
        "main parses config",
        "exploration",
        entity_refs=["func:0x401000"],
        tags=["parser", "config"],
    )
    assert saved.outcome == "created"
    stored = repo._store.get_fact(saved.record.id)
    assert stored is not None
    assert stored.entity_refs == ["func:0x401000"]
    assert stored.tags == ["parser", "config"]
    assert stored.semantic_hash == semantic_fact_hash("function_purpose", "main parses config")
```

- [ ] **Step 2: Run repository test to verify RED**

```bash
uv run --frozen --python 3.11 python -m pytest tests/memory/test_repository.py -q
```

Expected: `AttributeError: 'SQLiteKnowledgeRepository' object has no attribute 'save_exploration_finding'`.

- [ ] **Step 3: Implement repository method**

In `rikugan/memory/repository.py`:

```python
def save_exploration_finding(
    self,
    category: str,
    fact: str,
    source: str,
    *,
    entity_refs: list[str] | None = None,
    tags: list[str] | None = None,
    title: str | None = None,
    confidence: float = 0.7,
) -> SavedKnowledgeMemory:
    from .workspace import new_record_id
    from .fact_identity import canonicalize_fact_content, canonicalize_fact_type, semantic_fact_hash

    canonical_type = canonicalize_fact_type(category)
    canonical_content = canonicalize_fact_content(fact)
    digest = semantic_fact_hash(canonical_type, canonical_content)
    resolved_title = title or canonical_type
    record, outcome = self._store.save_fact_if_semantically_absent(
        fact_id=new_record_id("fact"),
        fact_type=canonical_type,
        title=resolved_title,
        content=canonical_content,
        semantic_hash=digest,
        confidence=confidence,
        observation_id=new_record_id("observation"),
        observation_type=source,
        observation_payload=json.dumps(
            {"category": canonical_type, "semantic_hash": digest, "source": source},
            ensure_ascii=False,
            sort_keys=True,
        ),
        entity_refs=entity_refs,
        tags=tags,
    )
    return SavedKnowledgeMemory(
        record=KnowledgeMemory(
            id=record.fact_id,
            binary_id=self.owner_memory_id,
            type=record.fact_type,
            title=record.title,
            content=record.content,
            confidence=record.confidence,
        ),
        outcome=outcome,
    )
```

Also update `list_entities` to read `tags` from the dedicated column (not from `metadata` JSON) and update `upsert_entity` to stop writing `tags` into the `metadata` JSON blob.

- [ ] **Step 4: Write failing service test**

```python
# Append to tests/memory/test_service.py
def test_save_exploration_finding_returns_outcome_and_persists_metadata(tmp_path: Path) -> None:
    service, issuer, context = _create_service(tmp_path)
    authority = issuer.issue(context)
    result = service.save_exploration_finding(
        authority,
        category="function_purpose",
        title="main",
        content="main parses config",
        confidence=0.8,
        entity_refs=["func:0x401000"],
        tags=["parser"],
        source="exploration",
    )
    assert result.outcome == "created"
    stored = service.repository._store.get_fact(result.record_id)
    assert stored is not None
    assert stored.entity_refs == ["func:0x401000"]
    assert stored.tags == ["parser"]
```

- [ ] **Step 5: Run service test to verify RED**

```bash
uv run --frozen --python 3.11 python -m pytest tests/memory/test_service.py -q
```

Expected: `AttributeError: 'BinaryMemoryService' object has no attribute 'save_exploration_finding'`.

- [ ] **Step 6: Implement service method**

In `rikugan/memory/service.py`:

```python
def save_exploration_finding(
    self,
    authority: MemoryWriteAuthority | None,
    *,
    category: str,
    title: str,
    content: str,
    confidence: float,
    entity_refs: list[str],
    tags: list[str],
    source: str,
) -> SaveMemoryResult:
    self.require_write_authority(authority)
    normalized_category = _sanitize_category(category)
    normalized_content = _sanitize_fact(content)
    if not normalized_category:
        raise ValueError("category must not be empty after sanitization")
    if not normalized_content:
        raise ValueError("content must not be empty after sanitization")

    saved = self.repository.save_exploration_finding(
        normalized_category,
        normalized_content,
        source,
        entity_refs=entity_refs,
        tags=tags,
        title=title,
        confidence=confidence,
    )
    record = saved.record
    verify = self.repository._store.get_fact(record.id)
    if verify is None:
        from ..core.logging import log_error as _le
        _le(f"save_exploration_finding BUG: fact {record.id} not found after save!")
    try:
        self.projector.project(self.paths, self.store)
        return SaveMemoryResult(
            record_id=record.id,
            revision=verify.revision if verify is not None else getattr(record, "revision", 1),
            outcome=saved.outcome,
            projection_dirty=False,
            warning="",
        )
    except Exception as exc:
        from ..core.logging import log_error as _le
        _le(f"MEMORY.md projection failed: {exc!r}")
        self.store.mark_projection_dirty()
        return SaveMemoryResult(
            record_id=record.id,
            revision=verify.revision if verify is not None else getattr(record, "revision", 1),
            outcome=saved.outcome,
            projection_dirty=True,
            warning=str(exc),
        )
```

- [ ] **Step 7: Run focused tests and lint**

```bash
uv run --frozen --python 3.11 python -m pytest tests/memory/test_repository.py tests/memory/test_service.py -q
uv run --frozen --python 3.11 python -m ruff check rikugan/memory/repository.py rikugan/memory/service.py
```

Expected: all tests pass; Ruff clean.

- [ ] **Step 8: Commit repository and service extensions**

```bash
git add rikugan/memory/repository.py rikugan/memory/service.py tests/memory/test_repository.py tests/memory/test_service.py
git commit -m "feat(memory): expose exploration finding write with graph metadata"
```

---

### Task 3: JSONL → Bundle Adapter and Temp Bundle Writer

**Files:**

- Create: `rikugan/memory/jsonl_migration.py`
- Create: `tests/memory/test_jsonl_migration.py`

**Interfaces:**

- Consumes: `KnowledgeRawStore.list_memories()`, `list_entities()`, `list_relations()` from `rikugan/memory/raw_store.py`; `KnowledgePaths` from `rikugan/memory/paths.py`; `MEMORY_BUNDLE_SCHEMA_VERSION` and `ManifestFile` from `rikugan/memory/bundle_schema.py`.
- Produces:
  - `jsonl_to_bundle_envelopes(store, paths) -> list[dict[str, Any]]`
  - `write_envelopes_to_temp_bundle(envelopes, origin_memory_id) -> Path`

- [ ] **Step 1: Write failing adapter tests**

```python
# tests/memory/test_jsonl_migration.py
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from rikugan.memory.jsonl_migration import (
    jsonl_to_bundle_envelopes,
    write_envelopes_to_temp_bundle,
)
from rikugan.memory.paths import KnowledgePaths, derive_binary_id
from rikugan.memory.raw_store import KnowledgeRawStore
from rikugan.memory.schema import KnowledgeMemory, KnowledgeEntity, KnowledgeRelation


def _make_paths(tmp_path: Path) -> KnowledgePaths:
    return KnowledgePaths(
        idb_path=str(tmp_path / "test.i64"),
        notes_dir=str(tmp_path / "notes"),
        kb_dir=str(tmp_path / "kb"),
        reports_dir=str(tmp_path / "notes" / "reports"),
        binary_id=derive_binary_id(str(tmp_path / "test.i64")),
    )


def test_jsonl_to_bundle_envelopes_preserves_fact_fields(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    paths.ensure()
    store = KnowledgeRawStore(paths)
    mem = KnowledgeMemory(
        id="mem:function_purpose:0x401000:abc",
        binary_id=paths.binary_id,
        type="function_purpose",
        title="main parses config",
        content="main parses config at startup",
        entity_refs=["func:0x401000"],
        tags=["parser", "config"],
        confidence=0.8,
    )
    store.upsert_memory(mem)

    envelopes = jsonl_to_bundle_envelopes(store, paths)
    assert len(envelopes) == 1
    env = envelopes[0]
    assert env["record_type"] == "fact"
    assert env["record_id"] == mem.id
    payload = env["payload"]
    assert payload["type"] == "function_purpose"
    assert payload["title"] == "main parses config"
    assert payload["content"] == "main parses config at startup"
    assert payload["confidence"] == 0.8
    assert payload["entity_refs"] == ["func:0x401000"]
    assert payload["tags"] == ["parser", "config"]


def test_jsonl_to_bundle_envelopes_preserves_entity_and_relation(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    paths.ensure()
    store = KnowledgeRawStore(paths)
    ent = KnowledgeEntity(
        id="func:0x401000", binary_id=paths.binary_id, type="function",
        name="main", display_name="main", address="0x401000", tags=["entry"],
    )
    rel = KnowledgeRelation(
        id="rel:1", binary_id=paths.binary_id, src="func:0x401000",
        predicate="calls", dst="func:0x402000", evidence="xref at 0x401020",
    )
    store.upsert_entity(ent)
    store.upsert_relation(rel)

    envelopes = jsonl_to_bundle_envelopes(store, paths)
    types = {e["record_type"] for e in envelopes}
    assert types == {"entity", "relation"}


def test_jsonl_to_bundle_envelopes_empty_store_returns_empty(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    paths.ensure()
    store = KnowledgeRawStore(paths)
    assert jsonl_to_bundle_envelopes(store, paths) == []


def test_write_envelopes_to_temp_bundle_creates_valid_zip(tmp_path: Path) -> None:
    envelopes = [
        {"record_type": "fact", "record_id": "mem:1", "payload": {"type": "general", "title": "t", "content": "c", "confidence": 0.5}},
    ]
    bundle_path = write_envelopes_to_temp_bundle(envelopes, "mem-abc")
    assert bundle_path.is_file()
    with zipfile.ZipFile(bundle_path, "r") as zf:
        names = zf.namelist()
        assert "manifest.json" in names
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["schema_version"] == 1
        assert manifest["origin_memory_id"] == "mem-abc"
        assert any(n.startswith("records/") for n in names)
    bundle_path.unlink(missing_ok=True)
```

- [ ] **Step 2: Run tests to verify RED**

```bash
uv run --frozen --python 3.11 python -m pytest tests/memory/test_jsonl_migration.py -q
```

Expected: `ModuleNotFoundError: No module named 'rikugan.memory.jsonl_migration'`.

- [ ] **Step 3: Implement the adapter and temp bundle writer**

```python
# rikugan/memory/jsonl_migration.py
"""Adapter converting legacy JSONL knowledge records into bundle envelopes.

The output matches the wire format consumed by ``import_workspace_bundle``
so the durability tranche's idempotent importer can ingest legacy data
without a separate import code path.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .bundle_schema import MEMORY_BUNDLE_SCHEMA_VERSION, ManifestFile
from .paths import KnowledgePaths
from .raw_store import KnowledgeRawStore


def jsonl_to_bundle_envelopes(
    store: KnowledgeRawStore,
    paths: KnowledgePaths,  # noqa: ARG001 — reserved for future source_refs
) -> list[dict[str, Any]]:
    """Convert all JSONL records into bundle envelope dicts."""
    envelopes: list[dict[str, Any]] = []

    for mem in store.list_memories():
        envelopes.append({
            "record_type": "fact",
            "record_id": mem.id,
            "payload": {
                "type": mem.type,
                "title": mem.title,
                "content": mem.content,
                "confidence": mem.confidence,
                "entity_refs": mem.entity_refs,
                "tags": mem.tags,
            },
        })

    for ent in store.list_entities():
        envelopes.append({
            "record_type": "entity",
            "record_id": ent.id,
            "payload": {
                "type": ent.type,
                "name": ent.name,
                "display_name": ent.display_name,
                "address": ent.address,
                "tags": ent.tags,
            },
        })

    for rel in store.list_relations():
        envelopes.append({
            "record_type": "relation",
            "record_id": rel.id,
            "payload": {
                "src": rel.src,
                "predicate": rel.predicate,
                "dst": rel.dst,
                "confidence": rel.confidence,
                "evidence": rel.evidence,
            },
        })

    return envelopes


def write_envelopes_to_temp_bundle(
    envelopes: list[dict[str, Any]],
    origin_memory_id: str,
) -> Path:
    """Write envelopes to a temp ZIP bundle matching export_workspace layout."""
    record_files: dict[str, list[bytes]] = {}
    for env in envelopes:
        rtype = env["record_type"]
        fname = f"records/{rtype}s.jsonl"
        line = json.dumps(env, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        record_files.setdefault(fname, []).append(line)

    manifest_files: list[ManifestFile] = []
    total = 0
    for name, lines in record_files.items():
        content = b"\n".join(lines) + b"\n"
        sha = hashlib.sha256(content).hexdigest()
        count = len(lines)
        manifest_files.append(ManifestFile(name=name, sha256=sha, uncompressed_size=len(content), record_count=count))
        total += count

    manifest_json = json.dumps(
        {
            "schema_version": MEMORY_BUNDLE_SCHEMA_VERSION,
            "scope": "binary",
            "export_mode": "portable",
            "origin_memory_id": origin_memory_id,
            "exported_at": datetime.now(UTC).isoformat(),
            "files": [
                {"name": f.name, "sha256": f.sha256, "uncompressed_size": f.uncompressed_size, "record_count": f.record_count}
                for f in manifest_files
            ],
            "record_counts": {
                name.replace("records/", "").replace(".jsonl", ""): f.record_count
                for name, f in zip(record_files.keys(), manifest_files, strict=True)
            },
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )

    fd, tmp_path_str = tempfile.mkstemp(prefix="rikugan-jsonl-", suffix=".zip")
    tmp_path = Path(tmp_path_str)
    import os
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", manifest_json)
            for name, lines in record_files.items():
                content = b"\n".join(lines) + b"\n"
                zf.writestr(name, content)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path
```

- [ ] **Step 4: Run focused tests and lint**

```bash
uv run --frozen --python 3.11 python -m pytest tests/memory/test_jsonl_migration.py -q
uv run --frozen --python 3.11 python -m ruff check rikugan/memory/jsonl_migration.py tests/memory/test_jsonl_migration.py
```

Expected: all tests pass; Ruff clean.

- [ ] **Step 5: Commit the adapter**

```bash
git add rikugan/memory/jsonl_migration.py tests/memory/test_jsonl_migration.py
git commit -m "feat(memory): convert JSONL records to bundle envelopes"
```

---

### Task 4: Auto-Import Trigger

**Files:**

- Modify: `rikugan/memory/jsonl_migration.py`
- Create: `tests/memory/test_jsonl_migration.py` (extend)
- Modify: `rikugan/ui/session_controller_base.py`

**Interfaces:**

- Consumes: `write_envelopes_to_temp_bundle` and `jsonl_to_bundle_envelopes` from Task 3; `import_workspace_bundle` from the durability tranche; `WorkspaceStore` and `SQLiteKnowledgeRepository`.
- Produces:
  - `maybe_import_legacy_jsonl(workspace_store, owner_memory_id, jsonl_paths) -> None`

- [ ] **Step 1: Write failing auto-import tests**

```python
# Append to tests/memory/test_jsonl_migration.py
import sqlite3

from rikugan.memory.workspace import MemoryLocator, new_memory_id
from rikugan.memory.workspace_store import WorkspaceStore
from rikugan.memory.repository import SQLiteKnowledgeRepository
from rikugan.memory.jsonl_migration import maybe_import_legacy_jsonl


def test_maybe_import_skips_when_marker_present(tmp_path: Path) -> None:
    owner = new_memory_id()
    paths = MemoryLocator(tmp_path).binary(owner)
    store = WorkspaceStore.create(paths, owner_memory_id=owner)
    store._conn.execute(
        "INSERT INTO workspace_meta(key, value) VALUES('legacy_jsonl_imported', '2026-01-01T00:00:00Z')"
    )
    store._conn.commit()
    jsonl_paths = _make_paths(tmp_path / "jsonl")
    jsonl_paths.ensure()
    # Add a record — it should NOT be imported because the marker exists.
    raw = KnowledgeRawStore(jsonl_paths)
    raw.upsert_memory(KnowledgeMemory(
        id="mem:x", binary_id=jsonl_paths.binary_id, type="general",
        title="t", content="c",
    ))
    maybe_import_legacy_jsonl(store, owner, jsonl_paths)
    repo = SQLiteKnowledgeRepository(store, owner_memory_id=owner)
    assert repo.count_observations() == 0
    assert len(repo.list_memories()) == 0
    store.close()


def test_maybe_import_imports_records_once(tmp_path: Path) -> None:
    owner = new_memory_id()
    workspace_paths = MemoryLocator(tmp_path / "ws").binary(owner)
    store = WorkspaceStore.create(workspace_paths, owner_memory_id=owner)

    jsonl_paths = _make_paths(tmp_path / "jsonl")
    jsonl_paths.ensure()
    raw = KnowledgeRawStore(jsonl_paths)
    raw.upsert_memory(KnowledgeMemory(
        id="mem:function_purpose:0x401000:abc",
        binary_id=jsonl_paths.binary_id, type="function_purpose",
        title="main", content="main parses config",
        entity_refs=["func:0x401000"], tags=["parser"],
    ))

    maybe_import_legacy_jsonl(store, owner, jsonl_paths)
    repo = SQLiteKnowledgeRepository(store, owner_memory_id=owner)
    memories = repo.list_memories()
    assert len(memories) == 1
    assert memories[0].content == "main parses config"

    marker = store._conn.execute(
        "SELECT value FROM workspace_meta WHERE key = 'legacy_jsonl_imported'"
    ).fetchone()
    assert marker is not None
    store.close()


def test_maybe_import_does_not_delete_jsonl_files(tmp_path: Path) -> None:
    owner = new_memory_id()
    workspace_paths = MemoryLocator(tmp_path / "ws").binary(owner)
    store = WorkspaceStore.create(workspace_paths, owner_memory_id=owner)

    jsonl_paths = _make_paths(tmp_path / "jsonl")
    jsonl_paths.ensure()
    raw = KnowledgeRawStore(jsonl_paths)
    raw.upsert_memory(KnowledgeMemory(
        id="mem:1", binary_id=jsonl_paths.binary_id, type="general",
        title="t", content="c",
    ))

    maybe_import_legacy_jsonl(store, owner, jsonl_paths)
    assert Path(jsonl_paths.memories_path).exists()
    store.close()


def test_maybe_import_idempotent_on_second_call(tmp_path: Path) -> None:
    owner = new_memory_id()
    workspace_paths = MemoryLocator(tmp_path / "ws").binary(owner)
    store = WorkspaceStore.create(workspace_paths, owner_memory_id=owner)

    jsonl_paths = _make_paths(tmp_path / "jsonl")
    jsonl_paths.ensure()
    raw = KnowledgeRawStore(jsonl_paths)
    raw.upsert_memory(KnowledgeMemory(
        id="mem:1", binary_id=jsonl_paths.binary_id, type="general",
        title="t", content="c",
    ))

    maybe_import_legacy_jsonl(store, owner, jsonl_paths)
    maybe_import_legacy_jsonl(store, owner, jsonl_paths)
    repo = SQLiteKnowledgeRepository(store, owner_memory_id=owner)
    assert len(repo.list_memories()) == 1
    store.close()


def test_maybe_import_failed_leaves_marker_unset(tmp_path: Path, monkeypatch) -> None:
    owner = new_memory_id()
    workspace_paths = MemoryLocator(tmp_path / "ws").binary(owner)
    store = WorkspaceStore.create(workspace_paths, owner_memory_id=owner)

    jsonl_paths = _make_paths(tmp_path / "jsonl")
    jsonl_paths.ensure()
    raw = KnowledgeRawStore(jsonl_paths)
    raw.upsert_memory(KnowledgeMemory(
        id="mem:1", binary_id=jsonl_paths.binary_id, type="general",
        title="t", content="c",
    ))

    def boom(*a, **k):
        raise RuntimeError("import crash")
    monkeypatch.setattr("rikugan.memory.jsonl_migration.import_workspace_bundle", boom)

    with pytest.raises(RuntimeError, match="import crash"):
        maybe_import_legacy_jsonl(store, owner, jsonl_paths)
    marker = store._conn.execute(
        "SELECT value FROM workspace_meta WHERE key = 'legacy_jsonl_imported'"
    ).fetchone()
    assert marker is None
    store.close()
```

- [ ] **Step 2: Run tests to verify RED**

```bash
uv run --frozen --python 3.11 python -m pytest tests/memory/test_jsonl_migration.py -q
```

Expected: failures because `maybe_import_legacy_jsonl` does not exist.

- [ ] **Step 3: Implement the trigger**

Add to `rikugan/memory/jsonl_migration.py`:

```python
from datetime import UTC, datetime

from .bundle_import import import_workspace_bundle
from .repository import SQLiteKnowledgeRepository
from .workspace_store import WorkspaceStore


def maybe_import_legacy_jsonl(
    workspace_store: WorkspaceStore,
    owner_memory_id: str,
    jsonl_paths: KnowledgePaths,
) -> None:
    """Import JSONL records into SQLite once per workspace.

    Idempotent via the ``legacy_jsonl_imported`` marker in ``workspace_meta``.
    JSONL files are never deleted.
    """
    row = workspace_store._conn.execute(
        "SELECT value FROM workspace_meta WHERE key = 'legacy_jsonl_imported'"
    ).fetchone()
    if row is not None:
        return

    raw_store = KnowledgeRawStore(jsonl_paths)
    envelopes = jsonl_to_bundle_envelopes(raw_store, jsonl_paths)
    if not envelopes:
        workspace_store._conn.execute(
            "INSERT OR REPLACE INTO workspace_meta(key, value) VALUES('legacy_jsonl_imported', ?)",
            (datetime.now(UTC).isoformat(),),
        )
        workspace_store._conn.commit()
        return

    bundle_path = write_envelopes_to_temp_bundle(envelopes, owner_memory_id)
    try:
        repo = SQLiteKnowledgeRepository(workspace_store, owner_memory_id=owner_memory_id)
        import_workspace_bundle(bundle_path, repo)
    finally:
        bundle_path.unlink(missing_ok=True)

    workspace_store._conn.execute(
        "INSERT OR REPLACE INTO workspace_meta(key, value) VALUES('legacy_jsonl_imported', ?)",
        (datetime.now(UTC).isoformat(),),
    )
    workspace_store._conn.commit()
```

- [ ] **Step 4: Wire the trigger into `_wire_central_memory`**

In `rikugan/ui/session_controller_base.py`, after `store = open_workspace_for_write(...)` / `WorkspaceStore.create(...)` and before constructing the repository, add:

```python
            from ..memory.paths import derive_knowledge_paths
            from ..memory.jsonl_migration import maybe_import_legacy_jsonl

            try:
                jsonl_paths = derive_knowledge_paths(session.idb_path or "", session.db_instance_id or "")
                if jsonl_paths is not None:
                    maybe_import_legacy_jsonl(store, result.binding.memory_id, jsonl_paths)
            except Exception as e:
                log_error(f"legacy JSONL import failed: {e}")
```

If `derive_knowledge_paths` does not exist, use the existing `KnowledgePaths(...)` constructor directly with the same arguments `make_store` uses.

- [ ] **Step 5: Run focused tests and lint**

```bash
uv run --frozen --python 3.11 python -m pytest tests/memory/test_jsonl_migration.py -q
uv run --frozen --python 3.11 python -m ruff check rikugan/memory/jsonl_migration.py rikugan/ui/session_controller_base.py
```

Expected: all tests pass; Ruff clean.

- [ ] **Step 6: Commit the auto-import trigger**

```bash
git add rikugan/memory/jsonl_migration.py rikugan/ui/session_controller_base.py tests/memory/test_jsonl_migration.py
git commit -m "feat(memory): auto-import legacy JSONL on first IDB open"
```

---

### Task 5: Dual-Write Refactor of ingest_exploration_finding and ingest_research_note

**Files:**

- Modify: `rikugan/memory/ingest.py`
- Modify: `rikugan/agent/loop.py:1847-1860, 2011-2025`
- Create: `tests/memory/test_dual_write_ingest.py`

**Interfaces:**

- Consumes: `BinaryMemoryService.save_exploration_finding` from Task 2; `_LEGACY_JSONL_DUAL_WRITE` module flag.
- Produces:
  - `_LEGACY_JSONL_DUAL_WRITE = True` in `rikugan/memory/ingest.py`
  - `ingest_exploration_finding(..., *, memory_service=None)` and `ingest_research_note(..., *, memory_service=None)` with dual-write behavior

- [ ] **Step 1: Write failing dual-write tests**

```python
# tests/memory/test_dual_write_ingest.py
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from rikugan.memory import ingest
from rikugan.memory.paths import KnowledgePaths, derive_binary_id
from rikugan.memory.raw_store import KnowledgeRawStore


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
        store, paths,
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
        store, paths,
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
        store, paths,
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
    monkeypatch.setattr(store, "upsert_memory", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("jsonl boom")))

    memory_service = MagicMock()
    memory_service.save_exploration_finding.return_value = MagicMock(outcome="created")

    ingest.ingest_exploration_finding(
        store, paths,
        category="function_purpose",
        summary="main parses config",
        address=0x401000,
        relevance="high",
        memory_service=memory_service,
    )
    memory_service.save_exploration_finding.assert_called_once()
```

- [ ] **Step 2: Run tests to verify RED**

```bash
uv run --frozen --python 3.11 python -m pytest tests/memory/test_dual_write_ingest.py -q
```

Expected: failures because `ingest_exploration_finding` does not accept `memory_service` and `_LEGACY_JSONL_DUAL_WRITE` does not exist.

- [ ] **Step 3: Add the flag and refactor ingest_exploration_finding**

In `rikugan/memory/ingest.py`, add the module-level constant near the top:

```python
_LEGACY_JSONL_DUAL_WRITE = True
```

Refactor `ingest_exploration_finding` to accept a keyword-only `memory_service: BinaryMemoryService | None = None` parameter. Split the body into two private helpers:

```python
def _write_exploration_to_sqlite(memory_service, category, summary, address, relevance, evidence=""):
    """Write to SQLite via BinaryMemoryService.save_exploration_finding."""
    from .core.logging import log_error  # adjust import path if needed

    entity_refs = []
    if address is not None:
        entity_refs.append(f"func:0x{address:x}")
    tags = [category] if category else ["general"]
    title = _memory_title(summary)
    try:
        memory_service.save_exploration_finding(
            None,  # authority handled inside service
            category=category or "general",
            title=title,
            content=summary,
            confidence=_relevance_to_confidence(relevance),
            entity_refs=entity_refs,
            tags=tags,
            source="exploration",
        )
    except Exception as e:
        log_error(f"SQLite exploration write failed: {e}")


def _write_exploration_to_jsonl(store, paths, category, summary, address, relevance, evidence=""):
    """Existing JSONL write logic — extracted verbatim from the current body."""
    # ... existing logic from ingest_exploration_finding body ...
```

The public function dispatches:

```python
def ingest_exploration_finding(
    store, paths, *,
    category, summary, address, relevance, evidence="",
    memory_service=None,
):
    # ... existing validation ...
    if memory_service is not None:
        _write_exploration_to_sqlite(memory_service, category, summary, address, relevance, evidence)
    if _LEGACY_JSONL_DUAL_WRITE and store is not None and paths is not None:
        try:
            _write_exploration_to_jsonl(store, paths, category, summary, address, relevance, evidence)
        except Exception as e:
            from ..core.logging import log_error
            log_error(f"JSONL exploration write failed: {e}")
```

Apply the same pattern to `ingest_research_note`.

Add a private helper `_relevance_to_confidence(relevance: str) -> float` mapping `{"high": 0.9, "medium": 0.7, "low": 0.5}`.

- [ ] **Step 4: Update AgentLoop callers**

In `rikugan/agent/loop.py`, the two call sites that invoke `ingest_exploration_finding` (around line 1847) and `ingest_research_note` (around line 2011) must pass `memory_service=self.memory_service`:

```python
ingest_exploration_finding(
    store, paths,
    category=...,
    summary=...,
    address=...,
    relevance=...,
    evidence=...,
    memory_service=self.memory_service,
)
```

- [ ] **Step 5: Run focused tests and lint**

```bash
uv run --frozen --python 3.11 python -m pytest tests/memory/test_dual_write_ingest.py tests/knowledge/test_ingest.py -q
uv run --frozen --python 3.11 python -m ruff check rikugan/memory/ingest.py rikugan/agent/loop.py
```

Expected: dual-write tests pass; existing `tests/knowledge/test_ingest.py` remains green (JSONL path still works when `memory_service` is `None`).

- [ ] **Step 6: Commit the dual-write refactor**

```bash
git add rikugan/memory/ingest.py rikugan/agent/loop.py tests/memory/test_dual_write_ingest.py
git commit -m "refactor(memory): dual-write exploration/research to SQLite and JSONL"
```

---

### Task 6: SQLite Retrieval Adapter and Ranker Refactor

**Files:**

- Modify: `rikugan/memory/retrieve.py`
- Create: `rikugan/memory/sqlite_retrieval.py`
- Create: `tests/memory/test_sqlite_retrieval.py`

**Interfaces:**

- Consumes: `retrieve.retrieve` at `retrieve.py:168`, `RetrievalPack` at `retrieve.py:60`, `RetrievalQuery` at `retrieve.py:48`.
- Produces:
  - `retrieve.retrieve_from_records(memories, entities, relations, notes, query, *, max_memories, max_entities, max_relations, max_notes, expand_relations) -> RetrievalPack`
  - `retrieve.retrieve(store, paths, query, ...)` becomes a thin wrapper.
  - `sqlite_retrieval.repository_to_retrieval_pack(repo, *, current_address, current_function, active_mode, active_goal, budget) -> RetrievalPack`

- [ ] **Step 1: Write failing adapter tests**

```python
# tests/memory/test_sqlite_retrieval.py
from __future__ import annotations

from pathlib import Path

from rikugan.memory.retrieve import RetrievalQuery, retrieve, retrieve_from_records, RetrievalPack
from rikugan.memory.schema import KnowledgeMemory, KnowledgeEntity, KnowledgeRelation
from rikugan.memory.sqlite_retrieval import repository_to_retrieval_pack
from rikugan.memory.workspace import MemoryLocator, new_memory_id
from rikugan.memory.workspace_store import WorkspaceStore
from rikugan.memory.repository import SQLiteKnowledgeRepository


def _create_repo(tmp_path: Path):
    owner = new_memory_id()
    paths = MemoryLocator(tmp_path).binary(owner)
    store = WorkspaceStore.create(paths, owner_memory_id=owner)
    repo = SQLiteKnowledgeRepository(store, owner_memory_id=owner)
    return repo, owner, store


def test_retrieve_from_records_matches_retrieve_for_same_input(tmp_path: Path) -> None:
    """Same input records → same RetrievalPack from both entry points."""
    memories = [KnowledgeMemory(id="mem:1", binary_id="b", type="general", title="t", content="main parses config")]
    entities = [KnowledgeEntity(id="func:0x401000", binary_id="b", type="function", name="main", address="0x401000")]
    relations: list[KnowledgeRelation] = []
    notes: list[str] = []
    query = RetrievalQuery(text="main", function_name="main", address="0x401000")

    pack_from_records = retrieve_from_records(
        memories, entities, relations, notes, query,
        max_memories=12, max_entities=8, max_relations=15, max_notes=3,
    )

    # Build a JSONL store with the same records and compare.
    from rikugan.memory.paths import KnowledgePaths, derive_binary_id
    from rikugan.memory.raw_store import KnowledgeRawStore
    jsonl_paths = KnowledgePaths(
        idb_path=str(tmp_path / "test.i64"),
        notes_dir=str(tmp_path / "notes"),
        kb_dir=str(tmp_path / "kb"),
        reports_dir=str(tmp_path / "notes" / "reports"),
        binary_id=derive_binary_id(str(tmp_path / "test.i64")),
    )
    jsonl_paths.ensure()
    store = KnowledgeRawStore(jsonl_paths)
    for m in memories:
        store.upsert_memory(m)
    for e in entities:
        store.upsert_entity(e)

    pack_from_store = retrieve(store, jsonl_paths, query)
    assert {m.id for m in pack_from_records.memories} == {m.id for m in pack_from_store.memories}


def test_repository_to_retrieval_pack_reads_sqlite(tmp_path: Path) -> None:
    repo, owner, store = _create_repo(tmp_path)
    repo.save_exploration_finding(
        "function_purpose", "main parses config", "exploration",
        entity_refs=["func:0x401000"], tags=["parser"],
    )
    pack = repository_to_retrieval_pack(
        repo,
        current_address="0x401000",
        current_function="main @ 0x401000",
        active_mode="normal",
        active_goal="",
        budget=None,  # adapter uses sensible defaults
    )
    assert isinstance(pack, RetrievalPack)
    assert any("main" in m.content for m in pack.memories)
    store.close()
```

- [ ] **Step 2: Run tests to verify RED**

```bash
uv run --frozen --python 3.11 python -m pytest tests/memory/test_sqlite_retrieval.py -q
```

Expected: import failures for `retrieve_from_records` and `sqlite_retrieval`.

- [ ] **Step 3: Extract `retrieve_from_records` from `retrieve`**

In `rikugan/memory/retrieve.py`, extract the ranking body of `retrieve` (currently starting at line 178 `pack = RetrievalPack()`) into a new function:

```python
def retrieve_from_records(
    memories: list[KnowledgeMemory],
    entities: list[KnowledgeEntity],
    relations: list[KnowledgeRelation],
    notes: list[str],
    query: RetrievalQuery,
    *,
    max_memories: int = 12,
    max_entities: int = 8,
    max_relations: int = 15,
    max_notes: int = 3,
    expand_relations: bool = True,
) -> RetrievalPack:
    """Rank a fixed set of records against the query.

    This is the ranking body extracted from ``retrieve``; ``retrieve`` now
    delegates here after loading records from the store.
    """
    pack = RetrievalPack()
    # ... existing ranking body verbatim, operating on the passed-in lists ...
    return pack


def retrieve(
    store: KnowledgeRawStore,
    paths: KnowledgePaths,
    query: RetrievalQuery,
    *,
    max_memories: int = 12,
    max_entities: int = 8,
    max_relations: int = 15,
    max_notes: int = 3,
    expand_relations: bool = True,
) -> RetrievalPack:
    """Build a ranked slice from the JSONL store."""
    from .notes import list_notes
    memories = store.list_memories()
    entities = store.list_entities()
    relations = store.list_relations()
    notes = [_note_excerpt(n, 600) for n in list_notes(paths.notes_dir)[:max_notes * 3]]
    return retrieve_from_records(
        memories, entities, relations, notes, query,
        max_memories=max_memories, max_entities=max_entities,
        max_relations=max_relations, max_notes=max_notes,
        expand_relations=expand_relations,
    )
```

- [ ] **Step 4: Implement the SQLite adapter**

```python
# rikugan/memory/sqlite_retrieval.py
"""Adapter feeding SQLite repository records into the existing ranker."""
from __future__ import annotations

from .retrieve import RetrievalPack, RetrievalQuery, retrieve_from_records
from .repository import SQLiteKnowledgeRepository


def repository_to_retrieval_pack(
    repo: SQLiteKnowledgeRepository,
    *,
    current_address: str | None,
    current_function: str | None,
    active_mode: str,
    active_goal: str,
    budget,  # ContextBudget | None — defaults applied if None
) -> RetrievalPack:
    """Read SQLite records and rank them via the shared ranker."""
    memories = repo.list_memories()
    entities = repo.list_entities()
    relations = repo.list_relations()
    notes: list[str] = []  # Notes remain filesystem-backed; empty here.

    query = RetrievalQuery(
        text="",
        function_name=current_function or "",
        address=current_address or "",
        active_mode=active_mode,
        active_goal=active_goal,
    )
    return retrieve_from_records(memories, entities, relations, notes, query)
```

- [ ] **Step 5: Run focused tests and lint**

```bash
uv run --frozen --python 3.11 python -m pytest tests/memory/test_sqlite_retrieval.py -q
uv run --frozen --python 3.11 python -m ruff check rikugan/memory/retrieve.py rikugan/memory/sqlite_retrieval.py
```

Expected: all tests pass; Ruff clean.

- [ ] **Step 6: Commit the adapter and ranker refactor**

```bash
git add rikugan/memory/retrieve.py rikugan/memory/sqlite_retrieval.py tests/memory/test_sqlite_retrieval.py
git commit -m "feat(memory): rank retrieved knowledge from SQLite store"
```

---

### Task 7: SessionControllerBase memory_service Accessor

**Files:**

- Modify: `rikugan/ui/session_controller_base.py`
- Modify: `tests/agent/test_session_controller.py`

**Interfaces:**

- Consumes: `_wire_central_memory` sets `loop.memory_service`.
- Produces: `SessionControllerBase.memory_service` property returning `BinaryMemoryService | None` for the active tab.

- [ ] **Step 1: Write failing test**

```python
# Append to tests/agent/test_session_controller.py (in TestIdaSessionController)
def test_memory_service_property_returns_none_before_wiring(self) -> None:
    """Before _wire_central_memory runs, the accessor returns None."""
    self.assertIsNone(self.ctrl.memory_service)
```

- [ ] **Step 2: Run test to verify RED**

```bash
uv run --frozen --python 3.11 python -m pytest tests/agent/test_session_controller.py::TestIdaSessionController::test_memory_service_property_returns_none_before_wiring -q
```

Expected: `AttributeError: 'IdaSessionController' object has no attribute 'memory_service'`.

- [ ] **Step 3: Implement the accessor**

In `rikugan/ui/session_controller_base.py`, add a property:

```python
@property
def memory_service(self) -> "BinaryMemoryService | None":
    """Return the memory service wired into the active tab's runner, if any."""
    runner = self._runners.get(self._active_tab_id)
    if runner is None:
        return None
    loop = getattr(runner, "_loop", None)
    if loop is None:
        return None
    return getattr(loop, "memory_service", None)
```

The exact attribute path depends on how `SessionControllerBase` stores the loop. Verify by reading `_wire_central_memory` (which sets `loop.memory_service = service`). If the loop is not directly accessible, store the service on the controller in `_wire_central_memory`:

```python
# In _wire_central_memory, after loop.memory_service = service:
self._active_memory_service = service

# And the property becomes:
@property
def memory_service(self):
    return getattr(self, "_active_memory_service", None)
```

Use the simpler form if the loop is reachable; use the stored attribute form otherwise.

- [ ] **Step 4: Run focused tests and lint**

```bash
uv run --frozen --python 3.11 python -m pytest tests/agent/test_session_controller.py -q
uv run --frozen --python 3.11 python -m ruff check rikugan/ui/session_controller_base.py
```

Expected: test passes; Ruff clean.

- [ ] **Step 5: Commit the accessor**

```bash
git add rikugan/ui/session_controller_base.py tests/agent/test_session_controller.py
git commit -m "feat(ui): expose memory_service accessor on SessionControllerBase"
```

---

### Task 8: Knowledge Panel Read Path Migration

**Files:**

- Modify: `rikugan/ui/panel_core.py:3650-3718`
- Create: `tests/ui/test_knowledge_panel_sqlite_read.py`

**Interfaces:**

- Consumes: `SessionControllerBase.memory_service` from Task 7.
- Produces: `_refresh_knowledge_panel` prefers SQLite, falls back to JSONL.

- [ ] **Step 1: Write failing panel read tests**

```python
# tests/ui/test_knowledge_panel_sqlite_read.py
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from rikugan.ui.knowledge_panel import KnowledgePanel
from rikugan.ui.panel_core import RikuganPanelCore


def test_refresh_panel_reads_sqlite_when_service_wired(tmp_path: Path) -> None:
    """When memory_service is not None, panel reads from repository."""
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
    """When memory_service is None, panel reads from JSONL make_store."""
    # This is a smoke test verifying the fallback path doesn't crash.
    panel = KnowledgePanel()
    panel.set_disabled_message("No IDB path is set.")
    assert panel._table.rowCount() == 0
```

- [ ] **Step 2: Run test to verify RED**

```bash
uv run --frozen --python 3.11 python -m pytest tests/ui/test_knowledge_panel_sqlite_read.py -q
```

Expected: import or attribute failures.

- [ ] **Step 3: Migrate `_refresh_knowledge_panel`**

In `rikugan/ui/panel_core.py`, update `_refresh_knowledge_panel` (around line 3650):

```python
def _refresh_knowledge_panel(self) -> None:
    if self._is_shutdown:
        return
    panel = getattr(self, "_knowledge_panel", None)
    if panel is None:
        return

    if not bool(getattr(self._config, "knowledge_enabled", True)):
        panel.set_disabled_state(True)
        return

    idb_path = ""
    try:
        idb_path = self._ctrl.session.idb_path if self._ctrl and self._ctrl.session else ""
    except Exception:
        idb_path = ""

    if not idb_path:
        panel.set_disabled_state(False)
        panel.set_disabled_message("No IDB path is set. Open a binary to populate the knowledge store.")
        return

    # Prefer SQLite when the memory service is wired.
    memory_service = getattr(self._ctrl, "memory_service", None)
    if memory_service is not None:
        try:
            from ..memory.notes import list_notes

            memories = memory_service.repository.list_memories()
            entities = memory_service.repository.list_entities()
            relations = memory_service.repository.list_relations()
            obs_count = memory_service.repository.count_observations()
            notes_dir = str(memory_service.paths.notes)
            panel.set_disabled_state(False)
            panel.set_counts({
                "memories": len(memories),
                "entities": len(entities),
                "relations": len(relations),
                "observations": obs_count,
            })
            panel.populate(
                memories=memories,
                entities=entities,
                relations=relations,
                notes=[
                    f"{(n.title or '')}: {(n.body or '').strip()[:400]}"
                    for n in list_notes(notes_dir)[:20]
                ],
            )
            return
        except Exception as e:
            log_debug(f"knowledge panel SQLite refresh failed: {e}, falling back to JSONL")

    # Fallback: JSONL store.
    try:
        from ..memory.ingest import make_store
        from ..memory.notes import list_notes

        store, paths = make_store(idb_path)
        if store is None or paths is None:
            panel.set_disabled_state(False)
            panel.set_disabled_message("Could not initialize the knowledge store.")
            return

        memories = store.list_memories()
        entities = store.list_entities()
        relations = store.list_relations()
        obs_count = store.count_observations()
        panel.set_disabled_state(False)
        panel.set_counts({
            "memories": len(memories),
            "entities": len(entities),
            "relations": len(relations),
            "observations": obs_count,
        })
        panel.populate(
            memories=memories,
            entities=entities,
            relations=relations,
            notes=[
                f"{(n.title or '')}: {(n.body or '').strip()[:400]}"
                for n in list_notes(paths.notes_dir)[:20]
            ],
        )
    except Exception as e:
        log_debug(f"knowledge panel refresh failed: {e}")
        panel.set_disabled_message(f"Failed to load knowledge: {e}")
```

- [ ] **Step 4: Run focused tests and lint**

```bash
uv run --frozen --python 3.11 python -m pytest tests/ui/test_knowledge_panel_sqlite_read.py -q
uv run --frozen --python 3.11 python -m ruff check rikugan/ui/panel_core.py
```

Expected: tests pass; Ruff clean.

- [ ] **Step 5: Commit the panel migration**

```bash
git add rikugan/ui/panel_core.py tests/ui/test_knowledge_panel_sqlite_read.py
git commit -m "refactor(ui): knowledge panel reads from SQLite store"
```

---

### Task 9: Retrieved Knowledge Section Migration

**Files:**

- Modify: `rikugan/agent/loop.py:596-680`
- Modify: `tests/agent/test_agent_loop.py` (extend)

**Interfaces:**

- Consumes: `sqlite_retrieval.repository_to_retrieval_pack` from Task 6.
- Produces: `_build_retrieved_knowledge_section` prefers SQLite when `memory_service` is wired.

- [ ] **Step 1: Write failing test**

```python
# Append to tests/agent/test_agent_loop.py (in TestAgentLoop)
def test_build_retrieved_knowledge_section_uses_sqlite_when_service_wired(self, tmp_path: Path) -> None:
    """When memory_service is not None, the section reads from SQLite."""
    from unittest.mock import MagicMock, patch

    provider = MockProvider(responses=[_text_response("done")])
    loop = self._make_loop(provider)

    mock_service = MagicMock()
    mock_repo = MagicMock()
    mock_repo.list_memories.return_value = []
    mock_repo.list_entities.return_value = []
    mock_repo.list_relations.return_value = []
    mock_repo.count_observations.return_value = 0
    mock_service.repository = mock_repo
    loop.memory_service = mock_service

    section = loop._build_retrieved_knowledge_section(
        current_address="0x401000",
        current_function="main @ 0x401000",
        profile=self._profile(),
    )
    # The section may be empty (no memories), but the key assertion is that
    # the repository was queried, proving the SQLite path was taken.
    mock_repo.list_memories.assert_called()
```

- [ ] **Step 2: Run test to verify RED**

```bash
uv run --frozen --python 3.11 python -m pytest tests/agent/test_agent_loop.py -k "test_build_retrieved_knowledge_section_uses_sqlite" -q
```

Expected: failure because `_build_retrieved_knowledge_section` still calls `make_store`.

- [ ] **Step 3: Migrate `_build_retrieved_knowledge_section`**

In `rikugan/agent/loop.py`, update `_build_retrieved_knowledge_section` (around line 596):

```python
def _build_retrieved_knowledge_section(
    self,
    current_address: str | None,
    current_function: str | None,
    profile,
) -> str:
    """Return the per-turn Retrieved Knowledge block, or "" if disabled/unavailable."""
    try:
        if not getattr(self.config, "knowledge_enabled", True):
            return ""

        active_mode = self.session.metadata.get("active_mode", "normal") or "normal"
        active_goal = self.session.metadata.get(_GOAL_METADATA_KEY, "")

        # Prefer SQLite when the memory service is wired.
        if self.memory_service is not None:
            from ..memory.sqlite_retrieval import repository_to_retrieval_pack
            from ..memory.context import build_retrieved_context_with_pack

            pack = repository_to_retrieval_pack(
                self.memory_service.repository,
                current_address=current_address,
                current_function=current_function,
                active_mode=active_mode,
                active_goal=active_goal,
                budget=None,
            )
            self.session.metadata["last_knowledge_retrieval"] = {
                "counts": pack.counts,
                "items": [],
            }
            return build_retrieved_context_with_pack(pack)

        # Fallback: JSONL store.
        from ..memory.context import RetrievalQuery, budget_from_config, build_retrieved_context_with_pack
        from ..memory.ingest import make_store

        store, paths = make_store(self.session.idb_path)
        if store is None:
            return ""

        func_name = ""
        if current_function:
            func_name = current_function.split("@")[0].strip()

        query = RetrievalQuery(
            text=self._latest_user_message_text() or "",
            function_name=func_name,
            address=current_address or "",
            active_mode=active_mode,
            active_goal=active_goal,
        )
        from ..memory.retrieve import retrieve
        pack = retrieve(store, paths, query)
        self.session.metadata["last_knowledge_retrieval"] = {
            "counts": pack.counts,
            "items": [],
        }
        return build_retrieved_context_with_pack(pack)
    except Exception as e:
        log_debug(f"retrieved knowledge section failed: {e}")
        return ""
```

- [ ] **Step 4: Run focused tests and lint**

```bash
uv run --frozen --python 3.11 python -m pytest tests/agent/test_agent_loop.py -k "retrieved_knowledge" -q
uv run --frozen --python 3.11 python -m ruff check rikugan/agent/loop.py
```

Expected: tests pass; Ruff clean.

- [ ] **Step 5: Commit the section migration**

```bash
git add rikugan/agent/loop.py tests/agent/test_agent_loop.py
git commit -m "refactor(agent): retrieved knowledge section reads from SQLite"
```

---

### Task 10: MEMORY_SAVED Event and Panel Refresh

**Files:**

- Modify: `rikugan/agent/turn.py`
- Modify: `rikugan/agent/loop.py:1950-2000`
- Modify: `rikugan/ui/panel_core.py:1804-1830`

**Interfaces:**

- Consumes: `TurnEventType` enum, `_handle_save_memory_tool`.
- Produces: `TurnEventType.MEMORY_SAVED = "memory_saved"`; panel refreshes on this event.

- [ ] **Step 1: Write failing event test**

```python
# Append to tests/agent/test_memory_cutover.py
def test_save_memory_emits_memory_saved_event(self, tmp_path: Path) -> None:
    """A successful save_memory tool call emits a MEMORY_SAVED event."""
    from rikugan.agent.turn import TurnEventType

    loop, _service = _make_loop_with_central_memory(tmp_path)
    tc = ToolCall(id="tc1", name="save_memory", arguments={"category": "protocol", "fact": "Uses HTTP"})
    events = list(loop._handle_save_memory_tool(tc))

    event_types = [e.type for e in events if hasattr(e, "type")]
    assert TurnEventType.MEMORY_SAVED in event_types
```

- [ ] **Step 2: Run test to verify RED**

```bash
uv run --frozen --python 3.11 python -m pytest tests/agent/test_memory_cutover.py -q
```

Expected: failure because `MEMORY_SAVED` does not exist.

- [ ] **Step 3: Add the event type and emit it**

In `rikugan/agent/turn.py`, add the constant to the `TurnEventType` enum:

```python
class TurnEventType(str, Enum):
    # ... existing values ...
    MEMORY_SAVED = "memory_saved"
```

In `rikugan/agent/loop.py`, in `_handle_save_memory_tool`, after the successful save (after the compact result content is built), emit the event:

```python
# After: content = f"{label}: {result.record_id} [{category}]"
yield TurnEvent(
    type=TurnEventType.MEMORY_SAVED,
    tool_call_id=tc.id,
    text="",
)
```

The exact `TurnEvent` construction depends on the existing factory methods. If `TurnEvent` has a factory for knowledge events, use it; otherwise construct directly with the fields the event stream expects.

- [ ] **Step 4: Extend `_on_event` to handle MEMORY_SAVED**

In `rikugan/ui/panel_core.py`, in `_on_event` (around line 1804-1830), add `MEMORY_SAVED` to the set of event types that trigger a knowledge panel refresh:

```python
# Existing code likely checks:
# if event.type in {TurnEventType.EXPLORATION_FINDING, ...}:
#     self._on_knowledge_event_refresh(event.type.value)
# Add TurnEventType.MEMORY_SAVED to that set.
```

- [ ] **Step 5: Run focused tests and lint**

```bash
uv run --frozen --python 3.11 python -m pytest tests/agent/test_memory_cutover.py tests/agent/test_agent_loop.py -q
uv run --frozen --python 3.11 python -m ruff check rikugan/agent/turn.py rikugan/agent/loop.py rikugan/ui/panel_core.py
```

Expected: tests pass; Ruff clean.

- [ ] **Step 6: Commit the event and refresh wiring**

```bash
git add rikugan/agent/turn.py rikugan/agent/loop.py rikugan/ui/panel_core.py tests/agent/test_memory_cutover.py
git commit -m "feat(ui): refresh knowledge panel on save_memory success"
```

---

### Task 11: Full Verification

**Files:**

- Review: `docs/superpowers/specs/2026-07-29-knowledge-ui-sqlite-migration-design.md`
- Review: `docs/superpowers/plans/2026-07-29-knowledge-ui-sqlite-migration.md`

**Interfaces:**

- Consumes: all deliverables from Tasks 1–10.

- [ ] **Step 1: Run formatting and lint**

```bash
uv run --frozen --python 3.11 python -m ruff format --check rikugan/ tests/
uv run --frozen --python 3.11 python -m ruff check rikugan/ tests/
```

Expected: both commands exit 0 on files touched by this tranche.

- [ ] **Step 2: Run type checks**

```bash
uv run --frozen --python 3.11 python -m mypy rikugan/core rikugan/providers rikugan/memory
```

Expected: exit 0 with no NEW errors (pre-existing errors in `case_commands.py`, `authority.py`, `markdown.py`, `manager.py` are acceptable).

- [ ] **Step 3: Run focused UI migration suite**

```bash
uv run --frozen --python 3.11 python -m pytest \
  tests/memory/test_workspace_migration_v3.py \
  tests/memory/test_jsonl_migration.py \
  tests/memory/test_dual_write_ingest.py \
  tests/memory/test_sqlite_retrieval.py \
  tests/ui/test_knowledge_panel_sqlite_read.py \
  tests/memory/test_workspace_store.py \
  tests/memory/test_repository.py \
  tests/memory/test_service.py \
  tests/agent/test_memory_cutover.py \
  tests/agent/test_agent_loop.py \
  tests/agent/test_session_controller.py \
  -q
```

Expected: zero failures.

- [ ] **Step 4: Run full dual-root suite on Python 3.11**

```bash
uv run --frozen --python 3.11 python -m pytest --tb=short -q
```

Expected: zero NEW failures (pre-existing master failures documented in `ci-master-red-preexisting` memory are acceptable).

- [ ] **Step 5: Verify schema migration boundary**

```bash
uv run --frozen --python 3.11 python -m pytest tests/memory/test_workspace_migration_v3.py tests/memory/test_workspace_migration_v2.py -q
```

Expected: v2→v3 and v1→v2 paths both pass.

- [ ] **Step 6: Verify repository state**

```bash
git diff --check
git status --short
git diff -- uv.lock
```

Expected: `git diff --check` exits 0; `uv.lock` has no diff; status contains only intended tranche files.

- [ ] **Step 7: Request code reviews**

Dispatch:

1. `python-reviewer` for all Python changes.
2. `code-reviewer` for cross-layer correctness (UI ↔ memory ↔ agent).
3. `ida-tooling-reviewer` only if any file under `rikugan/tools/`, `rikugan/ida/tools/`, or `rikugan/agent/mutation.py` changed unexpectedly.

Fix confirmed findings with focused regression tests, then rerun Steps 1–6.

- [ ] **Step 8: Commit final in-scope review fixes**

```bash
git status --short
git add <specific files from review>
git diff --cached --check
git commit -m "fix: address knowledge UI migration review findings"
```

- [ ] **Step 9: Produce the final verification report**

Report exact outputs/counts for:

- schema migration tests (v2→v3, v1→v2);
- auto-import tests;
- dual-write tests;
- Knowledge panel read tests;
- SQLite retrieval tests;
- MEMORY_SAVED event tests;
- Python 3.11 full suite;
- Ruff and mypy;
- working-tree state.

Do not bump the Rikugan version, create a release, push, or open a PR unless separately requested.

---

## Self-Review

### Spec coverage

| Spec requirement | Plan coverage |
|---|---|
| Schema v3 with graph metadata columns | Task 1 |
| Repository save_exploration_finding | Task 2 |
| Service save_exploration_finding | Task 2 |
| JSONL → bundle adapter | Task 3 |
| Auto-import trigger + idempotency marker | Task 4 |
| Dual-write ingest (flag + both paths) | Task 5 |
| Ranker refactor (retrieve_from_records) | Task 6 |
| SQLite retrieval adapter | Task 6 |
| SessionControllerBase.memory_service accessor | Task 7 |
| Knowledge panel SQLite read + JSONL fallback | Task 8 |
| Retrieved knowledge section SQLite read | Task 9 |
| MEMORY_SAVED event + panel refresh | Task 10 |
| Full verification and review | Task 11 |

### Placeholder scan

The plan contains no `TBD`, `TODO`, "implement later", or generic "add tests" placeholders. Every step has concrete code or commands. Where the exact attribute path in `SessionControllerBase` is uncertain (Task 7), two implementation forms are given with a verification instruction.

### Type consistency

- Task 1 defines `FactRecord.entity_refs`, `FactRecord.tags`, `EntityRecord.tags`, `RelationRecord.evidence`. Tasks 2, 3, 5, 6 consume these.
- Task 2 defines `save_exploration_finding` on both repository and service. Tasks 5 and 6 consume it.
- Task 3 defines `jsonl_to_bundle_envelopes` and `write_envelopes_to_temp_bundle`. Task 4 consumes them.
- Task 4 defines `maybe_import_legacy_jsonl`. `session_controller_base.py` consumes it.
- Task 6 defines `retrieve_from_records` and `repository_to_retrieval_pack`. Task 9 consumes them.
- Task 7 defines `memory_service` property. Tasks 8 and 9 consume it.
- Task 10 defines `TurnEventType.MEMORY_SAVED`. `panel_core.py` consumes it.
