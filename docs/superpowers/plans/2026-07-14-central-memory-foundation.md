# Central Memory Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Xây dựng dark-scaffolding cho central memory: config root, SQLite registry, identity resolver, per-workspace SQLite, `MEMORY.md` projection, raw-headless identity và run-bound session binding mà chưa thay đổi runtime memory hiện hành.

**Architecture:** `MemoryIdentityResolver` phân tích durable evidence, `MemoryRegistry` ánh xạ evidence sang generated workspace ID, và `MemoryLocator` tạo đường dẫn dưới `<config_dir>/memory`. Mỗi workspace dùng SQLite authoritative store và `MEMORY.md` projection có portable cross-process lock; controller đóng băng workspace/generation theo run nhưng feature flag vẫn tắt cho đến atomic cutover.

**Tech Stack:** Python 3.11–3.12, stdlib `sqlite3`, `portalocker>=3.3.0,<4`, SHA-256, dataclasses, pytest, IDA 9.x netnode identity, Windows/POSIX filesystem identity.

## Global Constraints

- Spec authority: `docs/superpowers/specs/2026-07-14-central-memory-workspaces-design.md`.
- Canonical root: `LucNhanConfig.memory_dir == <LucNhanConfig._config_dir>/memory`; không fallback cạnh IDB.
- `memory_id` format: `mem-` + 32 lowercase hex; `case_id` format: `case-` + 32 lowercase hex; authoritative record IDs use a validated kind prefix plus 32 lowercase hex.
- `db_instance_id`, path, display name và hash chỉ là evidence; không dùng trực tiếp làm directory name.
- SQLite files dùng `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`, transactional migrations và `PRAGMA user_version`.
- Existing read-only/retrieval DB được mở bằng SQLite URI `mode=ro` + `query_only=ON`; recovery có thể dùng `mode=rw` nhưng không tự tạo lại DB bị mất.
- Raw input hash là full lowercase SHA-256 64 hex, tính trước khi launch IDA và reject file thay đổi trong lúc hash.
- `database_generation` tách biệt record revision và chỉ đổi khi active database/workspace đổi.
- `MEMORY.md` managed projection deterministic, không gọi provider/IDA/MCP/embedding.
- `portalocker.Lock(lock_path, mode="a", timeout=5.0)` bảo vệ projection; lock timeout chỉ làm `projection_dirty`.
- First creation applies owner-only permissions where supported (`0o700` directories, `0o600` DB/Markdown/lock/temp files); Windows uses best-effort ACL handling and surfaces hardening failure without sidecar fallback.
- Phase này là dark scaffolding: `memory_workspaces_enabled=False`; không đổi reader/writer `RIKUGAN.md` hoặc JSONL hiện tại.
- Tất cả test mới nằm dưới root `tests/memory/` để GitHub CI hiện tại thu thập.
- Mọi function signature có type annotations; module mới có `from __future__ import annotations`.
- Commit theo Conventional Commits; không commit `.cocoindex_code/`.

**Spec reference:** `docs/superpowers/specs/2026-07-14-central-memory-workspaces-design.md`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `lucnhan/constants.py` | Modify | Tên directory/schema version/lock timeout constants |
| `lucnhan/core/config.py` | Modify | `memory_dir`, dark feature flag |
| `pyproject.toml` | Modify | Runtime dependency `portalocker>=3.3.0,<4` |
| `ida-plugin.json` | Modify | Giữ dependency manifest đồng bộ |
| `lucnhan/memory/workspace.py` | Create | Workspace/evidence/run-context dataclasses, ID validation, locator |
| `lucnhan/memory/sqlite_backend.py` | Create | Safe SQLite open, local-filesystem/WAL validation, migrations |
| `lucnhan/memory/registry.py` | Create | Registry schema v1 và atomic evidence/workspace operations |
| `lucnhan/memory/identity.py` | Create | Filesystem identity, raw hash, ordered resolver decision table |
| `lucnhan/memory/workspace_store.py` | Create | Workspace schema v1, facts/entities/relations/observations/projection state |
| `lucnhan/memory/markdown.py` | Create | Managed-region parser/render/projector và portable lock |
| `lucnhan/memory/manager.py` | Create | Façade bind database, resolve paths, create immutable run context |
| `lucnhan/state/session.py` | Modify | Persist binary memory/case binding fields |
| `lucnhan/state/history.py` | Modify | Session/manifest schema v2 and memory-ID filtering |
| `lucnhan/cli/headless.py` | Modify | Hash original raw input and carry source identity |
| `lucnhan/ida/headless_bootstrap.py` | Modify | Validate/bootstrap memory source identity |
| `lucnhan/ida/headless_controller.py` | Modify | Pass bootstrap identity to base controller |
| `lucnhan/ui/session_controller_base.py` | Modify | Resolve before UUID creation, generations, dark binding lifecycle |
| `tests/memory/test_config.py` | Create | Config root and dependency parity |
| `tests/memory/test_workspace.py` | Create | IDs, locator and immutable run context |
| `tests/memory/test_sqlite_backend.py` | Create | WAL, migrations, unsupported schema/filesystem behavior |
| `tests/memory/test_registry.py` | Create | Registry CRUD, evidence uniqueness, first-open transaction |
| `tests/memory/test_identity.py` | Create | Ordered decision matrix, copy/move/conflict/ephemeral behavior |
| `tests/memory/test_workspace_store.py` | Create | SQLite record/revision/projection-state behavior |
| `tests/memory/test_markdown.py` | Create | Managed parser, deterministic render, conflicts, locking |
| `tests/memory/test_manager.py` | Create | Binding/generation/dark mode integration |
| `tests/cli/test_headless_memory_identity.py` | Create | Raw hashing and bootstrap payload |
| `tests/state/test_memory_binding.py` | Create | Session/manifest round trip and filtering |

---

### Task 1: Memory config, constants, and lock dependency

**Files:**
- Modify: `lucnhan/constants.py:19-35`
- Modify: `lucnhan/core/config.py:74-179`
- Modify: `pyproject.toml:34-48`
- Modify: `ida-plugin.json:20-31`
- Modify: `requirements.txt`
- Modify: `uv.lock`
- Create: `tests/memory/test_config.py`

**Interfaces:**
- Produces: `MEMORY_DIR_NAME`, `MEMORY_REGISTRY_SCHEMA_VERSION`, `MEMORY_WORKSPACE_SCHEMA_VERSION`, `MEMORY_LOCK_TIMEOUT_SECONDS`, `MEMORY_MARKDOWN_MAX_BYTES`.
- Produces: `LucNhanConfig.memory_dir: str` and `LucNhanConfig.memory_workspaces_enabled: bool`.
- Consumed by: Tasks 2–9.

- [ ] **Step 1: Write failing config and manifest-parity tests**

```python
from __future__ import annotations

import json
import tomllib
from pathlib import Path

from lucnhan.core.config import LucNhanConfig


def test_memory_dir_is_central_and_feature_is_dark(tmp_path: Path) -> None:
    config = LucNhanConfig()
    config._config_dir = str(tmp_path)

    assert Path(config.memory_dir) == tmp_path / "memory"
    assert config.memory_workspaces_enabled is False


def test_invalid_memory_flag_type_keeps_safe_default(tmp_path: Path) -> None:
    config = LucNhanConfig()
    config._config_dir = str(tmp_path)
    config._apply_loaded_config({"memory_workspaces_enabled": "true"})

    assert config.memory_workspaces_enabled is False


def test_portalocker_runtime_dependency_is_in_all_manifests() -> None:
    root = Path(__file__).resolve().parents[2]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    plugin = json.loads((root / "ida-plugin.json").read_text(encoding="utf-8"))
    requirements = {
        line.strip()
        for line in (root / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    project_deps = set(pyproject["project"]["dependencies"])
    plugin_deps = set(plugin["plugin"]["pythonDependencies"])
    expected = "portalocker>=3.3.0,<4"

    assert expected in project_deps
    assert expected in plugin_deps
    assert expected in requirements
```

- [ ] **Step 2: Run tests and verify both fail**

Run: `uv run python -m pytest tests/memory/test_config.py -v`

