# Memory Durability and Orchestra Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent same-category memory loss, make exact memory saves atomic and idempotent, protect workspace migration with verified backups, make bundle re-import truly idempotent, gate unsafe Orchestra before side effects, and enforce both test roots in CI.

**Architecture:** Add a pure canonical-fact helper and workspace schema v2 with current semantic hashes. Keep `put_fact()` for explicit ID-based revision writes, add one transaction-owning exact-save primitive for model saves, and route production writable opens through a backup-aware helper before automatic migration. Keep bundle schema v1 but derive deterministic target-scoped import IDs. Gate `/orchestra` at the earliest `AgentLoop.run()` command boundary and use pytest `testpaths` as the single collection source.

**Tech Stack:** Python 3.11+, SQLite WAL/`BEGIN IMMEDIATE`, pytest, PySide6 test fixtures, GitHub Actions, Bash local CI.

## Global Constraints

- `MEMORY_WORKSPACE_SCHEMA_VERSION` becomes exactly `2`; bundle schema remains exactly `1`.
- `fact_type` is taxonomy only and must never select an implicit update target.
- Exact dedup uses SHA-256 over a length-delimited canonical category/content encoding, then verifies canonical equality after hash lookup.
- Migration preserves all existing fact IDs, revisions, observations, timestamps, confidence values, entities, relations, and legacy duplicates.
- Every v2 current fact has exactly one 64-character lowercase hexadecimal `semantic_hash`.
- Exact lookup, optional insert, and observation append execute in one `BEGIN IMMEDIATE` transaction.
- Production writable v1 opens require a uniquely named, verified backup before `WorkspaceStore.open()` can migrate.
- `/orchestra` is default-disabled and exits before skill resolution, session append, prompt/schema construction, retrieval, provider calls, tools, or child agents.
- `pyproject.toml` owns `testpaths = ["tests", "rikugan/tests"]`; CI must not pass a narrower positional test root.
- No live provider/model calls are added to CI.
- Follow TDD for each task: failing test, observed failure, minimal implementation, passing focused tests, then commit.
- Do not edit `uv.lock` as a side effect of test commands; use `uv run --frozen` where applicable.
- Do not implement `EffectiveToolPolicy`, compaction rewrites, prompt trust-boundary hardening, raw-knowledge consolidation, provider parity, or the Orchestra rewrite in this plan.

---

## File Structure

### New files

- `rikugan/memory/fact_identity.py` — pure Unicode canonicalization, semantic hashing, deterministic imported-record ID derivation; no store/repository imports.
- `rikugan/memory/workspace_open.py` — inspect schema, verify/create migration backup, backup-aware production open, offline v1 rollback.
- `tests/memory/test_fact_identity.py` — canonicalization/hash/collision-boundary contract.
- `tests/memory/test_workspace_migration_v2.py` — handcrafted v1 fixtures, v2 backfill/rollback/hash guards.
- `tests/memory/test_workspace_open.py` — backup-before-open, verification failure, unique names, offline rollback, call-site routing.
- `tests/test_pytest_collection_roots.py` — deterministic assertion that representative tests from both configured roots are collected.

### Modified production files

- `rikugan/constants.py` — workspace schema version 2.
- `rikugan/memory/workspace_store.py` — v2 migration, `FactRecord.semantic_hash`, hash-aware `put_fact()`, atomic exact-save primitive.
- `rikugan/memory/repository.py` — canonical exact-save mapping and removal of category overwrite.
- `rikugan/memory/service.py` — `SaveMemoryResult.outcome` and projection-preserving save response.
- `rikugan/memory/backup.py` — collision-resistant exclusive backups, verification, version-preserving offline rollback primitive.
- `rikugan/memory/workspace.py` — backup directory locator.
- `rikugan/ui/session_controller_base.py` — backup-aware writable binary open.
- `rikugan/memory/case_service.py` — backup-aware case open and hash-preserving promotion write.
- `rikugan/memory/case_repository.py` — backup-aware writable case open; read-only/list access remains non-migrating where applicable.
- `rikugan/memory/bundle_import.py` — deterministic target-scoped record IDs and record-count idempotency.
- `rikugan/agent/loop.py` — compact `save_memory` result and early Orchestra gate.
- `pyproject.toml` — pytest `testpaths`.
- `.github/workflows/ci.yml` — invoke pytest without positional root.
- `.github/workflows/release.yml` — same collection contract.
- `ci-local.sh` — same collection contract.
- `README.md` and `ARCHITECTURE.md` — Orchestra marked temporarily disabled/experimental.

### Modified tests

- `tests/memory/test_workspace_store.py`
- `tests/memory/test_repository.py`
- `tests/memory/test_service.py`
- `tests/memory/test_backup.py`
- `tests/memory/test_bundle_import.py`
- `tests/memory/test_bundle_export.py`
- `tests/memory/test_markdown.py`
- `tests/memory/test_case_service.py`
- `tests/memory/test_case_repository.py`
- `tests/memory/test_first_open_regression.py`
- `tests/agent/test_memory_cutover.py`
- `tests/agent/test_a2a_mode.py`
- `tests/agent/test_agent_loop.py`

---

### Task 1: Pure Fact Identity Contract

**Files:**

- Create: `rikugan/memory/fact_identity.py`
- Create: `tests/memory/test_fact_identity.py`

**Interfaces:**

- Consumes: Python `unicodedata`, `hashlib` only.
- Produces:
  - `canonicalize_fact_type(value: str) -> str`
  - `canonicalize_fact_content(value: str) -> str`
  - `semantic_fact_hash(fact_type: str, content: str) -> str`
  - `deterministic_import_record_id(target_memory_id: str, import_id: str, record_type: str, origin_record_id: str) -> str`

- [ ] **Step 1: Write failing canonicalization and ID tests**

```python
# tests/memory/test_fact_identity.py
from __future__ import annotations

import re

import pytest

from rikugan.memory.fact_identity import (
    canonicalize_fact_content,
    canonicalize_fact_type,
    deterministic_import_record_id,
    semantic_fact_hash,
)


def test_fact_type_normalizes_unicode_whitespace_and_case() -> None:
    assert canonicalize_fact_type("  Function Purpose  ") == "function purpose"


def test_fact_content_normalizes_line_endings_nfc_and_outer_space() -> None:
    assert canonicalize_fact_content("  Café\r\nLine 2\r  ") == "Café\nLine 2"


def test_fact_content_preserves_internal_case_spacing_punctuation_and_order() -> None:
    assert semantic_fact_hash("note", "A  B!") != semantic_fact_hash("note", "a B!")
    assert semantic_fact_hash("note", "first\nsecond") != semantic_fact_hash("note", "second\nfirst")


@pytest.mark.parametrize("value", ["", "   ", "\r\n"])
def test_empty_canonical_values_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        canonicalize_fact_type(value)
    with pytest.raises(ValueError):
        canonicalize_fact_content(value)


def test_semantic_hash_is_stable_lowercase_sha256() -> None:
    first = semantic_fact_hash(" Function Purpose ", " Uses RC4\r\n")
    second = semantic_fact_hash("function   purpose", "Uses RC4\n")
    assert first == second
    assert re.fullmatch(r"[0-9a-f]{64}", first)


def test_import_record_id_is_target_scoped_and_validator_shaped() -> None:
    first = deterministic_import_record_id("mem-" + "a" * 32, "import-1234", "fact", "origin-1")
    again = deterministic_import_record_id("mem-" + "a" * 32, "import-1234", "fact", "origin-1")
    other_target = deterministic_import_record_id("mem-" + "b" * 32, "import-1234", "fact", "origin-1")
    assert first == again
    assert first != other_target
    assert re.fullmatch(r"fact-[0-9a-f]{32}", first)
```

- [ ] **Step 2: Run the tests to verify RED**

Run:

```bash
uv run --frozen --python 3.11 python -m pytest tests/memory/test_fact_identity.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'rikugan.memory.fact_identity'`.

- [ ] **Step 3: Implement the pure helper module**

```python
# rikugan/memory/fact_identity.py
from __future__ import annotations

import hashlib
import re
import unicodedata

_TYPE_WS_RE = re.compile(r"\s+")
_RECORD_TYPES = frozenset({"fact", "entity", "relation"})


def canonicalize_fact_type(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("fact_type must be a string")
    canonical = _TYPE_WS_RE.sub(" ", unicodedata.normalize("NFC", value).strip()).casefold()
    if not canonical:
        raise ValueError("fact_type must not be empty after canonicalization")
    return canonical


def canonicalize_fact_content(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("content must be a string")
    canonical = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n")).strip()
    if not canonical:
        raise ValueError("content must not be empty after canonicalization")
    return canonical


def semantic_fact_hash(fact_type: str, content: str) -> str:
    category = canonicalize_fact_type(fact_type)
    body = canonicalize_fact_content(content)
    category_bytes = category.encode("utf-8")
    payload = str(len(category_bytes)).encode("ascii") + b":" + category_bytes + body.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def deterministic_import_record_id(
    target_memory_id: str,
    import_id: str,
    record_type: str,
    origin_record_id: str,
) -> str:
    if record_type not in _RECORD_TYPES:
        raise ValueError(f"unsupported import record type: {record_type!r}")
    fields = (target_memory_id, import_id, record_type, origin_record_id)
    encoded = b"".join(str(len(field.encode('utf-8'))).encode("ascii") + b":" + field.encode("utf-8") for field in fields)
    return f"{record_type}-{hashlib.sha256(encoded).hexdigest()[:32]}"
```

- [ ] **Step 4: Run focused tests and lint**

Run:

```bash
uv run --frozen --python 3.11 python -m pytest tests/memory/test_fact_identity.py -q
uv run --frozen --python 3.11 python -m ruff check rikugan/memory/fact_identity.py tests/memory/test_fact_identity.py
```

Expected: all fact identity tests pass; Ruff prints `All checks passed!`.

- [ ] **Step 5: Commit the fact identity unit**

```bash
git add rikugan/memory/fact_identity.py tests/memory/test_fact_identity.py
git commit -m "feat(memory): define canonical fact identity"
```