Expected: FAIL because `memory_dir`, `memory_workspaces_enabled`, and the dependency do not exist.

- [ ] **Step 3: Add constants, central config property, and private-root helpers**

```python
# lucnhan/constants.py
MEMORY_DIR_NAME = "memory"
MEMORY_REGISTRY_SCHEMA_VERSION = 1
MEMORY_WORKSPACE_SCHEMA_VERSION = 1
MEMORY_LOCK_TIMEOUT_SECONDS = 5.0
MEMORY_MARKDOWN_MAX_BYTES = 16 * 1024 * 1024
```

```python
# lucnhan/core/config.py dataclass fields/properties
memory_workspaces_enabled: bool = False

@property
def memory_dir(self) -> str:
    """Root for Luc Nhan-owned durable memory workspaces."""
    return os.path.join(self._config_dir, MEMORY_DIR_NAME)
```

Add `memory_workspaces_enabled` to the scalar fields persisted by `_apply_loaded_config()`/`to_dict()`. Import `MEMORY_DIR_NAME` from `lucnhan.constants`. When loading, accept only a real JSON boolean (`type(value) is bool`); an invalid value records a config warning and keeps the safe default instead of assigning a truthy string/integer. Add the same typed-load rule for `case_memory_enabled` and `peer_retrieval_enabled` when introduced later.

Add private-root helpers in `lucnhan/core/config.py` or `lucnhan/memory/sqlite_backend.py`: new directories are created with owner-only mode where supported; new DB/Markdown/lock/temp files are hardened after creation. Add POSIX mode assertions and Windows best-effort/error-path tests. The later `StorageGuard` centralizes and expands these checks.

- [ ] **Step 4: Add the same exact dependency to all runtime manifests**

```toml
# pyproject.toml
"portalocker>=3.3.0,<4",
```

```json
"portalocker>=3.3.0,<4"
```

```text
# requirements.txt
portalocker>=3.3.0,<4
```

Keep the dependency order identical where manifests use ordered lists. `requirements.txt` is part of the release archive and installer path, so parity across all three runtime manifests is required. Regenerate the lockfile once with:

```bash
uv lock
uv lock --check
```

Expected: `uv.lock` contains `portalocker` and the project root metadata remains consistent.

- [ ] **Step 5: Run config tests**

Run: `uv run python -m pytest tests/memory/test_config.py -v`

Expected: 3 passed.

- [ ] **Step 6: Run config validation tests and lint**

Run: `uv run python -m pytest tests/memory/test_config.py tests/tools/test_settings_dialog.py tests/headless/test_provider_config.py -v`

Expected: PASS.

Run: `uvx ruff check lucnhan/constants.py lucnhan/core/config.py tests/memory/test_config.py`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add lucnhan/constants.py lucnhan/core/config.py pyproject.toml ida-plugin.json requirements.txt uv.lock tests/memory/test_config.py
git commit -m "feat(memory): add central memory configuration"
```

---

### Task 2: Workspace identity models and locator

**Files:**
- Create: `lucnhan/memory/workspace.py`
- Create: `tests/memory/test_workspace.py`

**Interfaces:**
- Consumes: `LucNhanConfig.memory_dir` from Task 1.
- Produces: `new_memory_id() -> str`, `new_case_id() -> str`, `new_record_id(kind: str) -> str`, `validate_memory_id(value: str) -> str`, `validate_case_id(value: str) -> str`, `validate_record_id(kind: str, value: str) -> str`.
- Produces: `FilesystemIdentity`, `IdentityRequest`, `WorkspaceBinding`, `WorkspacePaths`, `MemoryRunContext`, `MemoryLocator`.
- Consumed by: Tasks 3–9 and all later plans.

- [ ] **Step 1: Write failing ID and locator tests**

```python
from __future__ import annotations

from pathlib import Path

import re

import pytest

from lucnhan.memory.workspace import (
    MemoryLocator,
    MemoryRunContext,
    new_case_id,
    new_memory_id,
    new_record_id,
    validate_memory_id,
    validate_record_id,
)


def test_binary_locator_never_uses_display_name(tmp_path: Path) -> None:
    memory_id = new_memory_id()
    paths = MemoryLocator(tmp_path).binary(memory_id)

    assert paths.root == tmp_path / "binaries" / memory_id
    assert paths.database == paths.root / "memory.db"
    assert paths.markdown == paths.root / "MEMORY.md"
    assert paths.reports == paths.root / "notes" / "reports"
    assert paths.lock == paths.root / ".workspace.lock"


def test_generated_record_ids_are_opaque_and_strict() -> None:
    fact_id = new_record_id("fact")
    assert re.fullmatch(r"fact-[0-9a-f]{32}", fact_id)
    assert validate_record_id("fact", fact_id) == fact_id
    with pytest.raises(ValueError):
        validate_record_id("fact", "func:0x401000")


def test_invalid_memory_id_cannot_become_path_component() -> None:
    with pytest.raises(ValueError, match="memory_id"):
        validate_memory_id("../../outside")


def test_run_context_is_immutable() -> None:
    context = MemoryRunContext(
        binary_memory_id=new_memory_id(),
        active_case_id=new_case_id(),
        database_generation=4,
        case_binding_generation=2,
    )
    with pytest.raises(Exception):
        context.database_generation = 5  # type: ignore[misc]
```

- [ ] **Step 2: Run tests and verify import failure**

Run: `uv run python -m pytest tests/memory/test_workspace.py -v`

Expected: FAIL with `ModuleNotFoundError: lucnhan.memory.workspace`.

- [ ] **Step 3: Implement generated IDs and frozen data contracts**

```python
from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_MEMORY_ID_RE = re.compile(r"^mem-[0-9a-f]{32}$")
_CASE_ID_RE = re.compile(r"^case-[0-9a-f]{32}$")
_RECORD_KINDS = frozenset({"fact", "entity", "relation", "observation", "source", "promotion", "note", "report"})
_RECORD_ID_RE = re.compile(r"^(?P<kind>[a-z]+)-[0-9a-f]{32}$")


def new_memory_id() -> str:
    return f"mem-{uuid.uuid4().hex}"


def new_case_id() -> str:
    return f"case-{uuid.uuid4().hex}"


def new_record_id(kind: str) -> str:
    if kind not in _RECORD_KINDS:
        raise ValueError("invalid record kind")
    return f"{kind}-{uuid.uuid4().hex}"


def validate_record_id(kind: str, value: str) -> str:
    match = _RECORD_ID_RE.fullmatch(value)
    if kind not in _RECORD_KINDS or match is None or match.group("kind") != kind:
        raise ValueError(f"invalid {kind} record ID")
    return value


def validate_memory_id(value: str) -> str:
    if not _MEMORY_ID_RE.fullmatch(value):
        raise ValueError("invalid memory_id")
    return value


def validate_case_id(value: str) -> str:
    if not _CASE_ID_RE.fullmatch(value):
        raise ValueError("invalid case_id")
    return value


def validate_workspace_id(value: str) -> str:
    if _MEMORY_ID_RE.fullmatch(value) or _CASE_ID_RE.fullmatch(value):
        return value
    raise ValueError("invalid workspace_id")


@dataclass(frozen=True)
class FilesystemIdentity:
    volume_or_device: str
    file_or_inode: str

    @property
    def evidence_value(self) -> str:
        return f"{self.volume_or_device}:{self.file_or_inode}"


@dataclass(frozen=True)
class IdentityRequest:
    source_kind: Literal["idb", "raw"]
    idb_path: str
    db_instance_id: str = ""
    source_sha256: str = ""
    display_name: str = ""
    filesystem_identity: FilesystemIdentity | None = None


@dataclass(frozen=True)
class WorkspaceBinding:
    memory_id: str  # empty only when state is ephemeral/disabled; never a path component
    state: Literal["active", "provisional", "ephemeral", "disabled"]
    display_name: str
    warning: str = ""
    uuid_write_pending: bool = False


@dataclass(frozen=True)
class MemoryRunContext:
    binary_memory_id: str
    active_case_id: str
    database_generation: int
    case_binding_generation: int
```

- [ ] **Step 4: Implement canonical locator paths**

```python
@dataclass(frozen=True)
class WorkspacePaths:
    root: Path
    database: Path
    markdown: Path
    notes: Path
    reports: Path
    lock: Path


class MemoryLocator:
    def __init__(self, memory_root: str | Path):
        self.root = Path(memory_root)

    def registry_database(self) -> Path:
        return self.root / "registry.db"

    def binary(self, memory_id: str) -> WorkspacePaths:
        return self._workspace("binaries", validate_memory_id(memory_id))

    def case(self, case_id: str) -> WorkspacePaths:
        return self._workspace("cases", validate_case_id(case_id))

    def _workspace(self, group: str, workspace_id: str) -> WorkspacePaths:
        root = self.root / group / workspace_id
        notes = root / "notes"
        return WorkspacePaths(
            root=root,
            database=root / "memory.db",
            markdown=root / "MEMORY.md",
            notes=notes,
            reports=notes / "reports",
            lock=root / ".workspace.lock",
        )
```

- [ ] **Step 5: Run tests and type/lint checks**

Run: `uv run python -m pytest tests/memory/test_workspace.py -v`

Expected: 3 passed.

Run: `uvx ruff check lucnhan/memory/workspace.py tests/memory/test_workspace.py`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add lucnhan/memory/workspace.py tests/memory/test_workspace.py
git commit -m "feat(memory): define workspace identity contracts"
```

---

### Task 3: Safe SQLite backend and registry schema v1

**Files:**
- Create: `lucnhan/memory/sqlite_backend.py`
- Create: `lucnhan/memory/registry.py`
- Create: `tests/memory/test_sqlite_backend.py`
- Create: `tests/memory/test_registry.py`

**Interfaces:**
- Consumes: Task 1 schema constants and Task 2 IDs.
- Produces: `open_sqlite(path: Path, *, read_only: bool, expected_version: int, migrations: Mapping[int, Callable[[sqlite3.Connection], None]], allow_create: bool = False) -> sqlite3.Connection`; only registry/workspace initialization passes `allow_create=True`.
- Produces: `MemoryRegistry.initialize()`, `create_workspace()`, `find_evidence()`, `bind_evidence()`, `retire_evidence()`, `touch_path_alias()`, `workspace()`, `transaction()`.
- Consumed by: Task 4 resolver and Task 8 manager.

- [ ] **Step 1: Write failing backend tests**

```python
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lucnhan.memory.sqlite_backend import UnsupportedSchemaError, open_sqlite


def test_open_sqlite_enables_required_pragmas(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    conn = open_sqlite(
        db,
        read_only=False,
        expected_version=1,
        migrations={1: lambda conn: conn.execute("CREATE TABLE item(id TEXT PRIMARY KEY)")},
        allow_create=True,
    )
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        conn.close()


def test_read_only_open_is_query_only_and_windows_safe(tmp_path: Path) -> None:
    db = tmp_path / "space and ünicode.db"
    writer = open_sqlite(
        db,
        read_only=False,
        expected_version=1,
        migrations={1: lambda conn: conn.execute("CREATE TABLE item(id TEXT PRIMARY KEY)")},
        allow_create=True,
    )
    writer.close()

    reader = open_sqlite(db, read_only=True, expected_version=1, migrations={})
    try:
        assert reader.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("INSERT INTO item VALUES ('x')")
    finally:
        reader.close()


def test_newer_schema_is_rejected_without_mutation(tmp_path: Path) -> None:
    db = tmp_path / "newer.db"
    raw = sqlite3.connect(db)
    raw.execute("PRAGMA user_version = 9")
    raw.close()

    with pytest.raises(UnsupportedSchemaError):
        open_sqlite(
            db,
            read_only=False,
            expected_version=1,
            migrations={1: lambda conn: conn.execute("SELECT 1")},
        )
```

- [ ] **Step 2: Write failing registry tests**

```python
from __future__ import annotations

from pathlib import Path

import pytest

from lucnhan.memory.registry import EvidenceConflictError, MemoryRegistry
from lucnhan.memory.workspace import new_memory_id


def test_registry_current_filesystem_evidence_is_unique(tmp_path: Path) -> None:
    registry = MemoryRegistry(tmp_path / "registry.db")
    registry.initialize()
    first = registry.create_workspace("binary", "a.i64", memory_id=new_memory_id())
    second = registry.create_workspace("binary", "b.i64", memory_id=new_memory_id())
    registry.bind_evidence(first.memory_id, "filesystem", "vol:inode", status="current")

    with pytest.raises(EvidenceConflictError):
        registry.bind_evidence(second.memory_id, "filesystem", "vol:inode", status="current")


def test_db_instance_evidence_may_identify_copied_files(tmp_path: Path) -> None:
    registry = MemoryRegistry(tmp_path / "registry.db")
    registry.initialize()
    first = registry.create_workspace("binary", "a.i64")
    second = registry.create_workspace("binary", "a-copy.i64")

    registry.bind_evidence(first.memory_id, "db_instance", "same-uuid", status="current")
    registry.bind_evidence(second.memory_id, "db_instance", "same-uuid", status="current")

    assert {w.memory_id for w in registry.find_evidence("db_instance", "same-uuid")} == {
        first.memory_id,
        second.memory_id,
    }
```

- [ ] **Step 3: Run tests and verify missing modules**

Run: `uv run python -m pytest tests/memory/test_sqlite_backend.py tests/memory/test_registry.py -v`

Expected: FAIL with missing modules.

- [ ] **Step 4: Implement SQLite open and migration transaction**

```python
from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import quote


class UnsupportedSchemaError(RuntimeError):
    pass


class SchemaMigrationRequired(RuntimeError):
    pass


class UnsupportedStorageError(RuntimeError):
    pass


def begin_immediate_with_retry(
    conn: sqlite3.Connection,
    *,
    attempts: int = 4,
    initial_backoff_seconds: float = 0.025,
) -> None:
    delay = initial_backoff_seconds
    for attempt in range(attempts):
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt + 1 == attempts:
                raise
            time.sleep(delay)
            delay *= 2


def open_sqlite(
    path: Path,
    *,
    read_only: bool,
    expected_version: int,
    migrations: Mapping[int, Callable[[sqlite3.Connection], None]],
    allow_create: bool = False,
) -> sqlite3.Connection:
    path = path.resolve()
    if not path.is_file() and not allow_create:
        raise FileNotFoundError(path)
    if read_only:
        uri_path = quote(path.as_posix(), safe="/:")
        conn = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True, timeout=5.0)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        if allow_create and not path.exists():
            conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        else:
            uri_path = quote(path.as_posix(), safe="/:")
            conn = sqlite3.connect(
                f"file:{uri_path}?mode=rw",
                uri=True,
                timeout=5.0,
                isolation_level=None,
            )
            # mode=rw is the no-recreation guard for existing writable opens.
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if read_only:
        conn.execute("PRAGMA query_only = ON")
    else:
        mode = str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
        if mode != "wal":
            conn.close()
            raise UnsupportedStorageError(f"SQLite WAL unavailable for {path}")
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > expected_version:
        conn.close()
        raise UnsupportedSchemaError(f"schema {version} is newer than supported {expected_version}")
    if read_only and version < expected_version:
        conn.close()
        raise SchemaMigrationRequired(f"schema {version} requires migration to {expected_version}")
    for target in range(version + 1, expected_version + 1):
        begin_immediate_with_retry(conn)
        try:
            migrations[target](conn)
            conn.execute(f"PRAGMA user_version = {target}")
            conn.commit()
        except BaseException:
            conn.rollback()
            conn.close()
            raise
    return conn
```

- [ ] **Step 5: Implement registry v1 tables and partial uniqueness**

Use one migration callable containing complete v1 DDL. `MemoryRegistry.initialize()` passes `allow_create=True`; every later registry open leaves it false. Before migrating any existing DB, create a private pre-migration copy with `sqlite3.Connection.backup()`, fsync it, and atomically place it under `<memory_dir>/backups/migrations/`; retain it if migration fails. Fresh empty creation needs no backup. Execute each statement through `conn.execute()` inside the transaction created by `open_sqlite()`; do not use `executescript()` because it implicitly commits pending transactions:

```sql
CREATE TABLE workspaces(
    memory_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('binary','raw')),
    state TEXT NOT NULL CHECK(state IN ('active','provisional','disabled','retired')),
    display_name TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen_at REAL NOT NULL
);
CREATE TABLE identity_evidence(
    evidence_id INTEGER PRIMARY KEY,
    memory_id TEXT NOT NULL REFERENCES workspaces(memory_id),
    kind TEXT NOT NULL CHECK(kind IN ('filesystem','db_instance','raw_sha256')),
    value TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('current','retired','pending')),
    created_at REAL NOT NULL,
    retired_at REAL
);
CREATE UNIQUE INDEX uq_current_durable_evidence
ON identity_evidence(kind, value)
WHERE status='current' AND kind IN ('filesystem','raw_sha256');
CREATE UNIQUE INDEX uq_workspace_evidence
ON identity_evidence(memory_id, kind, value, status);
CREATE TABLE path_aliases(
    alias_id INTEGER PRIMARY KEY,
    memory_id TEXT NOT NULL REFERENCES workspaces(memory_id),
    normalized_path TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('current','retired')),
    last_seen_at REAL NOT NULL
);
CREATE INDEX ix_path_alias ON path_aliases(normalized_path);
CREATE TABLE legacy_sources(
    source_fingerprint TEXT PRIMARY KEY,
    path_metadata TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('detected','dismissed','imported')),
    last_seen_at REAL NOT NULL
);
```

Implement `MemoryRegistry.transaction()` as a context manager using `BEGIN IMMEDIATE`, and map `sqlite3.IntegrityError` on current durable evidence to `EvidenceConflictError`. Because all write paths share one low-level transaction boundary, no registry method opens its own nested transaction.

Implement bounded retry at `open_sqlite`/registry transaction entry only:

```python
def begin_immediate_with_retry(
    conn: sqlite3.Connection,
    *,
    attempts: int = 4,
    initial_backoff_seconds: float = 0.025,
) -> None:
    delay = initial_backoff_seconds
    for attempt in range(attempts):
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt + 1 == attempts:
                raise
            time.sleep(delay)
            delay *= 2
```

This snippet is the same implementation already placed in `sqlite_backend.py`; do not duplicate a second helper in `registry.py`. Retry only lock acquisition; never replay a partially executed transaction. Add an injected lock-contention test proving attempts are bounded and final failure is surfaced.

- [ ] **Step 6: Run backend/registry tests**

Run: `uv run python -m pytest tests/memory/test_sqlite_backend.py tests/memory/test_registry.py -v`

Expected: PASS.

- [ ] **Step 7: Add two-process first-open serialization test**

Add `MemoryRegistry.resolve_or_create_raw(source_sha256: str, display_name: str) -> WorkspaceRecord`: within one `BEGIN IMMEDIATE`, query current `raw_sha256` evidence, return its workspace when present, otherwise create one raw workspace plus evidence atomically. Add a top-level multiprocessing worker to `tests/memory/test_registry.py` that opens the same registry, calls `resolve_or_create_raw("a" * 64, "sample.bin")`, and returns the ID. Assert two spawned processes return one ID and one row.

Run: `uv run python -m pytest tests/memory/test_registry.py -v`

Expected: PASS without `database is locked` or duplicate workspace.

- [ ] **Step 8: Commit**

```bash
git add lucnhan/memory/sqlite_backend.py lucnhan/memory/registry.py tests/memory/test_sqlite_backend.py tests/memory/test_registry.py
git commit -m "feat(memory): add sqlite registry foundation"
```

---

### Task 4: Filesystem identity and ordered resolver

**Files:**
- Create: `lucnhan/memory/identity.py`
- Create: `tests/memory/test_identity.py`
- Modify: `lucnhan/memory/registry.py`

**Interfaces:**
- Consumes: `IdentityRequest`, `FilesystemIdentity`, `MemoryRegistry`.
- Produces: `IdentityResolution`, `IdentityChoice`, `ResolutionStatus`, `get_filesystem_identity(path)`, `hash_raw_binary(path)`, `MemoryIdentityResolver.resolve(request, choice=None)`.
- Consumed by: Tasks 5 and 8.

- [ ] **Step 1: Write table-driven failing resolver tests**

```python
from __future__ import annotations

from pathlib import Path

import pytest

from lucnhan.memory.identity import IdentityChoice, MemoryIdentityResolver, ResolutionStatus
from lucnhan.memory.registry import MemoryRegistry
from lucnhan.memory.workspace import FilesystemIdentity, IdentityRequest


def _idb(path: Path, uuid: str, fs_value: tuple[str, str]) -> IdentityRequest:
    return IdentityRequest(
        source_kind="idb",
        idb_path=str(path),
        db_instance_id=uuid,
        display_name=path.name,
        filesystem_identity=FilesystemIdentity(*fs_value),
    )


def test_reopen_and_rename_follow_filesystem_identity(tmp_path: Path) -> None:
    registry = MemoryRegistry(tmp_path / "registry.db")
    registry.initialize()
    resolver = MemoryIdentityResolver(registry)
    first = resolver.resolve(_idb(tmp_path / "a.i64", "uuid-a", ("vol", "7")))
    moved = resolver.resolve(_idb(tmp_path / "renamed.i64", "uuid-a", ("vol", "7")))

    assert first.binding is not None
    assert moved.binding is not None
    assert moved.binding.memory_id == first.binding.memory_id


def test_copy_detaches_when_original_binding_is_current(tmp_path: Path) -> None:
    registry = MemoryRegistry(tmp_path / "registry.db")
    registry.initialize()
    resolver = MemoryIdentityResolver(registry, path_exists=lambda path: path.endswith("a.i64"))
    original = resolver.resolve(_idb(tmp_path / "a.i64", "uuid-a", ("vol", "7")))
    copied = resolver.resolve(_idb(tmp_path / "copy.i64", "uuid-a", ("vol", "8")))

    assert original.binding is not None
    assert copied.binding is not None
    assert copied.status is ResolutionStatus.COPY_DETACHED
    assert copied.binding.memory_id != original.binding.memory_id
    assert copied.netnode_uuid_to_persist


def test_missing_prior_file_requires_explicit_choice(tmp_path: Path) -> None:
    registry = MemoryRegistry(tmp_path / "registry.db")
    registry.initialize()
    resolver = MemoryIdentityResolver(registry, path_exists=lambda _path: False)
    original = resolver.resolve(_idb(tmp_path / "a.i64", "uuid-a", ("vol", "7")))
    assert original.binding is not None
    ambiguous = resolver.resolve(_idb(tmp_path / "moved.i64", "uuid-a", ("new", "9")))
    assert ambiguous.status is ResolutionStatus.AMBIGUOUS
    assert ambiguous.binding is None

    linked = resolver.resolve(
        _idb(tmp_path / "moved.i64", "uuid-a", ("new", "9")),
        IdentityChoice.link_existing(original.binding.memory_id),
    )
    assert linked.binding is not None
    assert linked.binding.memory_id == original.binding.memory_id

    offline = resolver.resolve(
        _idb(tmp_path / "other.i64", "uuid-a", ("new", "10")),
        IdentityChoice.without_persistence(),
    )
    assert offline.status is ResolutionStatus.EPHEMERAL
    assert offline.binding is not None
    assert offline.binding.memory_id == ""
```

Add tests for: filesystem/UUID conflict, same-path replacement, raw SHA exact reuse, path-only never resolves, no durable evidence returns ephemeral, retired file reappearance does not silently link.

- [ ] **Step 2: Run tests and verify missing resolver**

Run: `uv run python -m pytest tests/memory/test_identity.py -v`

Expected: FAIL with missing module.

- [ ] **Step 3: Implement filesystem and raw hashing primitives**