---

### Task 2: Workspace Schema v2 and Hash Invariants

**Files:**

- Modify: `rikugan/constants.py:42-48`
- Modify: `rikugan/memory/workspace_store.py:32-50,94-178,200-382`
- Create: `tests/memory/test_workspace_migration_v2.py`
- Modify: `tests/memory/test_workspace_store.py:24-98`

**Interfaces:**

- Consumes: `semantic_fact_hash()` from Task 1.
- Produces:
  - `MEMORY_WORKSPACE_SCHEMA_VERSION = 2`
  - `FactRecord.semantic_hash: str`
  - `WorkspaceStore.put_fact(fact_id: str, fact_type: str, title: str, content: str, confidence: float, *, semantic_hash: str | None = None, expected_revision: int) -> FactRecord`
  - migration `_migrate_v2(conn: sqlite3.Connection) -> None`

- [ ] **Step 1: Add a handcrafted v1 migration fixture and failing assertions**

```python
# tests/memory/test_workspace_migration_v2.py
from __future__ import annotations

import sqlite3

import pytest

from rikugan.memory.fact_identity import semantic_fact_hash
from rikugan.memory.workspace import MemoryLocator, new_memory_id, new_record_id
from rikugan.memory import workspace_store
from rikugan.memory.workspace_store import WorkspaceStore


def _create_v1_database(path, owner: str) -> str:
    # Build a faithful v1 database via the REAL _migrate_v1 instead of
    # hand-declaring DDL, so the fixture cannot drift from the production
    # schema (notably the projection_state columns and relations.object_id
    # nullability). _migrate_v1 creates every v1 table and seeds
    # projection_state(id=1); user_version stays 0 until set below.
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    workspace_store._migrate_v1(conn)
    conn.execute("PRAGMA user_version = 1")
    fact_id = new_record_id("fact")
    conn.execute("INSERT INTO workspace_meta VALUES('owner_memory_id', ?)", (owner,))
    conn.execute("INSERT INTO workspace_meta VALUES('workspace_kind', 'binary')")
    conn.execute("INSERT INTO facts VALUES(?, 'Function  Purpose', 'Parser', 1, 10.0)", (fact_id,))
    conn.execute(
        "INSERT INTO fact_revisions VALUES(?, 1, 'Uses RC4\\r\\n', 'legacy-hash', 0.8, 10.0)",
        (fact_id,),
    )
    conn.commit()
    conn.close()
    return fact_id


def test_v1_workspace_migrates_without_changing_existing_records(tmp_path) -> None:
    owner = new_memory_id()
    paths = MemoryLocator(tmp_path).binary(owner)
    fact_id = _create_v1_database(paths.database, owner)

    store = WorkspaceStore.open(paths, owner_memory_id=owner)
    record = store.get_fact(fact_id)
    assert record is not None
    assert record.semantic_hash == semantic_fact_hash("Function  Purpose", "Uses RC4\\r\\n")
    assert record.revision == 1
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert store._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    store.close()


def test_v2_sql_guards_reject_missing_or_uppercase_hash(tmp_path) -> None:
    owner = new_memory_id()
    paths = MemoryLocator(tmp_path).binary(owner)
    store = WorkspaceStore.create(paths, owner_memory_id=owner)
    with pytest.raises(sqlite3.IntegrityError, match="semantic_hash"):
        store._conn.execute(
            "INSERT INTO facts(fact_id, fact_type, title, semantic_hash, current_revision, created_at) VALUES(?, 'x', 'x', NULL, 1, 1)",
            (new_record_id("fact"),),
        )
    with pytest.raises(sqlite3.IntegrityError, match="semantic_hash"):
        store._conn.execute(
            "INSERT INTO facts(fact_id, fact_type, title, semantic_hash, current_revision, created_at) VALUES(?, 'x', 'x', ?, 1, 1)",
            (new_record_id("fact"), "A" * 64),
        )
    store.close()


def test_v2_migration_failure_rolls_back_schema_and_user_version(tmp_path, monkeypatch) -> None:
    owner = new_memory_id()
    paths = MemoryLocator(tmp_path).binary(owner)
    _create_v1_database(paths.database, owner)

    def fail_after_schema_change(conn) -> None:
        conn.execute("ALTER TABLE facts ADD COLUMN semantic_hash TEXT")
        conn.execute("CREATE INDEX idx_facts_semantic ON facts(semantic_hash)")
        conn.execute("CREATE TRIGGER injected_guard BEFORE INSERT ON facts BEGIN SELECT 1; END")
        raise RuntimeError("injected migration failure")

    monkeypatch.setitem(workspace_store._MIGRATIONS, 2, fail_after_schema_change)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        WorkspaceStore.open(paths, owner_memory_id=owner)
    with sqlite3.connect(paths.database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        columns = {row[1] for row in conn.execute("PRAGMA table_info(facts)")}
        assert "semantic_hash" not in columns
        assert conn.execute("SELECT name FROM sqlite_master WHERE name IN ('idx_facts_semantic', 'injected_guard')").fetchall() == []
```

- [ ] **Step 2: Run migration tests to verify RED**

Run:

```bash
uv run --frozen --python 3.11 python -m pytest tests/memory/test_workspace_migration_v2.py -q
```

Expected: failures because schema version remains 1 and `FactRecord` has no `semantic_hash`.

- [ ] **Step 3: Add schema v2, backfill, triggers, and record field**

Implement in `rikugan/constants.py`:

```python
MEMORY_WORKSPACE_SCHEMA_VERSION = 2
```

Implement in `rikugan/memory/workspace_store.py`; import `re` at module scope for hash validation:

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


def _migrate_v2(conn: Any) -> None:
    from .fact_identity import semantic_fact_hash

    conn.execute(
        "ALTER TABLE facts ADD COLUMN semantic_hash TEXT "
        "CHECK(semantic_hash IS NULL OR length(semantic_hash) = 64)"
    )
    rows = conn.execute(
        """
        SELECT f.fact_id, f.fact_type, r.content
        FROM facts f
        JOIN fact_revisions r
          ON r.fact_id = f.fact_id AND r.revision = f.current_revision
        """
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE facts SET semantic_hash = ? WHERE fact_id = ?",
            (semantic_fact_hash(row["fact_type"], row["content"]), row["fact_id"]),
        )
    # Do not use Connection.executescript() here: CPython sqlite3 may commit
    # the outer migration transaction before executing the script.
    conn.execute("CREATE INDEX idx_facts_semantic ON facts(semantic_hash)")
    conn.execute(
        """
        CREATE TRIGGER facts_semantic_hash_insert_guard
        BEFORE INSERT ON facts
        WHEN NEW.semantic_hash IS NULL
          OR length(NEW.semantic_hash) != 64
          OR NEW.semantic_hash GLOB '*[^0-9a-f]*'
        BEGIN
          SELECT RAISE(ABORT, 'semantic_hash must be a 64-character lowercase SHA-256 hex digest');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER facts_semantic_hash_update_guard
        BEFORE UPDATE OF semantic_hash ON facts
        WHEN NEW.semantic_hash IS NULL
          OR length(NEW.semantic_hash) != 64
          OR NEW.semantic_hash GLOB '*[^0-9a-f]*'
        BEGIN
          SELECT RAISE(ABORT, 'semantic_hash must be a 64-character lowercase SHA-256 hex digest');
        END
        """
    )
    invalid = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE semantic_hash IS NULL OR semantic_hash GLOB '*[^0-9a-f]*' OR length(semantic_hash) != 64"
    ).fetchone()[0]
    if invalid:
        raise RuntimeError(f"semantic hash backfill left {invalid} invalid fact(s)")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("workspace v2 migration failed foreign key check")


_MIGRATIONS = {1: _migrate_v1, 2: _migrate_v2}
```

Update all `FactRecord(...)` construction and fact SELECTs to include `f.semantic_hash`. Extend `put_fact()`:

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
    expected_revision: int,
) -> FactRecord:
    from .fact_identity import semantic_fact_hash

    current_semantic_hash = semantic_hash or semantic_fact_hash(fact_type, content)
    if not re.fullmatch(r"[0-9a-f]{64}", current_semantic_hash):
        raise ValueError("semantic_hash must be a 64-character lowercase SHA-256 hex digest")
    # Preserve the existing validation and BEGIN IMMEDIATE revision workflow.
    # For INSERT, add semantic_hash between title and current_revision.
    # For UPDATE, set fact_type = ?, title = ?, semantic_hash = ?,
    # current_revision = ? for the explicit fact_id.
```

- [ ] **Step 4: Extend existing store tests for hash-preserving create/update/list**

Add to `tests/memory/test_workspace_store.py`:

```python
def test_create_update_and_list_expose_current_semantic_hash(tmp_path: Path) -> None:
    store, _ = _create_store(tmp_path)
    fid = new_record_id("fact")
    first = store.put_fact(fid, "algorithm", "RC4", "Uses RC4", 0.8, expected_revision=0)
    second = store.put_fact(fid, "algorithm", "RC4", "Uses modified RC4", 0.9, expected_revision=1)
    assert first.semantic_hash != second.semantic_hash
    assert store.get_fact(fid).semantic_hash == second.semantic_hash
    assert store.list_facts()[0].semantic_hash == second.semantic_hash
    store.close()
```

- [ ] **Step 5: Run focused store/migration tests**

Run:

```bash
uv run --frozen --python 3.11 python -m pytest \
  tests/memory/test_workspace_migration_v2.py \
  tests/memory/test_workspace_store.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit schema v2**

```bash
git add rikugan/constants.py rikugan/memory/workspace_store.py tests/memory/test_workspace_migration_v2.py tests/memory/test_workspace_store.py
git commit -m "feat(memory): migrate workspaces to semantic fact hashes"
```

---

### Task 3: Atomic Exact Save and Repository Semantics

**Files:**

- Modify: `rikugan/memory/workspace_store.py:260-382`
- Modify: `rikugan/memory/repository.py:187-247`
- Modify: `tests/memory/test_workspace_store.py:211-227`
- Modify: `tests/memory/test_repository.py`

**Interfaces:**

- Consumes: Task 1 canonicalization/hash functions; Task 2 `FactRecord.semantic_hash`.
- Produces:
  - `FactSaveOutcome = Literal["created", "deduplicated"]`
  - `WorkspaceStore.save_fact_if_semantically_absent(...) -> tuple[FactRecord, FactSaveOutcome]`
  - `SavedKnowledgeMemory(record: KnowledgeMemory, outcome: FactSaveOutcome)` as an immutable repository result.
  - `SQLiteKnowledgeRepository.save_memory_fact(category: str, fact: str, source: str) -> SavedKnowledgeMemory`
- Contract note: the caller-provided `fact_id` is consumed only for the `created` branch; the `deduplicated` branch returns the deterministic oldest matching legacy/current record and does not persist the unused generated ID.

- [ ] **Step 1: Add failing same-category and exact-dedup repository tests**

```python
# tests/memory/test_repository.py
from rikugan.memory.fact_identity import semantic_fact_hash


def test_save_two_facts_in_same_category_preserves_both(tmp_path: Path) -> None:
    repo, _ = _create_repo(tmp_path)
    first = repo.save_memory_fact("function_purpose", "0x401000 parses config", "save_memory")
    second = repo.save_memory_fact("function_purpose", "0x402000 decrypts packets", "save_memory")
    assert first.outcome == "created"
    assert second.outcome == "created"
    assert {m.content for m in repo.list_memories()} == {
        "0x401000 parses config",
        "0x402000 decrypts packets",
    }


def test_exact_semantic_duplicate_reuses_identity_without_revision(tmp_path: Path) -> None:
    repo, _ = _create_repo(tmp_path)
    first = repo.save_memory_fact(" Function  Purpose ", "Uses RC4\r\n", "save_memory")
    second = repo.save_memory_fact("function purpose", "Uses RC4\n", "save_memory")
    assert second.outcome == "deduplicated"
    assert second.record.id == first.record.id
    stored = repo._store.get_fact(first.record.id)
    assert stored is not None
    assert stored.revision == 1
    assert repo.count_observations() == 2
    assert stored.semantic_hash == semantic_fact_hash("function purpose", "Uses RC4")
```

- [ ] **Step 2: Run repository tests to verify RED**

Run:

```bash
uv run --frozen --python 3.11 python -m pytest tests/memory/test_repository.py -q
```

Expected: failures because `save_memory_fact` and outcome result do not exist.

- [ ] **Step 3: Implement one transaction-owning store primitive**

First extract private validators from the inline checks inside `put_fact()` (currently lines 280-287 of `rikugan/memory/workspace_store.py`):

```python
def _validate_fact_type(value: str) -> None:
    if not value or not isinstance(value, str):
        raise ValueError("fact_type must be a non-empty string")


def _validate_title(value: str) -> None:
    if not isinstance(value, str):
        raise ValueError("title must be a string")


def _validate_content(value: str) -> None:
    if not isinstance(value, str):
        raise ValueError("content must be a string")


def _validate_confidence(value: float) -> None:
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError("confidence must be finite and within [0, 1]")


def _validate_semantic_hash_shape(value: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("semantic_hash must be a 64-character lowercase SHA-256 hex digest")
```

Refactor `put_fact()` to call these helpers instead of the inline `if ... raise` blocks. `Task 2 Step 3` already requires `import re` at module scope for hash validation. These helpers are then reused by `save_fact_if_semantically_absent` below and by `put_fact()`'s new `semantic_hash` parameter (Task 2 Step 3).

Add to `rikugan/memory/workspace_store.py`:

```python
from typing import Literal

FactSaveOutcome = Literal["created", "deduplicated"]


def save_fact_if_semantically_absent(
    self,
    *,
    fact_id: str,
    fact_type: str,
    title: str,
    content: str,
    semantic_hash: str,
    confidence: float,
    observation_id: str,
    observation_type: str,
    observation_payload: str,
) -> tuple[FactRecord, FactSaveOutcome]:
    from .fact_identity import canonicalize_fact_content, canonicalize_fact_type

    validate_record_id("fact", fact_id)
    validate_record_id("observation", observation_id)
    _validate_fact_type(fact_type)
    _validate_title(title)
    _validate_content(content)
    _validate_confidence(confidence)
    _validate_semantic_hash_shape(semantic_hash)
    begin_immediate_with_retry(self._conn)
    try:
        rows = self._conn.execute(
            """
            SELECT f.fact_id, f.fact_type, f.title, f.semantic_hash,
                   f.current_revision, f.created_at, r.content, r.confidence
            FROM facts f
            JOIN fact_revisions r
              ON r.fact_id = f.fact_id AND r.revision = f.current_revision
            WHERE f.semantic_hash = ?
            ORDER BY f.created_at, f.fact_id
            """,
            (semantic_hash,),
        ).fetchall()
        match = next(
            (
                row
                for row in rows
                if canonicalize_fact_type(row["fact_type"]) == canonicalize_fact_type(fact_type)
                and canonicalize_fact_content(row["content"]) == canonicalize_fact_content(content)
            ),
            None,
        )
        if match is None:
            # Insert facts + revision 1 directly inside this transaction;
            # do not call put_fact(), which starts its own transaction.
            now = time.time()
            self._conn.execute(
                "INSERT INTO facts(fact_id, fact_type, title, semantic_hash, current_revision, created_at) VALUES(?, ?, ?, ?, 1, ?)",
                (fact_id, fact_type, title, semantic_hash, now),
            )
            self._conn.execute(
                "INSERT INTO fact_revisions(fact_id, revision, content, content_hash, confidence, created_at) VALUES(?, 1, ?, ?, ?, ?)",
                (fact_id, content, hashlib.sha256(content.encode("utf-8")).hexdigest(), confidence, now),
            )
            selected_id = fact_id
            outcome: FactSaveOutcome = "created"
        else:
            selected_id = match["fact_id"]
            outcome = "deduplicated"
        payload = json.loads(observation_payload)
        payload["fact_id"] = selected_id
        payload["outcome"] = outcome
        self._conn.execute(
            "INSERT INTO observations(observation_id, observation_type, content, created_at) VALUES(?, ?, ?, ?)",
            (observation_id, observation_type, json.dumps(payload, ensure_ascii=False, sort_keys=True), time.time()),
        )
        self._conn.commit()
    except BaseException:
        self._conn.rollback()
        raise
    record = self.get_fact(selected_id)
    if record is None:
        raise RuntimeError(f"saved fact disappeared after commit: {selected_id}")
    return record, outcome
```

- [ ] **Step 4: Replace category overwrite in repository**

Add to `rikugan/memory/repository.py`:

```python
from dataclasses import dataclass
from typing import Literal

from .fact_identity import canonicalize_fact_content, canonicalize_fact_type, semantic_fact_hash


@dataclass(frozen=True)
class SavedKnowledgeMemory:
    record: KnowledgeMemory
    outcome: Literal["created", "deduplicated"]


def save_memory_fact(self, category: str, fact: str, source: str) -> SavedKnowledgeMemory:
    from .workspace import new_record_id

    canonical_type = canonicalize_fact_type(category)
    canonical_content = canonicalize_fact_content(fact)
    digest = semantic_fact_hash(canonical_type, canonical_content)
    record, outcome = self._store.save_fact_if_semantically_absent(
        fact_id=new_record_id("fact"),
        fact_type=canonical_type,
        title=canonical_type,
        content=canonical_content,
        semantic_hash=digest,
        confidence=0.7,
        observation_id=new_record_id("observation"),
        observation_type=source,
        observation_payload=json.dumps(
            {"category": canonical_type, "semantic_hash": digest},
            ensure_ascii=False,
            sort_keys=True,
        ),
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

Delete `upsert_memory_fact()` or retain a short deprecated wrapper that delegates to `save_memory_fact()` without category-update behavior; update every caller in the same commit.

- [ ] **Step 5: Add cross-connection concurrency tests**

```python
# tests/memory/test_workspace_store.py
from concurrent.futures import ThreadPoolExecutor


def test_concurrent_exact_saves_create_one_fact_and_two_observations(tmp_path: Path) -> None:
    owner = new_memory_id()
    paths = MemoryLocator(tmp_path).binary(owner)
    WorkspaceStore.create(paths, owner_memory_id=owner).close()

    def save(index: int) -> str:
        store = WorkspaceStore.open(paths, owner_memory_id=owner)
        repo = SQLiteKnowledgeRepository(store, owner_memory_id=owner)
        try:
            return repo.save_memory_fact("algorithm", "Uses RC4", f"worker-{index}").record.id
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(save, range(2)))
    final = WorkspaceStore.open(paths, owner_memory_id=owner)
    assert len(set(ids)) == 1
    assert len(final.list_facts()) == 1
    assert final.count_observations() == 2
    final.close()
```

Add a second test with different content and the same category; assert two facts.

- [ ] **Step 6: Run repository/store tests**

Run:

```bash
uv run --frozen --python 3.11 python -m pytest \
  tests/memory/test_fact_identity.py \
  tests/memory/test_workspace_store.py \
  tests/memory/test_repository.py -q
```

Expected: all tests pass, including concurrency.

- [ ] **Step 7: Commit atomic fact save**

```bash
git add rikugan/memory/workspace_store.py rikugan/memory/repository.py tests/memory/test_workspace_store.py tests/memory/test_repository.py
git commit -m "fix(memory): preserve independent same-category facts"
```

---

### Task 4: Service Outcome and Compact `save_memory` Result

**Files:**

- Modify: `rikugan/memory/service.py:22-34,131-177`
- Modify: `rikugan/agent/loop.py:1580-1626`
- Modify: `tests/memory/test_service.py:70-126`
- Modify: `tests/agent/test_memory_cutover.py`

**Interfaces:**

- Consumes: `SQLiteKnowledgeRepository.save_memory_fact()` from Task 3.
- Produces: `SaveMemoryResult(record_id, revision, outcome, projection_dirty, warning)`.

- [ ] **Step 1: Write failing service/tool-result tests**

```python
# tests/memory/test_service.py

def test_save_fact_reports_created_then_deduplicated(tmp_path: Path) -> None:
    service, issuer, context = _create_service(tmp_path)
    authority = issuer.issue(context)
    created = service.save_fact(authority, category="protocol", fact="Uses HTTP", source="save_memory")
    duplicate = service.save_fact(authority, category=" Protocol ", fact="Uses HTTP\r\n", source="save_memory")
    assert created.outcome == "created"
    assert duplicate.outcome == "deduplicated"
    assert duplicate.record_id == created.record_id
    assert len(service.repository.list_memories()) == 1
```

```python
# tests/agent/test_memory_cutover.py

def test_save_memory_tool_returns_compact_created_and_duplicate_messages(self, tmp_path: Path) -> None:
    # This file uses module-level `_make_loop_with_central_memory(tmp_path)`
    # (returns tuple[AgentLoop, BinaryMemoryService]); there is no `self._make_loop`.
    loop, _service = _make_loop_with_central_memory(tmp_path)
    fact = "Uses HTTP " + "X" * 900
    first = list(loop._handle_save_memory_tool(ToolCall(id="one", name="save_memory", arguments={"category": "protocol", "fact": fact})))
    second = list(loop._handle_save_memory_tool(ToolCall(id="two", name="save_memory", arguments={"category": "protocol", "fact": fact})))
    assert first[-1].tool_result.startswith("Memory created: fact-")
    assert second[-1].tool_result.startswith("Memory already exists: fact-")
    assert fact not in first[-1].tool_result
    assert fact not in second[-1].tool_result
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
uv run --frozen --python 3.11 python -m pytest tests/memory/test_service.py tests/agent/test_memory_cutover.py -q
```

Expected: failures because `SaveMemoryResult.outcome` and compact messages do not exist.

- [ ] **Step 3: Implement outcome propagation**

```python
# rikugan/memory/service.py
from typing import Literal

@dataclass(frozen=True)
class SaveMemoryResult:
    record_id: str
    revision: int
    outcome: Literal["created", "deduplicated"]
    projection_dirty: bool
    warning: str
```

In `save_fact()`:

```python
saved = self.repository.save_memory_fact(normalized_category, normalized_fact, source)
record = saved.record
# Existing verification and projector behavior remain.
return SaveMemoryResult(
    record_id=record.id,
    revision=verify.revision if verify is not None else 1,
    outcome=saved.outcome,
    projection_dirty=False,
    warning="",
)
```

Mirror `outcome=saved.outcome` in the projection-dirty return branch.

In `rikugan/agent/loop.py`:

```python
label = "Memory created" if result.outcome == "created" else "Memory already exists"
content = f"{label}: {result.record_id} [{category}]"
if result.projection_dirty:
    content += " (MEMORY.md projection pending)"
```

Keep logging bounded to `fact[:80]`; do not include the full fact in the tool result.

- [ ] **Step 4: Run focused tests**

Run:

```bash
uv run --frozen --python 3.11 python -m pytest \
  tests/memory/test_service.py \
  tests/agent/test_memory_cutover.py \
  tests/agent/test_memory_write_ownership.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit service/tool behavior**

```bash
git add rikugan/memory/service.py rikugan/agent/loop.py tests/memory/test_service.py tests/agent/test_memory_cutover.py
git commit -m "fix(memory): report exact save outcomes compactly"
```

---

### Task 5: Verified Backup-Aware Writable Open

**Files:**

- Modify: `rikugan/memory/workspace.py:166-199`
- Modify: `rikugan/memory/backup.py:20-135`
- Create: `rikugan/memory/workspace_open.py`
- Create: `tests/memory/test_workspace_open.py`
- Modify: `tests/memory/test_backup.py`

**Interfaces:**

- Consumes: `WorkspacePaths`, `WorkspaceStore.open()`, schema v2 from Task 2.
- Produces:
  - `MemoryLocator.backups(workspace_id: str) -> Path`
  - `inspect_workspace_version(paths: WorkspacePaths) -> int`
  - `verify_backup(result: BackupResult, owner_memory_id: str) -> None`
  - `open_workspace_for_write(paths: WorkspacePaths, owner_memory_id: str, backup_dir: Path) -> WorkspaceStore`
  - `restore_v1_backup_offline(backup_path: Path, target_paths: WorkspacePaths, owner_memory_id: str) -> None`

- [ ] **Step 1: Write failing backup/open tests**

```python
# tests/memory/test_workspace_open.py
from __future__ import annotations

import sqlite3

import pytest

from rikugan.memory.backup import BackupVerificationError
from rikugan.memory.workspace import MemoryLocator, new_memory_id
from rikugan.memory.workspace_open import open_workspace_for_write, restore_v1_backup_offline

from .test_workspace_migration_v2 import _create_v1_database


def test_writable_v1_open_creates_verified_backup_before_migration(tmp_path) -> None:
    owner = new_memory_id()
    locator = MemoryLocator(tmp_path / "memory")
    paths = locator.binary(owner)
    _create_v1_database(paths.database, owner)

    store = open_workspace_for_write(paths, owner, locator.backups(owner))
    backups = list(locator.backups(owner).glob("memory_*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 1
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 2
    store.close()


def test_backup_failure_aborts_before_migration(tmp_path, monkeypatch) -> None:
    owner = new_memory_id()
    locator = MemoryLocator(tmp_path / "memory")
    paths = locator.binary(owner)
    _create_v1_database(paths.database, owner)
    monkeypatch.setattr("rikugan.memory.workspace_open.create_backup", lambda *a, **k: (_ for _ in ()).throw(BackupVerificationError("boom")))
    with pytest.raises(BackupVerificationError, match="boom"):
        open_workspace_for_write(paths, owner, locator.backups(owner))
    with sqlite3.connect(paths.database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_offline_rollback_preserves_v1_without_reopening_v2(tmp_path) -> None:
    owner = new_memory_id()
    locator = MemoryLocator(tmp_path / "memory")
    paths = locator.binary(owner)
    _create_v1_database(paths.database, owner)
    store = open_workspace_for_write(paths, owner, locator.backups(owner))
    store.close()
    backup = next(locator.backups(owner).glob("memory_*.db"))
    restore_v1_backup_offline(backup, paths, owner)
    with sqlite3.connect(paths.database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
uv run --frozen --python 3.11 python -m pytest tests/memory/test_workspace_open.py tests/memory/test_backup.py -q
```

Expected: import failures for the new helper and verification error.

- [ ] **Step 3: Harden backup creation and verification**

In `rikugan/memory/workspace.py`:

```python
def backups(self, workspace_id: str) -> Path:
    if not (_MEMORY_ID_RE.fullmatch(workspace_id) or _CASE_ID_RE.fullmatch(workspace_id)):
        raise ValueError(f"invalid workspace ID: {workspace_id!r}")
    return self.root / "backups" / workspace_id
```

In `rikugan/memory/backup.py`:

```python
class BackupVerificationError(RuntimeError):
    pass


def _exclusive_backup_path(backup_dir: Path, owner_memory_id: str) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    for attempt in range(100):
        suffix = f"{time.time_ns()}_{attempt}"
        candidate = backup_dir / f"memory_{owner_memory_id[:12]}_{suffix}.db"
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return candidate
    raise FileExistsError("could not allocate a unique backup path")
```

Resolve `paths.database`, URL-quote its POSIX form, and open the source using `sqlite3.connect(f"file:{quoted_path}?mode=ro", uri=True)`. Back it up into the exclusive destination, compute SHA-256 incrementally, and add:

```python
def verify_backup(result: BackupResult, owner_memory_id: str) -> None:
    path = result.backup_path
    if not path.is_file() or path.stat().st_size <= 0:
        raise BackupVerificationError("backup is missing or empty")
    with path.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            raise BackupVerificationError("backup has an invalid SQLite header")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != result.manifest_hash:
        raise BackupVerificationError("backup hash mismatch")
    conn = sqlite3.connect(f"file:{quote(path.as_posix(), safe='/:')}?mode=ro", uri=True)
    try:
        owner = conn.execute("SELECT value FROM workspace_meta WHERE key = 'owner_memory_id'").fetchone()
        if owner is None or owner[0] != owner_memory_id:
            raise BackupVerificationError("backup owner mismatch")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupVerificationError("backup integrity check failed")
    finally:
        conn.close()
```

- [ ] **Step 4: Implement backup-aware open and offline rollback**

```python
# rikugan/memory/workspace_open.py
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from urllib.parse import quote

from .backup import create_backup, verify_backup
from .workspace import WorkspacePaths
from .workspace_store import WorkspaceStore


def inspect_workspace_version(paths: WorkspacePaths) -> int:
    uri = quote(paths.database.resolve().as_posix(), safe="/:")
    conn = sqlite3.connect(f"file:{uri}?mode=ro", uri=True)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def open_workspace_for_write(paths: WorkspacePaths, owner_memory_id: str, backup_dir: Path) -> WorkspaceStore:
    version = inspect_workspace_version(paths)
    if version == 1:
        result = create_backup(paths, owner_memory_id, backup_dir)
        verify_backup(result, owner_memory_id)
    return WorkspaceStore.open(paths, owner_memory_id=owner_memory_id)


def restore_v1_backup_offline(backup_path: Path, target_paths: WorkspacePaths, owner_memory_id: str) -> None:
    # Verify owner/integrity with the same verification primitive using a freshly computed BackupResult.
    # Copy to a sibling temp file, fsync, then os.replace while no WorkspaceStore is open.
    # Re-open only with raw sqlite3 mode=ro to assert user_version == 1 and owner identity.
```

Implement the full offline body in this step: use `shutil.copyfileobj`, `os.fsync`, `os.replace`, and remove the temporary file in `finally`. Do not call `WorkspaceStore.create()` or `WorkspaceStore.open()`.

- [ ] **Step 5: Add collision and owner/integrity failure tests**

Extend `tests/memory/test_backup.py`:

```python
def test_two_backups_in_same_clock_tick_never_overwrite(tmp_path, monkeypatch) -> None:
    store, paths, mid = _create_workspace(tmp_path)
    monkeypatch.setattr("time.time_ns", lambda: 123)
    first = create_backup(paths, mid, tmp_path / "backups")
    second = create_backup(paths, mid, tmp_path / "backups")
    assert first.backup_path != second.backup_path
    assert first.backup_path.exists() and second.backup_path.exists()
    store.close()
```

Add corrupt-header, wrong-owner, and hash-mismatch tests; each must raise `BackupVerificationError`.

- [ ] **Step 6: Run focused backup tests**

Run:

```bash
uv run --frozen --python 3.11 python -m pytest \
  tests/memory/test_backup.py \
  tests/memory/test_workspace_open.py \
  tests/memory/test_workspace_migration_v2.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit backup-aware open**

```bash
git add rikugan/memory/workspace.py rikugan/memory/backup.py rikugan/memory/workspace_open.py tests/memory/test_backup.py tests/memory/test_workspace_open.py
git commit -m "feat(memory): back up workspaces before migration"
```

---

### Task 6: Route Production Binary and Case Opens Through the Backup Gate

**Files:**

- Modify: `rikugan/ui/session_controller_base.py:514-571`
- Modify: `rikugan/memory/case_service.py:100-161`
- Modify: `rikugan/memory/case_repository.py:27-36,320-380`
- Modify: `rikugan/memory/peer_retrieval.py:101-126`
- Modify: `rikugan/memory/backup.py:90-135`
- Modify: `tests/memory/test_first_open_regression.py`
- Modify: `tests/agent/test_session_controller.py`
- Modify: `tests/memory/test_case_service.py`
- Modify: `tests/memory/test_case_relations.py`
- Modify: `tests/memory/test_peer_retrieval.py`
- Modify: `tests/memory/test_workspace_open.py`

**Interfaces:**

- Consumes: `open_workspace_for_write()` and offline rollback from Task 5.
- Produces: no direct writable production `WorkspaceStore.open()` calls outside `workspace_open.py`; read-only peer opens remain direct and non-migrating.

- [ ] **Step 1: Add failing call-site routing tests**

Do not leave the old hand-copied controller `if exists: WorkspaceStore.open()` branch in `tests/memory/test_first_open_regression.py`; rewrite those assertions around the production helper directly. Add:

```python
def test_existing_binary_workspace_uses_backup_aware_open(tmp_path, monkeypatch) -> None:
    owner = new_memory_id()
    locator = MemoryLocator(tmp_path / "memory")
    paths = locator.binary(owner)
    WorkspaceStore.create(paths, owner_memory_id=owner).close()
    calls: list[tuple[object, str, Path]] = []
    real_open = workspace_open.open_workspace_for_write

    def tracking_open(paths_arg, owner_arg, backup_dir_arg):
        calls.append((paths_arg, owner_arg, backup_dir_arg))
        return real_open(paths_arg, owner_arg, backup_dir_arg)

    monkeypatch.setattr(workspace_open, "open_workspace_for_write", tracking_open)
    store = workspace_open.open_workspace_for_write(paths, owner, locator.backups(owner))
    store.close()
    assert calls == [(paths, owner, locator.backups(owner))]
```

For the actual controller routing, add a focused test to the existing `TestIdaSessionController` class in `tests/agent/test_session_controller.py`. The existing `setUp()` already builds a real `IdaSessionController(self.cfg)` with a per-test tempdir at `self.cfg._config_dir`, and exposes `self.ctrl._db_instance_id` (an `IdaSessionController` attribute) plus `self.ctrl.session.idb_path`. The new test therefore does not need a new fixture — it patches within the existing scaffold.

`SessionControllerBase._wire_central_memory(self, loop, tab_id=None)` (production code at `rikugan/ui/session_controller_base.py:583-648`) takes a `loop` argument and builds a `MemoryWorkspaceManager(self.config)` internally. The test must therefore:

1. **Seed the workspace DB once** by calling `_wire_central_memory(loop)` with a throwaway `AgentLoop` mock so the first-run path (line 622-623 `WorkspaceStore.create`) runs and writes `memory.db` to disk. Use `unittest.mock.MagicMock()` for `loop` — the only attribute `_wire_central_memory` touches on `loop` is `loop.memory_service = ...` and `loop._memory_authority = ...` and `loop._memory_manager = ...`, all of which `MagicMock` accepts.

2. **Patch and call again** to drive the second-run path:

```python
def test_wire_central_memory_routes_existing_workspace_through_backup_helper(self) -> None:
    """Second-run _wire_central_memory must open via open_workspace_for_write."""
    from unittest.mock import MagicMock
    from rikugan.memory import workspace_open

    # First call seeds memory.db on disk (first-run create path).
    self.ctrl._db_instance_id = "a" * 32
    self.ctrl._wire_central_memory(MagicMock())

    calls: list[tuple] = []
    real_open = workspace_open.open_workspace_for_write

    def _tracking_open(paths_arg, owner_arg, backup_dir_arg):
        calls.append((paths_arg, owner_arg, backup_dir_arg))
        return real_open(paths_arg, owner_arg, backup_dir_arg)

    with patch.object(workspace_open, "open_workspace_for_write", _tracking_open):
        self.ctrl._wire_central_memory(MagicMock())

    self.assertEqual(len(calls), 1)
    # Verify the helper was called with owner + backups dir, not WorkspaceStore.open.
    _paths, owner, _backup_dir = calls[0]
    self.assertEqual(owner, self.ctrl.session.binary_memory_id if hasattr(self.ctrl.session, "binary_memory_id") else owner)
```

If the first call fails to bind (e.g., `_db_instance_id` shape is rejected by `MemoryWorkspaceManager.bind`), fall back to seeding the DB directly: `WorkspaceStore.create(MemoryLocator(self.cfg.memory_dir).binary(new_memory_id()), owner_memory_id=mid).close()` then patch `_wire_central_memory`'s identity-resolution path. The invariant under test is: **when `paths.database.exists()` is truthy, `open_workspace_for_write` is invoked exactly once and direct `WorkspaceStore.open` is NOT called.**

In `tests/memory/test_case_service.py`, create a v1 case workspace and assert promotion migrates only after a backup appears. In `tests/memory/test_case_relations.py`, assert writable relation creation uses the helper and add:

```python
def test_list_relations_does_not_migrate_stale_workspace(tmp_path: Path) -> None:
    cases, _registry, _mid_a, _mid_b = _setup(tmp_path)
    case = cases.list_cases()[0]
    case_paths = cases.locator.case(case.case_id)
    _create_v1_database(case_paths.database, case.case_id)
    with pytest.raises(SchemaMigrationRequired):
        cases.list_case_relations(case.case_id)
    with sqlite3.connect(case_paths.database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
```

Import `_create_v1_database` from `tests.memory.test_workspace_migration_v2` and `SchemaMigrationRequired` from `rikugan.memory.sqlite_backend`. The production call-site audit shows `PeerMemoryRetriever._find_eligible_peers()` is the only non-test caller. Make peer retrieval fail closed for a stale case workspace.

**Dependency order:** Step 3 changes `list_case_relations()` to open with `read_only=True` so that a stale v1 case DB raises `SchemaMigrationRequired` instead of silently migrating. Only after that change does the `try/except` in `_find_eligible_peers()` actually fire — if you wrap `_find_eligible_peers` before `list_case_relations` is read-only, the migration will run before the exception is raised and the test for `PRAGMA user_version == 1` will fail.

```python
# rikugan/memory/peer_retrieval.py
from .sqlite_backend import SchemaMigrationRequired


def _find_eligible_peers(self, case_id: str, active_memory_id: str) -> list[PeerCandidate]:
    try:
        relations = self._cases.list_case_relations(case_id)
    except SchemaMigrationRequired:
        return []
    members = self._cases.list_members(case_id)
    member_names = {m.memory_id: m for m in members}
    # ... remainder of the existing relation filtering + sort body unchanged
```

In `tests/memory/test_peer_retrieval.py`, construct a stale v1 case database with `_create_v1_database`, call `retriever.retrieve(...)`, and assert `PeerContextPack(peers=(), records=(), used_chars=0)` plus `PRAGMA user_version == 1`. This proves a read path neither crashes nor silently migrates.

- [ ] **Step 2: Run call-site tests to verify RED**

Run:

```bash
uv run --frozen --python 3.11 python -m pytest \
  tests/memory/test_first_open_regression.py \
  tests/agent/test_session_controller.py \
  tests/memory/test_case_service.py \
  tests/memory/test_case_relations.py \
  tests/memory/test_peer_retrieval.py -q
```

Expected: patched helper is not called and case paths still call `WorkspaceStore.open()` directly.

- [ ] **Step 3: Replace production writable opens**

In controller wiring:

```python
from ..memory.workspace_open import open_workspace_for_write

if paths.database.exists():
    store = open_workspace_for_write(paths, result.binding.memory_id, manager.locator.backups(result.binding.memory_id))
else:
    store = WorkspaceStore.create(paths, owner_memory_id=result.binding.memory_id)
```

`MemoryWorkspaceManager.locator` already exists and is used unchanged. Add the matching read-only property only to `CaseRepository`:

```python
# rikugan/memory/case_repository.py
@property
def locator(self) -> MemoryLocator:
    return self._locator
```

In `CaseMemoryService.promote()` and `CaseRepository.put_case_relation()`, use:

```python
store = (
    open_workspace_for_write(case_paths, case_id, self._cases.locator.backups(case_id))
    if case_paths.database.exists()
    else WorkspaceStore.create(case_paths, owner_memory_id=case_id, workspace_kind="case")
)
```

Use a repository/locator property rather than private access when adding production code. **Make `list_case_relations()` read-only** by changing `WorkspaceStore.open(case_paths, owner_memory_id=case_id)` → `WorkspaceStore.open(case_paths, owner_memory_id=case_id, read_only=True)` at `rikugan/memory/case_repository.py:370`. This is a prerequisite for the peer-retrieval fail-closed behavior in Step 1 — without `read_only=True`, a stale v1 case DB silently migrates before the `SchemaMigrationRequired` exception can fire, so the `_find_eligible_peers` try/except becomes a no-op and the Step 1 test for `PRAGMA user_version == 1` fails.

It must raise `SchemaMigrationRequired` for a stale v1 case rather than migrate during a list operation.

Case promotion continues to call hash-aware `put_fact()`, which Task 2 made canonical by default.

- [ ] **Step 4: Make normal backup restore use the production open helper**

After copying a backup to a current-schema destination, replace the final direct writable reopen in `restore_from_backup()` with `open_workspace_for_write()` and an explicit backup directory argument. Keep offline v1 rollback as a separate function that never invokes this path.

Update the function signature and its current test call explicitly:

```python
def restore_from_backup(
    backup_path: Path,
    target_paths: WorkspacePaths,
    owner_memory_id: str,
    *,
    migration_backup_dir: Path,
) -> WorkspaceStore:
    if not backup_path.is_file():
        raise FileNotFoundError(backup_path)
    source = sqlite3.connect(str(backup_path))
    target = WorkspaceStore.create(target_paths, owner_memory_id=owner_memory_id)
    target.close()
    destination = sqlite3.connect(str(target_paths.database))
    try:
        source.backup(destination)
        destination.execute(
            "UPDATE workspace_meta SET value = ? WHERE key = 'owner_memory_id'",
            (owner_memory_id,),
        )
        destination.commit()
    finally:
        destination.close()
        source.close()
    return open_workspace_for_write(
        target_paths,
        owner_memory_id,
        migration_backup_dir,
    )

# tests/memory/test_backup.py
restored = restore_from_backup(
    result.backup_path,
    new_paths,
    new_mid,
    migration_backup_dir=tmp_path / "migration-backups",
)
```

- [ ] **Step 5: Verify no forbidden writable call sites remain**

Run an AST-based check so a read-only call elsewhere in the same file cannot hide a writable call:

```bash
python - <<'PY'
import ast
from pathlib import Path

allowed = {
    Path('rikugan/memory/workspace_open.py'),
    Path('rikugan/memory/workspace_store.py'),
}
violations = []
for path in Path('rikugan').rglob('*.py'):
    if path in allowed:
        continue
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not (node.func.attr == 'open' and isinstance(node.func.value, ast.Name) and node.func.value.id == 'WorkspaceStore'):
            continue
        read_only = next((kw.value for kw in node.keywords if kw.arg == 'read_only'), None)
        if not (isinstance(read_only, ast.Constant) and read_only.value is True):
            violations.append(f'{path}:{node.lineno}')
assert not violations, violations
print('no direct production writable WorkspaceStore.open calls')
PY
```

Expected: prints `no direct production writable WorkspaceStore.open calls`.

- [ ] **Step 6: Run binary/case/backup tests**

Run:

```bash
uv run --frozen --python 3.11 python -m pytest \
  tests/memory/test_first_open_regression.py \
  tests/agent/test_session_controller.py \
  tests/memory/test_case_service.py \
  tests/memory/test_case_repository.py \
  tests/memory/test_case_relations.py \
  tests/memory/test_peer_retrieval.py \
  tests/memory/test_backup.py \
  tests/memory/test_workspace_open.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit production open routing**

```bash
git add rikugan/ui/session_controller_base.py rikugan/memory/case_service.py rikugan/memory/case_repository.py rikugan/memory/peer_retrieval.py rikugan/memory/backup.py tests/memory/test_first_open_regression.py tests/agent/test_session_controller.py tests/memory/test_case_service.py tests/memory/test_case_relations.py tests/memory/test_peer_retrieval.py tests/memory/test_workspace_open.py
git commit -m "fix(memory): route writable opens through migration backup"
```

---

### Task 7: True Bundle Re-import Idempotency and Projection Compatibility

**Files:**

- Modify: `rikugan/memory/bundle_import.py:80-177`
- Modify: `tests/memory/test_bundle_import.py`
- Modify: `tests/memory/test_bundle_export.py`
- Modify: `tests/memory/test_markdown.py`

**Interfaces:**

- Consumes: `deterministic_import_record_id()` from Task 1; hash-aware ID-based `upsert_memory()` from Task 2.
- Produces:
  - `BundleImportConflictError(RuntimeError)` for deterministic-ID payload mismatches.
  - same bundle + same target → same destination IDs, `imported_count == 0` on replay, and unchanged fact/entity/relation/observation counts.

- [ ] **Step 1: Strengthen the currently weak idempotency test**

Replace `test_import_is_idempotent` with:

```python
def test_import_is_record_count_idempotent_and_target_scoped(tmp_path: Path) -> None:
    bundle = _seed_and_export(tmp_path)
    target_mid = new_memory_id()
    locator = MemoryLocator(tmp_path / "target")
    paths = locator.binary(target_mid)
    store = WorkspaceStore.create(paths, owner_memory_id=target_mid)
    repo = SQLiteKnowledgeRepository(store, owner_memory_id=target_mid)

    first = import_workspace_bundle(bundle, repo)
    first_ids = {
        "facts": {m.id for m in repo.list_memories()},
        "entities": {e.id for e in repo.list_entities()},
        "relations": {r.id for r in repo.list_relations()},
    }
    first_counts = {key: len(value) for key, value in first_ids.items()}
    first_observation_count = repo.count_observations()

    second = import_workspace_bundle(bundle, repo)
    assert second.import_id == first.import_id
    assert second.imported_count == 0
    assert {m.id for m in repo.list_memories()} == first_ids["facts"]
    assert {e.id for e in repo.list_entities()} == first_ids["entities"]
    assert {r.id for r in repo.list_relations()} == first_ids["relations"]
    assert {"facts": len(repo.list_memories()), "entities": len(repo.list_entities()), "relations": len(repo.list_relations())} == first_counts
    assert repo.count_observations() == first_observation_count
    store.close()
```

Add another concrete test that imports into a second target and asserts every destination ID differs while each imported relation's `src`/`dst` IDs exist in that target's entity set. Add a conflict test: import once, mutate one deterministic destination fact through explicit ID-based upsert, replay the bundle, assert `BundleImportConflictError`, and assert all fact/entity/relation/observation counts and the mutated record remain unchanged.

- [ ] **Step 2: Run bundle tests to verify RED**

Run:

```bash
uv run --frozen --python 3.11 python -m pytest tests/memory/test_bundle_import.py -q
```

Expected: the record-count test fails because second import allocates fresh random IDs.

- [ ] **Step 3: Replace random import IDs with deterministic IDs**

In `bundle_import.py`, replace `_new_fact_id()`, `_new_entity_id()`, and `_new_relation_id()` calls:

```python
map_key = (record_type, origin_id)
new_id = id_map.setdefault(
    map_key,
    deterministic_import_record_id(target_mid, import_id, record_type, origin_id),
)
```

Use `dict[tuple[str, str], str]`, not `dict[str, str]`, so an origin bundle that reuses the same textual ID across record types cannot collide in the map. Validate that `origin_id` is a non-empty string and reject duplicate `(record_type, origin_id)` envelopes during the first parse pass. Build `id_map` for all fact/entity/relation envelopes in that first pass, then import in a second pass so relations can reference entities regardless of file ordering. Relation endpoint remapping uses `id_map[("entity", payload["src"])]` and `id_map[("entity", payload["dst"])]` so a fact with the same origin ID cannot be selected accidentally.

Define the importer-owned conflict and payload comparisons:

```python
class BundleImportConflictError(RuntimeError):
    """A deterministic destination ID already stores different data."""


def _fact_payload_matches(existing: KnowledgeMemory, payload: dict[str, object]) -> bool:
    return (
        canonicalize_fact_type(existing.type)
        == canonicalize_fact_type(str(payload.get("type", "general")))
        and canonicalize_fact_content(existing.content)
        == canonicalize_fact_content(str(payload.get("content", "")))
        and existing.title == str(payload.get("title", ""))
        and existing.confidence == float(payload.get("confidence", 0.5))
    )
```

Add the exact entity and relation comparisons:

```python
def _entity_payload_matches(existing: KnowledgeEntity, payload: dict[str, object]) -> bool:
    # Compare only the fields the v1 bundle wire format actually serializes.
    # export_workspace writes type/name/display_name/address for entities and
    # does NOT carry `tags`. Matching an omitted field would make every replay
    # look equal (both empty) yet flag a false conflict if `tags` were edited
    # out-of-band. Lossless entity/relation serialization is a follow-up, out
    # of scope here.
    return (
        existing.type == str(payload.get("type", "unknown"))
        and existing.name == str(payload.get("name", ""))
        and existing.display_name == str(payload.get("display_name", ""))
        and existing.address == str(payload.get("address", ""))
    )


def _relation_payload_matches(
    existing: KnowledgeRelation,
    payload: dict[str, object],
    id_map: dict[tuple[str, str], str],
) -> bool:
    # Compare only graph fields the v1 wire format carries:
    # src/dst/predicate/confidence. export_workspace does NOT serialize
    # relation `evidence`, so omitting it avoids a false conflict if evidence
    # were edited out-of-band.
    return (
        existing.src == id_map[("entity", str(payload.get("src", "")))]
        and existing.predicate == str(payload.get("predicate", "related_to"))
        and existing.dst == id_map[("entity", str(payload.get("dst", "")))]
        and existing.confidence == float(payload.get("confidence", 0.5))
    )
```

Do not compare only `semantic_hash`: title/confidence and the graph metadata the wire format actually carries (entity identity fields, relation endpoints/predicate) are part of bundle replay identity. Do not compare fields the v1 wire format omits (`entity.tags`, `relation.evidence`); extend `export_workspace` to carry them in a follow-up tranche if lossless entity/relation interchange is needed.

Before applying records, build destination lookup dictionaries once from `repository.list_memories()`, `repository.list_entities()`, and `repository.list_relations()`, then compare deterministic IDs against those dictionaries. If every destination record already exists and all payload helpers return true, return `BundleImportResult(import_id=import_id, imported_count=0, target_memory_id=target_mid)` without calling any upsert; this prevents extra revisions or observations on replay. If only a subset exists, validate matching existing records, import only absent records, and raise `BundleImportConflictError(f"import destination conflict: {new_id}")` before any writes when a payload differs. Stage/validate the complete import first so a late conflict cannot leave a partial replay.

Keep bundle schema version 1 and wire payload unchanged.

- [ ] **Step 4: Add same-category round-trip and hash assertions**

Extend export seed data with two facts of type `function_purpose`, export/import, then assert both facts and their valid hashes survive:

```python
facts = target_repo.list_memories()
assert [f.type for f in facts].count("function_purpose") == 2
for fact in target_store.list_facts():
    assert re.fullmatch(r"[0-9a-f]{64}", fact.semantic_hash)
```

- [ ] **Step 5: Add projection compatibility test**

In `tests/memory/test_markdown.py`:

```python
def test_projection_preserves_multiple_same_category_facts_in_existing_order(tmp_path: Path) -> None:
    # Save two independent `function_purpose` facts through repository/service.
    # Project and assert both fact IDs/content appear exactly once.
    # Parse managed entries and assert order uses (fact_type, title, fact_id).
```

Implement the complete fixture using `WorkspaceStore`, `SQLiteKnowledgeRepository`, and `MemoryProjector`; assert concrete IDs and content rather than comments.

- [ ] **Step 6: Run bundle/projection tests**

Run:

```bash
uv run --frozen --python 3.11 python -m pytest \
  tests/memory/test_bundle_import.py \
  tests/memory/test_bundle_export.py \
  tests/memory/test_markdown.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit interchange compatibility**

```bash
git add rikugan/memory/bundle_import.py tests/memory/test_bundle_import.py tests/memory/test_bundle_export.py tests/memory/test_markdown.py
git commit -m "fix(memory): make bundle reimport idempotent"
```

---

### Task 8: Early Orchestra Safety Gate

**Files:**

- Modify: `rikugan/agent/loop.py:140-165,2227-2300`
- Modify: `tests/agent/test_a2a_mode.py:38-84`
- Modify: `tests/agent/test_agent_loop.py:123-150`
- Modify: `README.md:55-70`
- Modify: `ARCHITECTURE.md` Orchestra section

**Interfaces:**

- Consumes: existing `_ParsedCommand.use_orchestra_mode` and `TurnEvent.text_done()`.
- Produces: `rikugan.agent.loop._ORCHESTRA_ENABLED = False` and one early disabled response.

- [ ] **Step 1: Add failing no-side-effect gate test**

```python
# tests/agent/test_agent_loop.py
from unittest.mock import patch


def test_orchestra_gate_returns_before_session_prompt_retrieval_or_provider(self):
    provider = MockProvider(responses=[])
    loop = self._make_loop(provider)
    original_messages = list(loop.session.messages)
    loop.session.metadata["active_mode"] = "research"

    with (
        patch.object(loop, "_resolve_skill", side_effect=AssertionError("skill resolution ran")),
        patch.object(loop, "_build_system_prompt", side_effect=AssertionError("prompt built")),
        patch.object(loop, "_build_tools_schema", side_effect=AssertionError("schemas built")),
    ):
        events = list(loop.run("/orchestra analyze this binary"))

    assert [(event.type, event.text) for event in events] == [
        (
            TurnEventType.TEXT_DONE,
            "Orchestra is temporarily disabled while its execution and context isolation contracts are being hardened.",
        )
    ]
    assert loop.session.messages == original_messages
    assert loop.session.metadata["active_mode"] == "research"
    assert provider._call_count == 0
```

Use the existing private `_call_count` counter on `MockProvider`; no new counter is needed.

- [ ] **Step 2: Run gate tests to verify RED**

Run:

```bash
uv run --frozen --python 3.11 python -m pytest \
  tests/agent/test_agent_loop.py::TestAgentLoop::test_orchestra_gate_returns_before_session_prompt_retrieval_or_provider \
  tests/agent/test_a2a_mode.py -q
```

Expected: current code invokes skill/session/prompt work or reaches Orchestra.

- [ ] **Step 3: Add the early gate**

In `rikugan/agent/loop.py` near command parsing constants:

```python
_ORCHESTRA_ENABLED = False
_ORCHESTRA_DISABLED_MESSAGE = (
    "Orchestra is temporarily disabled while its execution and context isolation contracts are being hardened."
)
```

Immediately after direct-command handling and before `user_message = cmd.message`:

```python
if cmd.use_orchestra_mode and not _ORCHESTRA_ENABLED:
    yield TurnEvent.text_done(_ORCHESTRA_DISABLED_MESSAGE)
    return
```

Keep the existing later `run_orchestra_mode()` branch for focused legacy tests that monkeypatch the constant to `True`.

- [ ] **Step 4: Pin parser compatibility and A2A independence**

Extend `tests/agent/test_a2a_mode.py`:

```python
def test_orchestra_command_remains_recognized_while_runtime_is_gated(self) -> None:
    cmd = _parse_user_command("/orchestra inspect imports")
    self.assertTrue(cmd.use_orchestra_mode)
    self.assertEqual(cmd.message, "inspect imports")
```

Run existing `/a2a` mode tests unchanged to prove the gate is not shared with A2A.

- [ ] **Step 5: Mark Orchestra disabled in docs**

Update the README orchestration feature row and the `ARCHITECTURE.md` Orchestra section with the exact status:

```text
Experimental — temporarily disabled pending shared execution-policy and context-isolation hardening.
```

Do not remove A2A documentation or Orchestra implementation files.

- [ ] **Step 6: Run agent command tests**

Run:

```bash
uv run --frozen --python 3.11 python -m pytest \
  tests/agent/test_agent_loop.py \
  tests/agent/test_a2a_mode.py \
  tests/agent/test_commands.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit the gate**

```bash
git add rikugan/agent/loop.py tests/agent/test_agent_loop.py tests/agent/test_a2a_mode.py README.md ARCHITECTURE.md
git commit -m "fix(agent): gate unsafe orchestra mode"
```

---

### Task 9: Dual-Root Pytest Collection and CI Enforcement

**Files:**

- Modify: `pyproject.toml:153-162`
- Create: `tests/test_pytest_collection_roots.py`
- Modify: `.github/workflows/ci.yml:83-97`
- Modify: `.github/workflows/release.yml:91-105`
- Modify: `ci-local.sh:81-90`
- Create: `rikugan/tests/__init__.py` (empty package marker; see Step 3)

**Interfaces:**

- Consumes: pytest 8+ and current root/package-local conftests.
- Produces: one collection source in `pyproject.toml`; workflows invoke `python -m pytest --tb=short -q` without a test-root argument.

- [ ] **Step 1: Record the current missing-root failure**

Run:

```bash
uv run --frozen --python 3.11 python -m pytest --collect-only -q > .pytest-collect-before.txt
python - <<'PY'
from pathlib import Path
text = Path('.pytest-collect-before.txt').read_text(encoding='utf-8')
assert 'rikugan/tests/test_token_usage_regression.py' not in text
print('confirmed: package-local tests are absent before testpaths configuration')
PY
rm .pytest-collect-before.txt
```

Expected: prints the confirmation. If current local pytest already discovers both roots without positional arguments, record that result and continue; the CI positional root is still a confirmed gap.

- [ ] **Step 2: Add pytest testpaths and a collection contract test**

Append to `pyproject.toml` before dependency groups:

```toml
[tool.pytest.ini_options]
testpaths = ["tests", "rikugan/tests"]
```

Create `tests/test_pytest_collection_roots.py`:

```python
from __future__ import annotations

from pathlib import Path

import tomllib


def test_pytest_testpaths_include_both_regression_roots() -> None:
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert config["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests", "rikugan/tests"]


def test_representative_tests_exist_in_both_roots() -> None:
    assert Path("tests/agent/test_agent_loop.py").is_file()
    assert Path("rikugan/tests/test_token_usage_regression.py").is_file()


def test_rikugan_tests_is_a_package_to_avoid_bare_basename_collisions() -> None:
    # The two roots share a duplicate basename: tests/test_ida_docs_review_prompt.py
    # and rikugan/tests/test_ida_docs_review_prompt.py both exist. The empty
    # __init__.py marker forces package-local tests to import as fully-qualified
    # rikugan.tests.<name> (not bare basenames), preventing a prepend-mode
    # collection collision/shadow between the two roots.
    assert Path("rikugan/tests/__init__.py").is_file()
```

- [ ] **Step 3: Collect both roots and resolve import/fixture conflicts**

Run:

```bash
uv run --frozen --python 3.11 python -m pytest --collect-only -q > .pytest-collect-311.txt
uv run --frozen --python 3.12 python -m pytest --collect-only -q > .pytest-collect-312.txt
python - <<'PY'
from pathlib import Path
for name in ('.pytest-collect-311.txt', '.pytest-collect-312.txt'):
    text = Path(name).read_text(encoding='utf-8')
    assert 'tests/agent/test_agent_loop.py' in text, name
    assert 'rikugan/tests/test_token_usage_regression.py' in text, name
    # The two roots share a duplicate basename; assert BOTH collect (see the
    # __init__.py note below). Distinct fully-qualified names prove no collision.
    assert 'tests/test_ida_docs_review_prompt.py' in text, name
    assert 'rikugan/tests/test_ida_docs_review_prompt.py' in text, name
print('both roots collected on Python 3.11 and 3.12')
PY
rm .pytest-collect-311.txt .pytest-collect-312.txt
```

Expected: both roots are present and collection has no errors.

Add an empty `rikugan/tests/__init__.py` **unconditionally** (not only on error). The two roots share a duplicate test filename — `tests/test_ida_docs_review_prompt.py` and `rikugan/tests/test_ida_docs_review_prompt.py` both exist — and `rikugan/tests/` currently has no `__init__.py`, so under pytest's default *prepend* import mode its files import by bare basename and can collide with / shadow the package-qualified `tests.test_ida_docs_review_prompt`. The marker makes every package-local test import as `rikugan.tests.<name>`, eliminating the collision and matching the existing mypy override at `pyproject.toml` (`module = ["rikugan.tests.test_settings_dialog_fixes"]`). This is why `__init__.py` is committed in Step 7 rather than omitted.

If `tests/conftest.py` stub cleanup is needed in `rikugan/tests`, move the shared hook into a root-level conftest or explicit plugin module and add a regression test; do not duplicate divergent cleanup hooks.

- [ ] **Step 4: Run both roots under each supported Python**

Run:

```bash
uv run --frozen --python 3.11 python -m pytest --tb=short -q
uv run --frozen --python 3.12 python -m pytest --tb=short -q
```

Expected: zero failures on both versions. If pre-existing environment-specific failures occur, classify and fix only collection/fixture defects caused by dual-root execution; do not hide failures or add broad skips.

- [ ] **Step 5: Remove narrower test roots from CI commands**

Change all three commands:

```yaml
# .github/workflows/ci.yml and release.yml
python -m pytest --tb=short -q
```

```bash
# ci-local.sh
python3 -m pytest --tb=short -q
```

Add to `tests/test_pytest_collection_roots.py`:

```python
def test_ci_commands_do_not_narrow_pytest_to_one_root() -> None:
    for path in (Path(".github/workflows/ci.yml"), Path(".github/workflows/release.yml"), Path("ci-local.sh")):
        text = path.read_text(encoding="utf-8")
        assert "pytest tests/" not in text
```

- [ ] **Step 6: Run collection contract and YAML/shell checks**

Run:

```bash
uv run --frozen --python 3.11 python -m pytest tests/test_pytest_collection_roots.py -q
bash -n ci-local.sh
python - <<'PY'
from pathlib import Path
import yaml
for path in (Path('.github/workflows/ci.yml'), Path('.github/workflows/release.yml')):
    yaml.safe_load(path.read_text(encoding='utf-8'))
print('workflow YAML valid')
PY
```

Expected: tests pass, Bash syntax is valid, workflow YAML parses.

- [ ] **Step 7: Commit dual-root CI**

```bash
git add pyproject.toml tests/test_pytest_collection_roots.py .github/workflows/ci.yml .github/workflows/release.yml ci-local.sh rikugan/tests/__init__.py
git commit -m "test: enforce both pytest regression roots"
```

`rikugan/tests/__init__.py` is always created in Step 3, so it is always included in the commit above.

---

### Task 10: Full Verification and Release-Readiness Report

**Files:**

- Modify only if verification uncovers an in-scope regression: files from Tasks 1–9.
- Review: `docs/superpowers/specs/2026-07-22-memory-durability-and-orchestra-gate-design.md`
- Review: `docs/superpowers/plans/2026-07-22-memory-durability-and-orchestra-gate.md`

**Interfaces:**

- Consumes: all deliverables from Tasks 1–9.
- Produces: verified green tranche or an explicit blocker report; no release/version bump.

- [ ] **Step 1: Run formatting and lint**

Run:

```bash
uv run --frozen --python 3.11 python -m ruff format --check rikugan/ tests/
uv run --frozen --python 3.11 python -m ruff check rikugan/ tests/
```

Expected: both commands exit 0.

- [ ] **Step 2: Run type checks**

Run:

```bash
uv run --frozen --python 3.11 python -m mypy rikugan/core rikugan/providers rikugan/memory
```

Expected: exit 0 with no errors. If `rikugan/memory` is not yet in the configured strict-module set, fix actual annotations introduced by this tranche; do not silence the module wholesale.

- [ ] **Step 3: Run focused durability and gate suites**

Run:

```bash
uv run --frozen --python 3.11 python -m pytest \
  tests/memory/test_fact_identity.py \
  tests/memory/test_workspace_migration_v2.py \
  tests/memory/test_workspace_store.py \
  tests/memory/test_repository.py \
  tests/memory/test_service.py \
  tests/memory/test_backup.py \
  tests/memory/test_workspace_open.py \
  tests/memory/test_bundle_import.py \
  tests/memory/test_bundle_export.py \
  tests/memory/test_markdown.py \
  tests/memory/test_case_service.py \
  tests/memory/test_case_repository.py \
  tests/memory/test_first_open_regression.py \
  tests/agent/test_memory_cutover.py \
  tests/agent/test_agent_loop.py \
  tests/agent/test_a2a_mode.py \
  tests/test_pytest_collection_roots.py -q
```

Expected: zero failures.

- [ ] **Step 4: Run full dual-root suites on Python 3.11 and 3.12**

Run:

```bash
uv run --frozen --python 3.11 python -m pytest --tb=short -q
uv run --frozen --python 3.12 python -m pytest --tb=short -q
```

Expected: zero failures on both supported versions.

- [ ] **Step 5: Verify migration invariants directly**

Run:

```bash
uv run --frozen --python 3.11 python -m pytest \
  tests/memory/test_workspace_migration_v2.py \
  tests/memory/test_workspace_open.py \
  -q
python - <<'PY'
import ast
from pathlib import Path
violations = []
for path in Path('rikugan').rglob('*.py'):
    if path.name in {'workspace_store.py', 'workspace_open.py'}:
        continue
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'), filename=str(path))):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not (node.func.attr == 'open' and isinstance(node.func.value, ast.Name) and node.func.value.id == 'WorkspaceStore'):
            continue
        read_only = next((kw.value for kw in node.keywords if kw.arg == 'read_only'), None)
        if not (isinstance(read_only, ast.Constant) and read_only.value is True):
            violations.append(f'{path}:{node.lineno}')
assert not violations, violations
print('writable open boundary verified')
PY
```

Expected: tests pass and boundary check prints confirmation.

- [ ] **Step 6: Verify repository state and no accidental lock/version edits**

Run:

```bash
git diff --check
git status --short
git diff -- uv.lock
```

Expected: `git diff --check` exits 0; `uv.lock` has no diff; status contains only intended tranche files.

- [ ] **Step 7: Request mandatory code reviews**

Dispatch:

1. `python-reviewer` for all Python changes.
2. `code-reviewer` for cross-layer correctness.
3. `ida-tooling-reviewer` only if any file under `rikugan/tools/`, `rikugan/ida/tools/`, or `rikugan/agent/mutation.py` changed unexpectedly; normally it should not run for this tranche.

Fix confirmed findings with focused regression tests, then rerun Steps 1–6.

- [ ] **Step 8: Commit final in-scope review fixes**

If review fixes were required, inspect the final diff, then stage each concrete modified path named by `git status --short` from Tasks 1–9 individually; never use `git add -A` or a broad directory path. For example, when review changed only the fact helper and its tests:

```bash
git status --short
git add rikugan/memory/fact_identity.py tests/memory/test_fact_identity.py
git diff --cached --check
git commit -m "fix: address memory durability review findings"
```

Use that file-by-file form with the actual review-fix paths shown by status. Before committing, unstage any unrelated path shown by `git diff --cached --name-only`. If no changes were required, do not create an empty commit.

- [ ] **Step 9: Produce the final verification report**

Report exact outputs/counts for:

- schema/migration tests;
- backup/rollback tests;
- atomic dedup/concurrency tests;
- bundle idempotency tests;
- Orchestra gate tests;
- Python 3.11 full suite;
- Python 3.12 full suite;
- Ruff and mypy;
- working-tree state.

Do not bump the Rikugan version, create a release, push, or open a PR unless separately requested.

---

## Self-Review

### Spec coverage

| Spec requirement | Plan coverage |
|---|---|
| Canonical fact identity and exact dedup | Tasks 1 and 3 |
| Preserve same-category facts | Task 3 |
| Workspace schema v2 and backfill | Task 2 |
| Hash invariant on every write path | Tasks 2, 6, and 7 |
| Atomic lookup/insert/observation | Task 3 |
| Compact created/deduplicated result | Task 4 |
| Verified backup before production migration | Tasks 5 and 6 |
| Version-preserving offline rollback | Task 5 |
| Bundle schema v1 and true re-import idempotency | Task 7 |
| Preserve independent Markdown entries/order | Task 7 |
| Early Orchestra gate before all side effects | Task 8 |
| A2A unaffected | Task 8 |
| Dual-root collection on Python 3.11/3.12 | Task 9 |
| Local, merge, and release CI parity | Task 9 |
| Full verification and review | Task 10 |

### Placeholder scan

The plan contains no `TBD`, `TODO`, `implement later`, generic “add tests,” or undefined neighboring interfaces. Conditional `rikugan/tests/__init__.py` creation is governed by a concrete observed-failure rule and regression test.

### Type consistency

- Task 1 defines all canonicalization/ID helpers consumed later.
- Task 2 adds `FactRecord.semantic_hash` before Tasks 3, 6, and 7 use it.
- Task 3 defines `SavedKnowledgeMemory` and `save_memory_fact()` before Task 4 propagates outcomes. Task 3 Step 3 first extracts private validators (`_validate_fact_type`, `_validate_title`, `_validate_content`, `_validate_confidence`, `_validate_semantic_hash_shape`) from `put_fact()` body and refactors `put_fact()` to use them — this unblocks `save_fact_if_semantically_absent` which depends on the same validators.
- Task 5 defines backup-aware open/rollback before Task 6 routes call sites.
- Task 6 changes `list_case_relations()` to `WorkspaceStore.open(..., read_only=True)` (Step 3) **before** the `try/except SchemaMigrationRequired` in `_find_eligible_peers` (Step 1 wrapping) becomes effective — without `read_only=True`, the migration runs before the exception fires.
- Task 7: `bundle_export.py:80` already serializes `"title": f.title` into fact payloads, so `_fact_payload_matches()` comparing `existing.title == str(payload.get("title", ""))` is correct.
- Task 9 changes only collection configuration after functionality is complete.