```python
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from .workspace import FilesystemIdentity, WorkspaceBinding


def get_filesystem_identity(path: str) -> FilesystemIdentity | None:
    if os.name == "nt":
        return _get_windows_file_identity(path)
    try:
        stat = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    if not stat.st_ino:
        return None
    return FilesystemIdentity(str(stat.st_dev), str(stat.st_ino))


def _get_windows_file_identity(path: str) -> FilesystemIdentity | None:
    """Return volume serial + 64-bit file index from a no-follow Windows handle."""
    # Define BY_HANDLE_FILE_INFORMATION with ctypes; open via CreateFileW using
    # FILE_READ_ATTRIBUTES, FILE_SHARE_READ|WRITE|DELETE, OPEN_EXISTING and
    # FILE_FLAG_OPEN_REPARSE_POINT|FILE_FLAG_BACKUP_SEMANTICS. Call
    # GetFileInformationByHandle, always CloseHandle, then combine
    # (nFileIndexHigh << 32) | nFileIndexLow. Return None on API failure.


def hash_raw_binary(path: str) -> str:
    before = os.stat(path, follow_symlinks=False)
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    after = os.stat(path, follow_symlinks=False)
    before_key = (before.st_size, before.st_mtime_ns)
    after_key = (after.st_size, after.st_mtime_ns)
    if before_key != after_key:
        raise RuntimeError("raw input changed while hashing")
    return digest.hexdigest()
```

Implement `_get_windows_file_identity()` fully in this task (the comment above is implementation pseudocode, not a permitted placeholder): Windows identity is volume serial + full file index. POSIX uses `st_dev/st_ino`. Tests inject `FilesystemIdentity` for decision-table determinism and monkeypatch the Windows API wrapper for handle close, high/low index composition, unavailable identity, rename, copy, same-path replacement, and rapid file-index reuse behavior.

- [ ] **Step 4: Implement explicit resolution result contracts**

```python
class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    CREATED = "created"
    COPY_DETACHED = "copy_detached"
    AMBIGUOUS = "ambiguous"
    CONFLICT = "conflict"
    EPHEMERAL = "ephemeral"


@dataclass(frozen=True)
class IdentityChoice:
    action: str
    memory_id: str = ""

    @classmethod
    def link_existing(cls, memory_id: str) -> "IdentityChoice":
        return cls("link_existing", memory_id)

    @classmethod
    def start_fresh(cls) -> "IdentityChoice":
        return cls("start_fresh")

    @classmethod
    def without_persistence(cls) -> "IdentityChoice":
        return cls("without_persistence")


@dataclass(frozen=True)
class IdentityResolution:
    status: ResolutionStatus
    binding: WorkspaceBinding | None
    candidates: tuple[str, ...] = ()
    netnode_uuid_to_persist: str = ""
    warning: str = ""
```

- [ ] **Step 5: Implement resolver in the spec's exact priority order**

Within one registry transaction:

1. `raw`: validate `[0-9a-f]{64}`, resolve/create current `raw_sha256` evidence.
2. `idb`: resolve filesystem evidence first; incompatible UUID returns `CONFLICT`.
3. UUID + new filesystem + old current path exists returns `COPY_DETACHED` and a new UUID action.
4. UUID + old unavailable returns `AMBIGUOUS` unless explicit choice supplied. UI maps the three choices exactly to `link_existing(candidate)`, `start_fresh()`, and `without_persistence()`; headless defaults to `start_fresh()` with a structured warning unless an explicit link ID is supplied.
5. `without_persistence()` returns an ephemeral/disabled binding and performs no registry/workspace mutation.
6. Link retires old filesystem/path evidence before binding new evidence.
7. Path alone never resolves.
8. No filesystem/UUID evidence returns an ephemeral binding with no directory.

Do not call IDA APIs from this host-agnostic module.

- [ ] **Step 6: Run resolver tests**

Run: `uv run python -m pytest tests/memory/test_identity.py tests/memory/test_registry.py -v`

Expected: PASS.

- [ ] **Step 7: Run copy/rename integration using real temporary files**

Add a test that writes `a.i64`, records `get_filesystem_identity`, renames it, and asserts identity equality; copy it and assert inequality. Skip only if the platform reports no inode/file index.

Run: `uv run python -m pytest tests/memory/test_identity.py -v`

Expected: PASS on Windows, Linux, and macOS.

- [ ] **Step 8: Commit**

```bash
git add lucnhan/memory/identity.py lucnhan/memory/registry.py tests/memory/test_identity.py
git commit -m "feat(memory): resolve copy and move identities"
```

---

### Task 5: Raw-headless SHA identity transport

**Files:**
- Modify: `lucnhan/cli/headless.py:134-167,486-510,540-570`
- Modify: `lucnhan/ida/headless_bootstrap.py:303-355`
- Modify: `lucnhan/ida/headless_controller.py:25-58`
- Create: `tests/cli/test_headless_memory_identity.py`
- Modify: `tests/ida/test_headless_bootstrap.py`

**Interfaces:**
- Consumes: `hash_raw_binary()` from Task 4.
- Produces bootstrap key `memory_source` with `{kind, original_path, sha256}`.
- Produces: `validate_bootstrap_memory_source(value: object) -> IdentityRequest | None`.
- Consumed by: Task 8 manager/controller binding.

- [ ] **Step 1: Write failing CLI payload tests**

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from lucnhan.cli.headless import _build_memory_source


def test_raw_input_carries_sha256_before_ida_launch(tmp_path: Path) -> None:
    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"abc")

    source = _build_memory_source(str(sample))

    assert source == {
        "kind": "raw",
        "original_path": str(sample.resolve()),
        "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    }


def test_existing_idb_does_not_hash_database(tmp_path: Path) -> None:
    idb = tmp_path / "sample.i64"
    idb.write_bytes(b"idb")
    with patch("lucnhan.cli.headless.hash_raw_binary") as hash_mock:
        source = _build_memory_source(str(idb))
    assert source == {"kind": "idb", "original_path": str(idb.resolve())}
    hash_mock.assert_not_called()
```

- [ ] **Step 2: Run and verify missing helper**

Run: `uv run python -m pytest tests/cli/test_headless_memory_identity.py -v`

Expected: FAIL because `_build_memory_source` does not exist.

- [ ] **Step 3: Implement CLI source payload and attach it to ask/server configs**

```python
def _build_memory_source(binary: str) -> dict[str, str]:
    resolved = os.path.realpath(os.path.abspath(binary))
    if resolved.lower().endswith((".i64", ".idb")):
        return {"kind": "idb", "original_path": resolved}
    return {
        "kind": "raw",
        "original_path": resolved,
        "sha256": hash_raw_binary(resolved),
    }
```

Set `bootstrap_cfg["memory_source"] = _build_memory_source(binary)` in both `cmd_ask` and `cmd_serve` before `_launch_ida_*`.

- [ ] **Step 4: Validate bootstrap values without trusting JSON types**

```python
def validate_bootstrap_memory_source(value: object) -> IdentityRequest | None:
    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    original_path = value.get("original_path")
    if kind not in {"idb", "raw"} or not isinstance(original_path, str):
        return None
    digest = ""
    if kind == "raw":
        raw_digest = value.get("sha256")
        if not isinstance(raw_digest, str) or re.fullmatch(r"[0-9a-f]{64}", raw_digest) is None:
            return None
        digest = raw_digest
    return IdentityRequest(
        source_kind=kind,
        idb_path=original_path,
        source_sha256=digest,
        display_name=Path(original_path).name,
    )
```

Bootstrap rejects malformed raw identity with exit code 2 rather than falling back to the temporary IDB. Pass the validated immutable request into the concrete constructor:

```python
controller = HeadlessSessionController(
    rk_config,
    dispatcher,
    wait_for_auto_analysis=wait_auto,
    memory_source=memory_source,
)
```

Extend `HeadlessSessionController.__init__` with `memory_source: IdentityRequest | None = None` and forward only that typed immutable value to `SessionControllerBase` as injected identity evidence.

- [ ] **Step 5: Add bootstrap malformed-hash tests**

Test a raw source with uppercase/short/non-string digest and assert `_clean_exit_ida(2, ...)` is reached. Test IDB source without hash is accepted.

Run: `uv run python -m pytest tests/cli/test_headless_memory_identity.py tests/ida/test_headless_bootstrap.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add lucnhan/cli/headless.py lucnhan/ida/headless_bootstrap.py lucnhan/ida/headless_controller.py tests/cli/test_headless_memory_identity.py tests/ida/test_headless_bootstrap.py
git commit -m "feat(memory): carry raw binary identity headlessly"
```

---

### Task 6: Authoritative workspace SQLite store

**Files:**
- Create: `lucnhan/memory/workspace_store.py`
- Create: `tests/memory/test_workspace_store.py`

**Interfaces:**
- Consumes: `open_sqlite()`, `WorkspacePaths`, `MEMORY_WORKSPACE_SCHEMA_VERSION`.
- Produces immutable records: `FactRecord`, `FactRevision`, `EntityRecord`, `RelationRecord`, `ObservationRecord`, `ProjectionState`.
- Produces: generated `new_record_id(kind) -> str` (`fact-`, `entity-`, `relation-`, `observation-` + 32 lowercase hex), `WorkspaceStore.create(paths, owner_memory_id, workspace_kind="binary")`, `open(paths, owner_memory_id, read_only)`, `put_fact()`, `get_fact()`, `list_facts()`, `put_entity()`, `put_relation()`, `append_observation()`, `projection_state()`, `mark_projection()`.
- Consumed by: Task 7 and atomic-cutover plan.

- [ ] **Step 1: Write failing fact/revision tests**

```python
from __future__ import annotations

from pathlib import Path

import pytest

from lucnhan.memory.workspace import MemoryLocator, new_memory_id
from lucnhan.memory.workspace_store import StaleRevisionError, WorkspaceStore


def test_fact_revision_is_atomic_and_owner_scoped(tmp_path: Path) -> None:
    memory_id = new_memory_id()
    paths = MemoryLocator(tmp_path).binary(memory_id)
    store = WorkspaceStore.create(paths, owner_memory_id=memory_id)
    first = store.put_fact(
        fact_id="fact-11111111111111111111111111111111",
        fact_type="algorithm",
        title="RC4",
        content="Uses RC4 for C2 traffic",
        confidence=0.8,
        expected_revision=0,
    )
    second = store.put_fact(
        fact_id="fact-11111111111111111111111111111111",
        fact_type="algorithm",
        title="RC4",
        content="Uses modified RC4 for C2 traffic",
        confidence=0.9,
        expected_revision=1,
    )

    assert first.owner_memory_id == memory_id
    assert second.revision == 2
    assert store.get_fact("fact-11111111111111111111111111111111").content == "Uses modified RC4 for C2 traffic"


def test_stale_expected_revision_is_rejected(tmp_path: Path) -> None:
    memory_id = new_memory_id()
    store = WorkspaceStore.create(MemoryLocator(tmp_path).binary(memory_id), memory_id)
    store.put_fact("fact-22222222222222222222222222222222", "fact", "A", "first", 0.5, expected_revision=0)
    with pytest.raises(StaleRevisionError):
        store.put_fact("fact-22222222222222222222222222222222", "fact", "A", "stale", 0.6, expected_revision=0)
```

- [ ] **Step 2: Write failing read-only/missing DB tests**

```python
def test_missing_database_is_never_recreated_by_open(tmp_path: Path) -> None:
    memory_id = new_memory_id()
    paths = MemoryLocator(tmp_path).binary(memory_id)
    with pytest.raises(FileNotFoundError):
        WorkspaceStore.open(paths, owner_memory_id=memory_id, read_only=True)
    with pytest.raises(FileNotFoundError):
        WorkspaceStore.open(paths, owner_memory_id=memory_id, read_only=False)
    assert not paths.database.exists()
```

- [ ] **Step 3: Run and verify missing store**

Run: `uv run python -m pytest tests/memory/test_workspace_store.py -v`

Expected: FAIL with missing module.

- [ ] **Step 4: Implement workspace v1 DDL**

Create all tables specified by the design: `workspace_meta`, `facts`, `fact_revisions`, `entities`, `entity_aliases`, `relations`, `observations`, `sources`, `note_index`, `projection_state`, `promotions`, and `import_receipts`. Add `CHECK` constraints for generated record-ID prefixes/32-hex suffixes on authoritative IDs; legacy deterministic IDs live only in the cutover compatibility map. `sources` must store `owner_memory_id`, `source_record_id`, `source_revision`, `content_hash`, kind/locator and optional namespaced address so later promotion/source-drift checks never depend on opaque strings. Store `owner_workspace_id` plus `workspace_kind` (`binary|case`) in immutable `workspace_meta`, validate the ID with `validate_workspace_id()`, and validate both fields on every open. Binary repositories expose it as `owner_memory_id`; case repositories use `case_id`. Seed one `projection_state` row.

Use JSON only for bounded metadata columns; facts and relation keys stay queryable SQL columns.

- [ ] **Step 5: Implement expected-revision fact transaction and private SQL helpers**

```python
import math


def put_fact(
    self,
    fact_id: str,
    fact_type: str,
    title: str,
    content: str,
    confidence: float,
    *,
    expected_revision: int,
) -> FactRecord:
    validate_record_id("fact", fact_id)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be finite and within [0, 1]")
    with self.transaction():
        row = self._conn.execute(
            "SELECT current_revision FROM facts WHERE fact_id = ?",
            (fact_id,),
        ).fetchone()
        current = int(row[0]) if row is not None else 0
        if current != expected_revision:
            raise StaleRevisionError(f"expected revision {expected_revision}, found {current}")
        revision = current + 1
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self._write_fact_revision(fact_id, revision, content, content_hash, confidence)
        self._upsert_fact_head(fact_id, revision, fact_type, title)
    return self.get_fact(fact_id)
```

Implement `new_record_id(kind)` and `validate_record_id(kind, value)` beside the record dataclasses. Implement `_write_fact_revision()` and `_upsert_fact_head()` as private `WorkspaceStore` methods immediately below `put_fact()`: the former inserts the immutable revision row and the latter inserts/updates the current head without starting another transaction. Implement analogous deterministic upsert/revision rules for entities and local relations. Observation IDs are immutable append-only generated IDs.

- [ ] **Step 6: Implement projection-state API**

`ProjectionState` contains `managed_hash`, `unmanaged_hash`, `projection_dirty`, `projection_conflict`, and `projected_revision`. `mark_projection_dirty()` and `mark_projection_clean(expected_projected_revision, hashes)` update it transactionally.

- [ ] **Step 7: Run workspace tests**

Run: `uv run python -m pytest tests/memory/test_workspace_store.py -v`

Expected: PASS.

- [ ] **Step 8: Add two-process optimistic-concurrency test**

Spawn two processes attempting `expected_revision=1` after the same first record. Assert one succeeds, one returns `StaleRevisionError`, final revision is 2, and no update is lost.

Run: `uv run python -m pytest tests/memory/test_workspace_store.py -v`

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add lucnhan/memory/workspace_store.py tests/memory/test_workspace_store.py
git commit -m "feat(memory): add workspace sqlite store"
```

---

### Task 7: Deterministic `MEMORY.md` document and projector

**Files:**
- Create: `lucnhan/memory/markdown.py`
- Create: `tests/memory/test_markdown.py`

**Interfaces:**
- Consumes: `portalocker`, `WorkspaceStore`, `WorkspacePaths`, lock timeout constant.
- Produces: `ManagedEntry`, `MemoryDocument`, `parse_memory_document()`, `render_memory_document()`, `MemoryProjector.project()`.
- Consumed by: atomic-cutover prompt/writer tasks.

- [ ] **Step 1: Write failing parser/render tests**

```python
from __future__ import annotations

from pathlib import Path

import pytest

from lucnhan.memory.markdown import ManagedRegionError, parse_memory_document, render_memory_document
from lucnhan.memory.workspace_store import FactRecord


def test_render_preserves_unmanaged_text_and_embeds_record_revision() -> None:
    original = "# Memory\n\n## User Notes\nKeep this line.\n"
    fact = FactRecord(
        fact_id="fact-33333333333333333333333333333333",
        owner_memory_id="mem-" + "1" * 32,
        revision=3,
        fact_type="protocol",
        title="Protocol",
        content="Uses RC4",
        confidence=0.8,
        state="current",
    )

    rendered = render_memory_document(parse_memory_document(original), [fact])

    assert "Keep this line." in rendered
    assert "<!-- lucnhan:record id=fact-33333333333333333333333333333333 rev=3 -->" in rendered
    assert rendered.count("<!-- lucnhan:managed:start -->") == 1
    assert rendered.count("<!-- lucnhan:managed:end -->") == 1


def test_nested_or_reversed_markers_are_conflicts() -> None:
    content = "<!-- lucnhan:managed:end -->\n<!-- lucnhan:managed:start -->"
    with pytest.raises(ManagedRegionError):
        parse_memory_document(content)
```

- [ ] **Step 2: Write failing atomic projection conflict test**

Create a store with one fact, write a `MEMORY.md` with user notes, inject a `before_replace` callback that edits the unmanaged region, call projector, and assert `ProjectionConflictError`, preserved manual edit, and `projection_conflict=True`.

- [ ] **Step 3: Run tests and verify missing module**

Run: `uv run python -m pytest tests/memory/test_markdown.py -v`

Expected: FAIL with missing module.

- [ ] **Step 4: Implement managed-region parser**

```python
MANAGED_START = "<!-- lucnhan:managed:start -->"
MANAGED_END = "<!-- lucnhan:managed:end -->"
_RECORD_RE = re.compile(r"<!-- lucnhan:record id=([A-Za-z0-9._:-]+) rev=([1-9][0-9]*) -->")


@dataclass(frozen=True)
class MemoryDocument:
    prefix: str
    managed: str
    suffix: str
    managed_hash: str
    unmanaged_hash: str


def parse_memory_document(content: str) -> MemoryDocument:
    starts = [match.start() for match in re.finditer(re.escape(MANAGED_START), content)]
    ends = [match.start() for match in re.finditer(re.escape(MANAGED_END), content)]
    if not starts and not ends:
        digest = _sha256(content)
        return MemoryDocument(content, "", "", _sha256(""), digest)
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ManagedRegionError("invalid managed-region delimiters")
    start_body = starts[0] + len(MANAGED_START)
    prefix = content[: starts[0]]
    managed = content[start_body : ends[0]]
    suffix = content[ends[0] + len(MANAGED_END) :]
    return MemoryDocument(
        prefix=prefix,
        managed=managed,
        suffix=suffix,
        managed_hash=_sha256(managed),
        unmanaged_hash=_sha256(prefix + suffix),
    )
```

- [ ] **Step 5: Implement deterministic local renderer**

Sort current facts by `(fact_type, title.casefold(), fact_id)`. Render exact hidden ID/revision markers and escaped single-line list text. Do not call any external subsystem. Preserve `prefix + suffix` byte-for-byte except normalized insertion point when no managed region exists. Reject an existing source file above the configured Markdown cap before reading it; new documents use the canonical `# Memory` skeleton.

- [ ] **Step 6: Implement locked projector**

```python
with portalocker.Lock(str(paths.lock), mode="a", timeout=MEMORY_LOCK_TIMEOUT_SECONDS):
    latest_facts = store.list_facts(state="current")
    before = _read_bounded_regular_utf8(paths.markdown, default="# Memory\n")
    document = parse_memory_document(before)
    rendered = render_memory_document(document, latest_facts)
    current = _read_bounded_regular_utf8(paths.markdown, default="# Memory\n")
    if _sha256(current) != _sha256(before):
        store.mark_projection_conflict()
        raise ProjectionConflictError("MEMORY.md changed during projection")
    _atomic_replace_regular_file(paths.markdown, rendered)
    store.mark_projection_clean(
        managed_hash=parse_memory_document(rendered).managed_hash,
        unmanaged_hash=parse_memory_document(rendered).unmanaged_hash,
    )
```

Define `_read_bounded_regular_utf8()` and `_atomic_replace_regular_file()` in `markdown.py`: both enforce containment/regular-file checks, bounded UTF-8 reads, same-directory temporary writes, flush + `os.fsync`, and atomic `os.replace`; the hardening plan later centralizes these in `StorageGuard`. Map `portalocker.AlreadyLocked`/`LockException` to `ProjectionLockTimeout`, mark dirty, and leave SQLite committed state intact. Reject symlink/reparse/non-regular targets immediately before replace.

- [ ] **Step 7: Add real two-process lock test**

Use two spawned processes projecting different committed facts into one workspace. Assert final Markdown contains both latest facts, one managed region, intact user notes, and no temporary files.

Run: `uv run python -m pytest tests/memory/test_markdown.py -v`

Expected: PASS.

- [ ] **Step 8: Run formatter/lint and commit**

Run: `uvx ruff format --check lucnhan/memory/markdown.py tests/memory/test_markdown.py`

Run: `uvx ruff check lucnhan/memory/markdown.py tests/memory/test_markdown.py`

Expected: PASS.

```bash
git add lucnhan/memory/markdown.py tests/memory/test_markdown.py
git commit -m "feat(memory): project sqlite facts to markdown"
```

---

### Task 8: Session binding, manager façade, and database generations

**Files:**
- Create: `lucnhan/memory/manager.py`
- Modify: `lucnhan/state/session.py:53-75`
- Modify: `lucnhan/state/history.py:35,140-166,275-296,438-550`
- Modify: `lucnhan/constants.py:33-35`
- Modify: `lucnhan/ui/session_controller_base.py:91-110,193-221,459-490,586-673`
- Modify: `lucnhan/ida/headless_controller.py:25-58`
- Create: `tests/memory/test_manager.py`
- Create: `tests/state/test_memory_binding.py`
- Modify: `tests/agent/test_session_controller.py`

**Interfaces:**
- Consumes: resolver, registry, locator, `memory_source` bootstrap.
- Produces: `MemoryWorkspaceManager.bind() -> IdentityResolution`, `run_context(active_case_id="") -> MemoryRunContext`, `validate_run_context(context) -> bool`.
- Adds durable `SessionState.binary_memory_id` and `active_case_id`; process-local generations live only in `MemoryWorkspaceManager`/`MemoryRunContext`, not serialized session JSON.
- Consumed by: atomic-cutover service/loop plan.

- [ ] **Step 1: Write failing session round-trip tests**

```python
from __future__ import annotations

from pathlib import Path

from lucnhan.core.config import LucNhanConfig
from lucnhan.state.history import SessionHistory
from lucnhan.state.session import SessionState


def test_session_and_manifest_round_trip_memory_binding(tmp_path: Path) -> None:
    config = LucNhanConfig()
    config._config_dir = str(tmp_path)
    session = SessionState(
        id="bound-session",
        idb_path="C:/samples/a.i64",
        db_instance_id="uuid-a",
        binary_memory_id="mem-" + "a" * 32,
        active_case_id="case-" + "b" * 32,
    )
    history = SessionHistory(config)
    history.save_session(session)
    loaded = history.load_session(session.id)

    assert loaded.binary_memory_id == session.binary_memory_id
    assert loaded.active_case_id == session.active_case_id
    summaries = history.list_sessions(binary_memory_id=session.binary_memory_id)
    assert [item["id"] for item in summaries] == [session.id]
```

- [ ] **Step 2: Write failing manager generation tests**

```python
from lucnhan.memory.manager import MemoryWorkspaceManager
from lucnhan.memory.workspace import FilesystemIdentity, IdentityRequest


def test_rebinding_database_invalidates_old_run_context(tmp_path: Path) -> None:
    config = LucNhanConfig()
    config._config_dir = str(tmp_path)
    config.memory_workspaces_enabled = True
    manager = MemoryWorkspaceManager(config)
    first = manager.bind(
        IdentityRequest("idb", "a.i64", "uuid-a", filesystem_identity=FilesystemIdentity("v", "1"))
    )
    old_context = manager.run_context()
    second = manager.bind(
        IdentityRequest("idb", "b.i64", "uuid-b", filesystem_identity=FilesystemIdentity("v", "2"))
    )

    assert first.binding is not None
    assert second.binding is not None
    assert manager.validate_run_context(old_context) is False
```

- [ ] **Step 3: Run tests and verify failure**

Run: `uv run python -m pytest tests/state/test_memory_binding.py tests/memory/test_manager.py -v`

Expected: FAIL because fields/manager do not exist.

- [ ] **Step 4: Add session/manifest schema v2 fields**

Set `SESSION_SCHEMA_VERSION = 2` and `MANIFEST_SCHEMA_VERSION = 2`. Serialize only durable bindings:

```python
"binary_memory_id": session.binary_memory_id,
"active_case_id": session.active_case_id,
```

Do not serialize `database_generation` or `case_binding_generation`: they are fresh process-local counters initialized by `MemoryWorkspaceManager` after identity/case validation. `list_sessions()` accepts `binary_memory_id`. If supplied, it is authoritative; path/UUID remain compatibility/display metadata and must not create a workspace. Loading v1 sessions defaults durable IDs to empty and ignores any stale generation-shaped keys.

- [ ] **Step 5: Implement manager dark binding**

```python
class MemoryWorkspaceManager:
    def __init__(self, config: LucNhanConfig):
        self._config = config
        self._locator = MemoryLocator(config.memory_dir)
        self._registry = MemoryRegistry(self._locator.registry_database())
        self._resolver = MemoryIdentityResolver(self._registry)
        self._binding: WorkspaceBinding | None = None
        self._database_generation = 0
        self._case_binding_generation = 0
        if config.memory_workspaces_enabled:
            self._registry.initialize()

    def bind(self, request: IdentityRequest, choice: IdentityChoice | None = None) -> IdentityResolution:
        if not self._config.memory_workspaces_enabled:
            self._binding = WorkspaceBinding("", "disabled", request.display_name)
            return IdentityResolution(ResolutionStatus.EPHEMERAL, self._binding)
        resolution = self._resolver.resolve(request, choice)
        if resolution.binding != self._binding:
            self._database_generation += 1
        self._binding = resolution.binding
        return resolution

    def run_context(self, active_case_id: str = "") -> MemoryRunContext:
        memory_id = self._binding.memory_id if self._binding is not None else ""
        return MemoryRunContext(memory_id, active_case_id, self._database_generation, self._case_binding_generation)

    def validate_run_context(self, context: MemoryRunContext) -> bool:
        current = self.run_context(context.active_case_id)
        return current == context

    def require_persistent_paths(self) -> WorkspacePaths:
        if self._binding is None or self._binding.state not in {"active", "provisional"}:
            raise PersistenceDisabled("central memory persistence is unavailable")
        return self._locator.binary(validate_memory_id(self._binding.memory_id))
```

Define `PersistenceDisabled` in `manager.py`. No caller may pass the empty disabled/ephemeral ID to `MemoryLocator`; all persistence service construction goes through `require_persistent_paths()`.

- [ ] **Step 6: Resolve identity before automatic UUID creation in controller**

Reorder controller startup:

1. normalize path;
2. read existing UUID without generating;
3. build identity request using injected source identity and filesystem identity;
4. manager resolves;
5. if resolution requests a fresh UUID, attempt `set_database_instance_id()`;
6. mark provisional/disabled on UUID-write failure;
7. catch registry/root `OSError`, permission, unsupported-WAL, and newer-schema errors at the controller boundary, create a visible disabled/degraded binding with a sanitized warning, and never fall back beside the IDB;
8. create sessions with resolved binding/generations.

On `reset_for_new_file`, cancel/invalidate current runner, increment generation through manager bind, clear memory UI/cache hooks, then restore sessions. Same-path replacement must not return early based on path alone; compare resolved identity/generation.

- [ ] **Step 7: Keep the scaffolding dark and surface storage failure**

With default config, assert controller constructs a disabled binding, performs no memory-directory creation, and retains current `RIKUGAN.md`/JSONL behavior. No prompt or pseudo-tool file changes belong in this task. With the feature enabled and an unwritable/unsupported memory root, assert the controller remains usable in visible memory-disabled mode, emits one sanitized status, creates no sidecar fallback, and rejects persistence.

- [ ] **Step 8: Run session/controller tests**

Run: `uv run python -m pytest tests/state/test_memory_binding.py tests/memory/test_manager.py tests/agent/test_session_controller.py -v`

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add lucnhan/memory/manager.py lucnhan/state/session.py lucnhan/state/history.py lucnhan/constants.py lucnhan/ui/session_controller_base.py lucnhan/ida/headless_controller.py tests/memory/test_manager.py tests/state/test_memory_binding.py tests/agent/test_session_controller.py
git commit -m "feat(memory): bind sessions to memory workspaces"
```

---

### Task 9: Foundation integration gate and dark-scaffolding verification

**Files:**
- Modify: `lucnhan/memory/__init__.py`
- Modify: `tests/memory/test_manager.py`
- Modify: `tests/memory/test_registry.py`
- Modify: `tests/memory/test_markdown.py`

**Interfaces:**
- Consumes: all previous tasks.
- Produces: stable public foundation exports used by later plans.
- Produces no user-visible memory cutover.

- [ ] **Step 1: Export only stable foundation types**

```python
from .manager import MemoryWorkspaceManager
from .markdown import MemoryProjector, parse_memory_document
from .registry import MemoryRegistry
from .workspace import MemoryLocator, MemoryRunContext, WorkspaceBinding, WorkspacePaths
from .workspace_store import WorkspaceStore

__all__ = [
    "MemoryLocator",
    "MemoryProjector",
    "MemoryRegistry",
    "MemoryRunContext",
    "MemoryWorkspaceManager",
    "WorkspaceBinding",
    "WorkspacePaths",
    "WorkspaceStore",
    "parse_memory_document",
]
```

Do not remove legacy `KnowledgeRawStore` exports until the atomic cutover plan.

- [ ] **Step 2: Add end-to-end foundation test**

Create two IDB identity requests with the same parent directory but distinct filesystem identity. Resolve both, create both workspace stores, write `func:0x401000` facts, project Markdown, and assert four physical files differ and contain only their owner content.

- [ ] **Step 3: Add dark-mode no-side-effect test**

Construct `MemoryWorkspaceManager` with the default config, call `bind`, and assert `<config_dir>/memory` does not exist. This guards against accidentally activating central storage before cutover.

- [ ] **Step 4: Run the full foundation suite**

Run: `uv run python -m pytest tests/memory tests/state/test_memory_binding.py tests/cli/test_headless_memory_identity.py tests/ida/test_headless_bootstrap.py tests/agent/test_session_controller.py -v`

Expected: PASS.

- [ ] **Step 5: Run both repository test roots to catch import regressions**

Run: `uv run python -m pytest tests/ lucnhan/tests/ -q`

Expected: PASS. If pre-existing order-dependent failures remain on the base branch, record exact test IDs in the execution report; do not weaken new tests or mark the task complete until new failures are fixed.

- [ ] **Step 6: Run static checks**

Run: `uvx ruff format --check lucnhan/ tests/`

Run: `uvx ruff check lucnhan/ tests/`

Run: `uvx mypy lucnhan/core lucnhan/providers --pretty`

Expected: PASS.

- [ ] **Step 7: Confirm no cutover occurred**

Run: `git grep -n "RIKUGAN.md" -- lucnhan/agent lucnhan/memory lucnhan/ui`

Expected: Existing runtime references still exist because this plan is dark scaffolding. Confirm no new consumer reads `MEMORY.md` except `lucnhan/memory/markdown.py`.

- [ ] **Step 8: Commit**

```bash
git add lucnhan/memory/__init__.py tests/memory/test_manager.py tests/memory/test_registry.py tests/memory/test_markdown.py
git commit -m "test(memory): verify dark workspace foundation"
```

---

## Foundation Exit Checklist

- [ ] `LucNhanConfig.memory_dir` and dependency manifests are correct.
- [ ] Registry v1 serializes first-open and rejects unsupported schema.
- [ ] Resolver follows the complete ordered copy/move/conflict decision table.
- [ ] Raw headless input carries validated SHA-256 before IDA launch.
- [ ] Per-workspace SQLite enforces owner identity and optimistic revisions.
- [ ] `MEMORY.md` projection is deterministic, locked, atomic, and conflict-aware.
- [ ] Session/manifest schema binds resolved workspace and generations.
- [ ] Default runtime remains dark with no dual read/write.
- [ ] Root `tests/memory/` and both repository test roots pass.
