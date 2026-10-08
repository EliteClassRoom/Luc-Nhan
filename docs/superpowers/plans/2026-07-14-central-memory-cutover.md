# Central Memory Atomic Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Atomically chuyển mọi binary-memory reader/writer từ folder-scoped `RIKUGAN.md`, `.lucnhan-kb/`, và `notes/` sang central per-binary workspace, bật feature sau khi explicit legacy import khả dụng, và không duy trì dual-write/fallback.

**Architecture:** Một `BinaryMemoryService` duy nhất sở hữu prompt read, structured retrieval, fact/note/report writes và projection. `AgentLoop` nhận frozen `MemoryRunContext` cùng non-serializable main-agent `MemoryWriteAuthority`; commands và UI chỉ dùng service/repository, không derive IDB-directory paths. Activation chỉ xảy ra sau khi mọi consumer và minimal legacy importer đã cut over trong cùng release.

**Tech Stack:** Foundation plan modules, Python 3.11–3.12, SQLite WAL, `portalocker`, deterministic `MEMORY.md`, pytest, PySide6 stubs, existing sanitize/retrieval logic adapted to repositories.

## Global Constraints

- Prerequisite: hoàn tất `docs/superpowers/plans/2026-07-14-central-memory-foundation.md`.
- Spec authority: `docs/superpowers/specs/2026-07-14-central-memory-workspaces-design.md`.
- Không runtime read/write `RIKUGAN.md`, `.lucnhan-kb/*.jsonl`, hoặc folder-level `notes/` sau activation.
- Không dual-write và không transparent fallback.
- `MEMORY.md` managed facts đến từ SQLite; prompt chỉ đọc phần unmanaged Markdown như `manual_notes`.
- `save_memory`, approved plans, exploration reports, research notes, reports và auto-ingestion đều cần `MemoryWriteAuthority` từ main controller/UI.
- Subagent/Bulk Renamer persistence trở thành candidate event, không commit.
- Mọi path đến central workspace đến từ frozen `MemoryRunContext`/`MemoryLocator`.
- Legacy data không được inject trước import; importer không xóa/move source và idempotent theo full source fingerprint + target + normalized selected items/group assignments.
- Database switch invalidates old generations, clears memory UI/cache và rejects late writes.
- Binary-memory activation chỉ khi central storage + explicit import + multiprocess/projection integration tests pass.
- Tests mới nằm dưới root `tests/`.
- Commit theo Conventional Commits; task chỉ complete khi targeted tests pass.

**Spec reference:** `docs/superpowers/specs/2026-07-14-central-memory-workspaces-design.md`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `lucnhan/memory/authority.py` | Create | Non-serializable write authority và candidate events |
| `lucnhan/memory/service.py` | Create | Binary-memory façade cho prompt/retrieval/write/note/report |
| `lucnhan/memory/repository.py` | Create | Adapter query/write giữa current knowledge dataclasses và SQLite workspace |
| `lucnhan/memory/legacy.py` | Create | Detect, inventory, fingerprint, preview và minimal explicit import |
| `lucnhan/memory/context.py` | Modify | Repository protocol thay `KnowledgeRawStore`; managed/unmanaged split |
| `lucnhan/memory/retrieve.py` | Modify | Repository protocol và SQLite-safe read behavior |
| `lucnhan/memory/ingest.py` | Modify | Authority-bound service writes; bỏ runtime `make_store(idb_path)` |
| `lucnhan/memory/notes.py` | Modify | Workspace paths, locked atomic writes và index metadata |
| `lucnhan/memory/report.py` | Modify | Workspace service/report index, no path derivation |
| `lucnhan/memory/raw_store.py` | Modify | Legacy/interchange-only marker; không runtime export |
| `lucnhan/memory/paths.py` | Modify | Giữ entity-ID helpers; retire folder layout functions |
| `lucnhan/memory/__init__.py` | Modify | Export central runtime API, không export raw store runtime |
| `lucnhan/agent/system_prompt.py` | Modify | Load unmanaged `MEMORY.md` and structured binary context |
| `lucnhan/agent/loop.py` | Modify | Frozen service/context/authority; replace all memory pseudo-tool handlers |
| `lucnhan/agent/loop_commands.py` | Modify | `/memory`, `/knowledge`, `/report`, `/memory sync/import-legacy` |
| `lucnhan/agent/modes/plan.py` | Modify | Approved plan persistence through service |
| `lucnhan/agent/modes/research.py` | Modify | Workspace notes and candidate-only subagent behavior |
| `lucnhan/agent/pseudo_tool_schemas.py` | Modify | `MEMORY.md` terminology and capability exposure |
| `lucnhan/agent/subagent.py` | Modify | Read snapshot only; no write authority/schema |
| `lucnhan/agent/bulk_renamer.py` | Modify | Candidate events only |
| `lucnhan/ui/session_controller_base.py` | Modify | Construct service/authority and activate feature |
| `lucnhan/ui/panel_core.py` | Modify | Workspace-aware Knowledge panel, legacy banner, cache clearing |
| `lucnhan/ui/knowledge_panel.py` | Modify | Status, import/sync/conflict UI and central rows |
| `lucnhan/core/config.py` | Modify | Enable cutover, explicit migration/sync settings |
| `README.md`, `ARCHITECTURE.md`, `AGENTS.md`, `CLAUDE.md`, `llms.txt` | Modify | New canonical contract |
| `tests/memory/test_authority.py` | Create | Main-agent-only write enforcement |
| `tests/memory/test_repository.py` | Create | SQLite adapter retrieval/write parity |
| `tests/memory/test_service.py` | Create | Prompt/write/note/report service integration |
| `tests/memory/test_legacy.py` | Create | Detection and minimal import |
| `tests/agent/test_memory_cutover.py` | Create | Agent pseudo-tool/command/plan/subagent cutover |
| `tests/ui/test_memory_workspace_ui.py` | Create | DB switch, panel status, legacy banner |
| Existing root/nested knowledge tests | Modify/Move | Run against SQLite repository and root collection |

---

### Task 1: Main-agent write authority and candidate protocol

**Files:**
- Create: `lucnhan/memory/authority.py`
- Create: `tests/memory/test_authority.py`

**Interfaces:**
- Consumes: `MemoryRunContext` from foundation.
- Produces: `MemoryAuthorityIssuer` (controller-owned), `MemoryWriteAuthority`, `MemoryWriteDenied`, `CandidateSourceRef`, `MemoryReadSnapshot`, `MemoryCandidate`, `MemoryCandidateSink`.
- Consumed by: Tasks 2, 5–8.

- [ ] **Step 1: Write failing authority tests**

```python
from __future__ import annotations

import pickle

import pytest

from lucnhan.memory.authority import MemoryAuthorityIssuer, MemoryWriteDenied
from lucnhan.memory.workspace import MemoryRunContext, new_memory_id


def _context() -> MemoryRunContext:
    return MemoryRunContext(new_memory_id(), "", 1, 0)


def test_authority_is_identity_bound_and_not_serializable() -> None:
    issuer = MemoryAuthorityIssuer()
    context = _context()
    authority = issuer.issue(context)

    assert issuer.require(authority, context) is authority
    with pytest.raises((pickle.PicklingError, TypeError)):
        pickle.dumps(authority)


def test_missing_or_wrong_authority_is_rejected() -> None:
    issuer = MemoryAuthorityIssuer()
    context = _context()
    with pytest.raises(MemoryWriteDenied):
        issuer.require(None, context)
    with pytest.raises(MemoryWriteDenied):
        issuer.require(issuer.issue(_context()), context)
```

- [ ] **Step 2: Run tests and verify missing module**

Run: `uv run python -m pytest tests/memory/test_authority.py -v`

Expected: FAIL with missing module.

- [ ] **Step 3: Implement opaque non-serializable authority**

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .workspace import MemoryRunContext


class MemoryWriteDenied(PermissionError):
    pass


class MemoryWriteAuthority:
    __slots__ = ("_context", "_nonce")

    def __init__(self, context: MemoryRunContext, nonce: object):
        self._context = context
        self._nonce = nonce

    def __reduce__(self) -> object:
        raise TypeError("MemoryWriteAuthority cannot be serialized")


class MemoryAuthorityIssuer:
    """Controller-owned issuer; do not pass this object into AgentLoop/subagents."""

    __slots__ = ("_nonce",)

    def __init__(self) -> None:
        self._nonce = object()

    def issue(self, context: MemoryRunContext) -> MemoryWriteAuthority:
        return MemoryWriteAuthority(context, self._nonce)

    def require(
        self,
        authority: MemoryWriteAuthority | None,
        context: MemoryRunContext,
    ) -> MemoryWriteAuthority:
        if authority is None or authority._nonce is not self._nonce or authority._context != context:
            raise MemoryWriteDenied("persistent memory write authority required")
        return authority
```

- [ ] **Step 4: Add candidate event contracts**

```python
@dataclass(frozen=True)
class CandidateSourceRef:
    source_memory_id: str
    source_record_id: str
    source_revision: int
    source_hash: str
    namespace_address: str = ""


@dataclass(frozen=True)
class MemoryCandidate:
    source: str
    kind: str
    title: str
    content: str
    confidence: float
    source_refs: tuple[CandidateSourceRef, ...] = ()


class MemoryCandidateSink(Protocol):
    def submit_candidate(self, candidate: MemoryCandidate) -> None:
        """Queue a bounded candidate for explicit main-agent review."""
        raise NotImplementedError
```

Validate candidate construction before queueing: allowlisted `kind`, finite `confidence` in `[0.0, 1.0]`, bounded title/content/source-ref counts, and sanitized text. The sink has a bounded queue and explicit overflow event; it never persists on overflow.

- [ ] **Step 5: Run tests and commit**

Run: `uv run python -m pytest tests/memory/test_authority.py -v`

Expected: PASS.

```bash
git add lucnhan/memory/authority.py tests/memory/test_authority.py
git commit -m "feat(memory): enforce main-agent write authority"
```

---

### Task 2: SQLite repository adapter for current knowledge contracts

**Files:**
- Create: `lucnhan/memory/repository.py`
- Create: `tests/memory/test_repository.py`
- Modify: `lucnhan/memory/schema.py`

**Interfaces:**
- Consumes: `WorkspaceStore`, legacy `KnowledgeMemory/Entity/Relation/Observation` contracts.
- Produces: `KnowledgeRepository` protocol and `SQLiteKnowledgeRepository`.
- Produces: `list_memories()`, `list_entities()`, `list_relations()`, `count_observations()`, `upsert_*()`, `append_observation()`.
- Maintains: a legacy-ID compatibility map (`legacy_record_ids`) so deterministic IDs such as `func:0x401000` remain round-trippable API IDs while authoritative SQLite uses generated collision-safe IDs. Structured source references resolve through a typed source table; `source_refs` strings are compatibility views only.
- Consumed by: retrieval/context/service.

- [ ] **Step 1: Write failing parity tests**

```python
from __future__ import annotations

from pathlib import Path

from lucnhan.memory.repository import SQLiteKnowledgeRepository
from lucnhan.memory.schema import KnowledgeEntity, KnowledgeMemory, KnowledgeRelation
from lucnhan.memory.workspace import MemoryLocator, new_memory_id
from lucnhan.memory.workspace_store import WorkspaceStore


def test_repository_round_trips_current_knowledge_shapes(tmp_path: Path) -> None:
    memory_id = new_memory_id()
    store = WorkspaceStore.create(MemoryLocator(tmp_path).binary(memory_id), memory_id)
    repo = SQLiteKnowledgeRepository(store)
    memory = KnowledgeMemory(
        id="mem:fact:rc4",
        owner_memory_id=memory_id,
        type="fact",
        title="RC4",
        content="Uses RC4",
        confidence=0.9,
    )
    entity = KnowledgeEntity(
        id="func:0x401000",
        owner_memory_id=memory_id,
        type="function",
        name="decrypt",
    )
    relation = KnowledgeRelation(
        id="rel:decrypt:uses:rc4",
        owner_memory_id=memory_id,
        src=entity.id,
        predicate="uses_algorithm",
        dst="algo:rc4",
    )

    repo.upsert_memory(memory)
    repo.upsert_entity(entity)
    repo.upsert_relation(relation)

    assert repo.list_memories() == [memory]
    assert repo.list_entities() == [entity]
    assert repo.list_relations() == [relation]
```

- [ ] **Step 2: Run and verify missing adapter/field**

Run: `uv run python -m pytest tests/memory/test_repository.py -v`

Expected: FAIL because repository and `owner_memory_id` do not exist.

- [ ] **Step 3: Rename trust-bearing record field**

In `lucnhan/memory/schema.py`, replace `binary_id` with `owner_memory_id` in all structured dataclasses and serialization. Add a legacy loader helper:

```python
@classmethod
def from_legacy_dict(cls, data: dict[str, Any], *, target_memory_id: str) -> "KnowledgeMemory":
    normalized = dict(data)
    normalized.pop("binary_id", None)
    normalized["owner_memory_id"] = target_memory_id
    return cls(**normalized)
```

This helper is importer-internal only: it receives an already validated/bounded record, rejects unknown keys/types/oversized collections before construction, and never trusts legacy `binary_id` for routing. Production deserialization uses explicit per-dataclass field validators rather than raw `cls(**json)`.

- [ ] **Step 4: Define repository protocol**

```python
class KnowledgeRepository(Protocol):
    owner_memory_id: str

    def list_memories(self) -> list[KnowledgeMemory]:
        raise NotImplementedError

    def list_entities(self) -> list[KnowledgeEntity]:
        raise NotImplementedError

    def list_relations(self) -> list[KnowledgeRelation]:
        raise NotImplementedError

    def count_observations(self) -> int:
        raise NotImplementedError

    def upsert_memory(self, value: KnowledgeMemory) -> None:
        raise NotImplementedError

    def upsert_entity(self, value: KnowledgeEntity) -> None:
        raise NotImplementedError

    def upsert_relation(self, value: KnowledgeRelation) -> None:
        raise NotImplementedError

    def append_observation(self, value: KnowledgeObservation) -> None:
        raise NotImplementedError

    def save_memory_fact(
        self,
        category: str,
        fact: str,
        source: str,
        *,
        precommit_check: Callable[[], None],
    ) -> KnowledgeMemory:
        """Allocate/update one generated-ID fact and append its observation atomically."""
        raise NotImplementedError
```

- [ ] **Step 5: Implement SQLite adapter with generated IDs, typed sources, and revision-safe upserts**

Add a workspace migration table:

```sql
CREATE TABLE legacy_record_ids(
    record_kind TEXT NOT NULL,
    legacy_id TEXT NOT NULL,
    generated_id TEXT NOT NULL,
    PRIMARY KEY(record_kind, legacy_id),
    UNIQUE(record_kind, generated_id)
);
```

`upsert_*()` resolves or allocates a generated authoritative ID inside the same transaction and rewrites entity/relation references through this map; deterministic address IDs such as `func:0x401000` never become authoritative primary keys. Typed source rows carry owner ID, source record ID, revision, content hash and optional namespaced address. The legacy `source_refs: list[str]` is a compatibility projection only.

Map current records to `WorkspaceStore` tables. Reject any record whose `owner_memory_id` differs from the store owner. Preserve timestamps/tags in bounded JSON metadata. Upsert acquires `BEGIN IMMEDIATE` through the foundation's bounded lock-acquisition retry, reads the current revision, invokes the non-blocking `precommit_check` immediately before the first mutation statement, and applies `expected_revision`; it never retries/replays after any mutation statement has run, and never hides stale-revision failure.

- [ ] **Step 6: Run repository tests**

Run: `uv run python -m pytest tests/memory/test_repository.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add lucnhan/memory/repository.py lucnhan/memory/schema.py tests/memory/test_repository.py
git commit -m "feat(memory): adapt knowledge records to sqlite"
```

---

### Task 3: Retrieval/context decoupling and `BinaryMemoryService`

**Files:**
- Create: `lucnhan/memory/service.py`
- Modify: `lucnhan/memory/retrieve.py:20-38,168-281`
- Modify: `lucnhan/memory/context.py:19-32,75-189`
- Modify: `lucnhan/memory/markdown.py`
- Create: `tests/memory/test_service.py`
- Modify: `lucnhan/tests/knowledge/test_retrieve_context.py` and move/copy coverage to `tests/memory/test_retrieve_context.py`

**Interfaces:**
- Consumes: repository, projector, authority, run context, config budgets. `BinaryMemoryService` receives the controller-owned `MemoryAuthorityIssuer` by dependency injection; the issuer itself never enters `AgentLoop` or subagents.
- Produces: `SaveMemoryResult(record_id: str, revision: int, projection_dirty: bool, warning: str)`, `StaleMemoryContext`, `BinaryMemoryService.structured_context()`, `manual_notes_context()`, `save_fact()`, `save_plan()`, `save_note()`, `write_report()`, `sync_markdown_preview()`.
- Internal helpers with exact ownership: `sanitize_category`/`sanitize_fact` live in `service.py`; `_validate_context` delegates to `MemoryWorkspaceManager.validate_run_context`; `ProjectionError` is the base class in `markdown.py`.
- Consumed by: Agent and UI tasks.

- [ ] **Step 1: Write failing prompt-source separation test**

```python
from __future__ import annotations

from pathlib import Path

from lucnhan.memory.authority import MemoryAuthorityIssuer
from lucnhan.memory.service import BinaryMemoryService
from lucnhan.memory.workspace import MemoryLocator, MemoryRunContext, new_memory_id
from lucnhan.memory.workspace_store import WorkspaceStore


def test_prompt_uses_sqlite_managed_facts_and_markdown_unmanaged_notes(tmp_path: Path) -> None:
    memory_id = new_memory_id()
    context = MemoryRunContext(memory_id, "", 1, 0)
    paths = MemoryLocator(tmp_path).binary(memory_id)
    store = WorkspaceStore.create(paths, memory_id)
    issuer = MemoryAuthorityIssuer()
    service = BinaryMemoryService(
        context,
        paths,
        store,
        authority_issuer=issuer,
        context_validator=lambda candidate: candidate == context,
    )
    service.save_fact(
        issuer.issue(context),
        category="algorithm",
        fact="Uses RC4",
        source="save_memory",
    )
    paths.markdown.write_text(
        paths.markdown.read_text(encoding="utf-8") + "\n## User Notes\nCheck key schedule.\n",
        encoding="utf-8",
    )

    structured = service.structured_context(query="RC4", mode="normal")
    manual = service.manual_notes_context()

    assert "Uses RC4" in structured
    assert "Check key schedule" not in structured
    assert "Check key schedule" in manual
    assert "lucnhan:record" not in manual
```

- [ ] **Step 2: Run and verify missing service**

Run: `uv run python -m pytest tests/memory/test_service.py -v`

Expected: FAIL with missing service.

- [ ] **Step 3: Refactor retriever/context to protocols**

Replace concrete `KnowledgeRawStore` parameters with `KnowledgeRepository`. Replace `KnowledgePaths` note reads with an injected `NoteReader` callable/protocol. Keep existing exact-address, keyword and one-hop relation ranking behavior. Failure remains non-throwing on the read path, but log storage errors and never create a DB.

- [ ] **Step 4: Expose unmanaged Markdown only**

Add `extract_unmanaged_markdown(content: str) -> str` to `markdown.py`. It returns `prefix + suffix`, never the managed block or hidden record markers. `manual_notes_context()` sanitizes and wraps it as `<manual_notes memory_id="...">` while preserving closing tags under truncation.

- [ ] **Step 5: Implement service save transaction + projection**

```python
def save_fact(
    self,
    authority: MemoryWriteAuthority,
    *,
    category: str,
    fact: str,
    source: str,
) -> SaveMemoryResult:
    self._authority_issuer.require(authority, self.context)
    if not self._validate_context(self.context):
        raise StaleMemoryContext("database binding changed")
    normalized_category = sanitize_category(category)
    normalized_fact = sanitize_fact(fact)
    record = self._repository.save_memory_fact(
        normalized_category,
        normalized_fact,
        source,
        precommit_check=lambda: self._require_current_context(),
    )
    try:
        self._projector.project(self.paths, self._store)
    except ProjectionError as exc:
        return SaveMemoryResult(
            record_id=record.id,
            revision=record.revision,
            projection_dirty=True,
            warning=str(exc),
        )
    return SaveMemoryResult(
        record_id=record.id,
        revision=record.revision,
        projection_dirty=False,
        warning="",
    )
```

Implement deterministic `save_plan()` as a structured `plan` fact/revision plus observation, not raw Markdown append.

- [ ] **Step 6: Run retrieval/service tests**

Run: `uv run python -m pytest tests/memory/test_service.py tests/memory/test_retrieve_context.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add lucnhan/memory/service.py lucnhan/memory/retrieve.py lucnhan/memory/context.py lucnhan/memory/markdown.py tests/memory/test_service.py tests/memory/test_retrieve_context.py lucnhan/tests/knowledge/test_retrieve_context.py
git commit -m "feat(memory): centralize binary memory service"
```

---

### Task 4: Workspace notes and reports

**Files:**
- Modify: `lucnhan/memory/notes.py:1-268`
- Modify: `lucnhan/memory/report.py:299-550`
- Modify: `lucnhan/memory/workspace_store.py`
- Modify: `lucnhan/memory/service.py`
- Create: `tests/memory/test_notes_reports.py`
- Modify/Move: `lucnhan/tests/knowledge/test_notes.py`, `lucnhan/tests/knowledge/test_report.py`

**Interfaces:**
- Consumes: service authority/context, `WorkspacePaths.notes/reports`, workspace lock.
- Produces: `NoteRecord(relative_path: str, content_hash: str, revision: int)`, `DocumentConflict`, `write_workspace_note()`, `write_workspace_report()`, `reconcile_note_index()`.
- Internal helpers `allocate_safe_note_path`, `workspace_lock`, `atomic_replace_text`, and root-relative path calculation live in `notes.py`; they reuse storage/lock primitives from foundation rather than undefined global helpers.
- Consumed by: research pseudo-tool and `/report`.

- [ ] **Step 1: Write failing path/atomicity tests**

Test that a note named `Config: Stage 1` writes under `paths.notes`, never IDB directory; traversal title is slugged/rejected; two concurrent writes allocate unique names; a manual file edit with mismatched expected hash raises `DocumentConflict`; case/binary reports use `notes/reports`.

- [ ] **Step 2: Run tests and verify current APIs cannot satisfy workspace paths**

Run: `uv run python -m pytest tests/memory/test_notes_reports.py -v`

Expected: FAIL.

- [ ] **Step 3: Implement locked atomic document writer**

```python
def write_workspace_note(
    paths: WorkspacePaths,
    store: WorkspaceStore,
    *,
    genre: str,
    title: str,
    content: str,
    expected_hash: str = "",
) -> NoteRecord:
    target = allocate_safe_note_path(paths.notes, genre, title)
    with workspace_lock(paths.lock):
        verify_regular_contained_target(paths.notes, target)
        if expected_hash and target.exists() and sha256_file(target) != expected_hash:
            raise DocumentConflict(str(target))
        atomic_replace_text(target, content)
        return store.upsert_note_index(relative_to(paths.root, target), sha256_text(content))
```

Use the same primitive for reports and research index. SQLite indexes ownership/hash/revision only; Markdown body remains authoritative.

- [ ] **Step 4: Implement crash reconciliation**

`reconcile_note_index(paths, store)` scans only regular contained Markdown files with bounded count/size, adds missing index rows, marks missing files inactive, and never follows symlinks.

- [ ] **Step 5: Adapt report context and output to repository/service**

`build_report_context()` consumes `KnowledgeRepository` and note reader. `write_report_file()` becomes an internal document primitive; runtime callers use `service.write_report(authority, scope)`.

- [ ] **Step 6: Run note/report tests and commit**

Run: `uv run python -m pytest tests/memory/test_notes_reports.py tests/memory/test_service.py -v`

Expected: PASS.

```bash
git add lucnhan/memory/notes.py lucnhan/memory/report.py lucnhan/memory/workspace_store.py lucnhan/memory/service.py tests/memory/test_notes_reports.py lucnhan/tests/knowledge/test_notes.py lucnhan/tests/knowledge/test_report.py
git commit -m "feat(memory): isolate workspace notes and reports"
```

---

### Task 5: Minimal explicit legacy inventory and import

**Files:**
- Create: `lucnhan/memory/legacy.py`
- Create: `tests/memory/test_legacy.py`
- Modify: `lucnhan/memory/service.py`
- Modify: `lucnhan/memory/workspace_store.py`
- Modify: `lucnhan/memory/registry.py`

**Interfaces:**
- Produces: `LegacySource`, `LegacyInventory`, `LegacyImportItem`, `LegacyImportSelection`, `LegacyImportReceipt`, `LegacyImportGraph`, `LegacyImportResult`, `detect_legacy_sources(idb_path)`, `inventory_legacy_sources()`, `import_legacy_selection()`.
- Defines in `legacy.py`: `build_deterministic_id_mapping()`, `rewrite_selected_graph()`, and `validate_graph()`; these are private pure staging helpers whose full graph/remap semantics are upgraded in the hardening plan.
- Consumed by: command/UI tasks and activation gate.

- [ ] **Step 1: Write failing no-auto-read detection test**

```python
from __future__ import annotations

from pathlib import Path

from lucnhan.memory.legacy import detect_legacy_sources


def test_detects_legacy_sources_without_reading_them_into_context(tmp_path: Path) -> None:
    idb = tmp_path / "a.i64"
    idb.write_bytes(b"idb")
    (tmp_path / "RIKUGAN.md").write_text("legacy secret", encoding="utf-8")
    (tmp_path / ".lucnhan-kb").mkdir()
    (tmp_path / "notes").mkdir()

    sources = detect_legacy_sources(str(idb))

    assert {source.kind for source in sources} == {"lucnhan_markdown", "jsonl_store", "notes_tree"}
    assert all(source.content is None for source in sources)
```

- [ ] **Step 2: Write failing idempotent selected import test**

Create a legacy Markdown with one selected fact and one unselected paragraph. Import selection into a binary service, rerun same selection, and assert one fact, one receipt, source untouched, no unselected content in `MEMORY.md` or SQLite.

- [ ] **Step 3: Implement metadata-only detector**

Detector uses canonical parent only to inventory legacy source locations; it records kind, path metadata, size, mtime and streaming SHA-256. It does not return content. Registry stores detection/dismissal by source fingerprint.

- [ ] **Step 4: Implement explicit inventory parser**

Only `inventory_legacy_sources()` reads bounded source content after UI/command request. It groups JSONL by legacy `binary_id`, labels address-bearing records, parses `RIKUGAN.md` as free-form sections/candidate bullets without assigning ownership, and inventories notes without following links.

- [ ] **Step 5: Implement selected import transaction**

```python
import hashlib
import json


@dataclass(frozen=True)
class LegacyImportSelection:
    source_fingerprint: str
    target_memory_id: str
    selected_item_ids: tuple[str, ...]
    legacy_group_assignments: tuple[tuple[str, str], ...]


def import_legacy_selection(
    service: BinaryMemoryService,
    authority: MemoryWriteAuthority,
    inventory: LegacyInventory,
    selection: LegacyImportSelection,
) -> LegacyImportResult:
    service.require_write_authority(authority)
    mapping = build_deterministic_id_mapping(inventory, selection)
    graph = rewrite_selected_graph(inventory, selection, mapping)
    validate_graph(graph, target_memory_id=service.context.binary_memory_id)
    normalized_selection = {
        "source_fingerprint": inventory.source_fingerprint,
        "target_memory_id": service.context.binary_memory_id,
        "selected_item_ids": sorted(selection.selected_item_ids),
        "legacy_group_assignments": sorted(selection.legacy_group_assignments),
    }
    import_id = "legacy-" + hashlib.sha256(
        json.dumps(normalized_selection, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    receipt = LegacyImportReceipt(
        import_id=import_id,
        source_fingerprint=inventory.source_fingerprint,
        target_memory_id=service.context.binary_memory_id,
        selected_item_ids=selection.selected_item_ids,
        mapping=mapping,
    )
    return service.import_graph_atomically(graph, receipt=receipt)
```

Minimal Phase-2 import supports binary target only. Case target/full archive flow arrives in interchange plan. Free-form Markdown imports as unmanaged note content or explicitly selected fact; never both silently.

- [ ] **Step 6: Run migration tests**

Run: `uv run python -m pytest tests/memory/test_legacy.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add lucnhan/memory/legacy.py lucnhan/memory/service.py lucnhan/memory/workspace_store.py lucnhan/memory/registry.py tests/memory/test_legacy.py
git commit -m "feat(memory): add explicit legacy memory import"
```

---

### Task 6: Agent prompt, `save_memory`, approved plans, and commands cutover

**Files:**
- Modify: `lucnhan/agent/system_prompt.py:20-96,104-183`
- Modify: `lucnhan/agent/loop.py:84-88,248-289,441-566,1454-1647,2081-2092`
- Modify: `lucnhan/agent/loop_commands.py:48-124,211-369`
- Modify: `lucnhan/agent/modes/plan.py:109-128`
- Modify: `lucnhan/agent/pseudo_tool_schemas.py:24-220`
- Modify: `lucnhan/ui/session_controller_base.py`
- Create: `tests/agent/test_memory_cutover.py`
- Modify: `tests/agent/test_system_prompt.py`

**Interfaces:**
- Consumes: controller-injected service, run context, main authority.
- Produces: canonical `/memory`, `/knowledge`, `/report`, `/memory sync`, `/memory import-legacy` behavior.

- [ ] **Step 1: Write failing service-construction and prompt cutover test**

Have `SessionControllerBase.start_agent()` obtain `paths = memory_manager.require_persistent_paths()`, open/create the bound workspace, construct one `BinaryMemoryService` with the controller-owned issuer, issue authority for the frozen run context, and inject only `(service, authority, context)` into `AgentLoop`. Disabled/ephemeral binding injects no service/authority and exposes no persistence schema.

Build a loop/session with central service, a legacy `RIKUGAN.md` beside IDB containing `DO_NOT_LOAD`, SQLite fact `Uses RC4`, and unmanaged `MEMORY.md` note `Check key`. Assert system prompt contains the SQLite fact and manual note but not legacy text or managed marker.

- [ ] **Step 2: Write failing `save_memory` authority/projection test**

Call real pseudo-tool dispatch with main authority and assert SQLite fact + `MEMORY.md` managed entry + success result. Construct subagent/no-authority loop and assert persistent schemas (`save_memory`, note/report persistence, promotion) are absent; candidate submission is a separate internal event path, never a provider-visible persistence substitute. Add a schema-generation regression test proving a stale/hidden persistence tool name is rejected again at dispatch, not only filtered from the schema.

- [ ] **Step 3: Write failing approved-plan and command tests**

Approve a plan and assert a structured plan fact, not Markdown append. `/memory` renders central workspace status/managed facts/manual notes. `/knowledge` and `/report` query central repository. `/memory import-legacy` requires explicit selected inventory/confirmation.

- [ ] **Step 4: Replace system-prompt file API**

Change the complete existing signature while retaining unrelated parameters:

```python
def build_system_prompt(
    host_name: str = "IDA Pro",
    binary_info: str | None = None,
    current_function: str | None = None,
    current_address: str | None = None,
    extra_context: str | None = None,
    active_goal: str | None = None,
    tool_names: list[str] | None = None,
    skill_summary: str | None = None,
    profile: AnalysisProfile | None = None,
    tools_table: str | None = None,
    structured_memory: str = "",
    manual_memory_notes: str = "",
) -> str:
```

Remove only `idb_dir` plus `_load_persistent_memory(idb_dir)` and its path cache; preserve every other call-site argument. Cache unmanaged `MEMORY.md` by workspace path+mtime/hash inside the service, not by IDB directory. Wrap structured and manual sections separately using existing sanitizers.

- [ ] **Step 5: Remove raw append helper and delegate pseudo-tools**

Add canonical pseudo-tool name constants in `lucnhan/constants.py` for binary persistence and later case promotion; schema construction and dispatch compare only those constants. Delete `_MEMORY_HEADER` and `append_to_memory_file()`. `_handle_save_memory_tool()` calls:

```python
result = self.memory_service.save_fact(
    self._memory_authority,
    category=category,
    fact=fact,
    source="save_memory",
)
```

Projection warnings produce success-with-warning. Exploration/research handlers call service APIs or submit candidates according to authority. Every write validates frozen run context immediately before commit.

- [ ] **Step 6: Cut over direct commands and approved plans**

Replace `_open_knowledge_store()` with `_require_binary_memory_service(loop)`. `/memory` never opens IDB-folder files. Plan persistence calls `service.save_plan(authority, goal, steps)`. Update all user-facing text from `RIKUGAN.md` to `MEMORY.md`.

- [ ] **Step 7: Run agent cutover tests**

Run: `uv run python -m pytest tests/agent/test_memory_cutover.py tests/agent/test_system_prompt.py tests/agent/test_agent_loop.py -v`

Expected: PASS.

- [ ] **Step 8: Assert no agent runtime legacy reads/writes**

Run: `git grep -n "RIKUGAN.md\|append_to_memory_file\|make_store(self.session.idb_path)" -- lucnhan/agent`

Expected: no runtime matches; migration detection strings may exist only under `lucnhan/memory/legacy.py`.

- [ ] **Step 9: Commit**

```bash
git add lucnhan/constants.py lucnhan/agent/system_prompt.py lucnhan/agent/loop.py lucnhan/agent/loop_commands.py lucnhan/agent/modes/plan.py lucnhan/agent/pseudo_tool_schemas.py lucnhan/ui/session_controller_base.py tests/agent/test_memory_cutover.py tests/agent/test_system_prompt.py
git commit -m "feat(memory): cut agent memory over atomically"
```

---

### Task 7: Research mode, subagents, and Bulk Renamer write ownership

**Files:**
- Modify: `lucnhan/agent/modes/research.py:43-104,130-240,351-526`
- Modify: `lucnhan/agent/subagent.py:72-103,137-168,194-228`
- Modify: `lucnhan/agent/bulk_renamer.py`
- Modify: `lucnhan/agent/loop.py`
- Create: `tests/agent/test_memory_write_ownership.py`

**Interfaces:**
- Consumes: `MemoryCandidateSink`, read-only service snapshot.
- Produces: subagent candidate events; main agent may explicitly accept/commit.

- [ ] **Step 1: Write failing exploration/research subagent tests**

Run a child with a parent binary context and assert it can read a frozen sanitized snapshot but does not receive `save_memory`, `research_note`, or `exploration_report` persistence schemas. Submit a finding and assert one `MemoryCandidate`, zero DB facts/notes.

- [ ] **Step 2: Write failing Bulk Renamer no-write test**

Inject a candidate sink into Bulk Renamer result processing and assert rename suggestions can create candidate metadata but cannot invoke repository/service writes without authority.

- [ ] **Step 3: Pass read snapshot, never write authority**

Add the exact frozen contract in `authority.py`:

```python
@dataclass(frozen=True)
class MemoryReadSnapshot:
    binary_memory_id: str
    structured_context: str
    manual_notes_context: str
    source_revision: int
    content_hash: str
```

Construct it once per parent run after sanitization and character budgeting; subagents cannot refresh repositories or use stale snapshots after parent cancellation. Subagent sessions do not receive writable `idb_path` as a memory capability. Remove `_always_allow_scripts`-style authority propagation for memory.

- [ ] **Step 4: Convert research note writes to candidates in child mode**

Main-agent research mode calls:

```python
service.save_note(
    authority,
    genre=genre,
    title=title,
    content=content,
    expected_hash=expected_hash,
)
```

Child research mode emits:

```python
MemoryCandidate(
    source="research_subagent",
    kind="research_note",
    title=title,
    content=content[:MAX_RESEARCH_NOTE_CHARS],
    confidence=0.7,
)
```

Parent UI/agent may present and explicitly persist.

- [ ] **Step 5: Run ownership tests**

Run: `uv run python -m pytest tests/agent/test_memory_write_ownership.py tests/agent/test_subagent_manager.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add lucnhan/agent/modes/research.py lucnhan/agent/subagent.py lucnhan/agent/bulk_renamer.py lucnhan/agent/loop.py tests/agent/test_memory_write_ownership.py
git commit -m "fix(memory): restrict persistence to main agent"
```

---

### Task 8: Knowledge UI, database switching, sync and legacy import UX

**Files:**
- Modify: `lucnhan/ui/knowledge_panel.py:58-301`
- Modify: `lucnhan/ui/panel_core.py:1269-1317,1490-1500,1933-1967,2464-2528`
- Modify: `lucnhan/ui/session_controller_base.py`
- Create: `tests/ui/test_memory_workspace_ui.py`
- Modify: `tests/tools/test_panel_core.py`

**Interfaces:**
- Consumes: controller `memory_service`, manager resolution status, legacy inventory, projection state.
- Produces: status/import/sync signals and workspace-aware panel refresh.

- [ ] **Step 1: Write failing same-folder isolation UI test**

Create controller/panel stubs for two identity bindings in one folder. Populate A then switch to B. Assert rows/status/notes from A are cleared before B refresh and B store is opened by `memory_id`, not path.

- [ ] **Step 2: Write failing legacy banner/sync status tests**

Assert detected legacy sources show `Inspect`, `Import`, `Dismiss`; no source text is rendered. Assert `projection_dirty`, `projection_conflict`, and `unsynced edits` display distinct actions. Import signal carries selected fingerprint/item IDs, not arbitrary paths.

- [ ] **Step 3: Extend KnowledgePanel signals/state**

```python
legacy_import_requested = Signal(str)
legacy_dismiss_requested = Signal(str)
markdown_sync_requested = Signal()
projection_regenerate_requested = Signal()
```

Add exact widget APIs:

```python
def set_workspace_status(self, memory_id: str, state: str) -> None:
    self._workspace_label.setText(f"{memory_id[:12]} · {state}")


def set_projection_state(self, *, dirty: bool, conflict: bool, unsynced: bool) -> None:
    self._sync_btn.setVisible(unsynced)
    self._regenerate_btn.setVisible(dirty or conflict)


def set_legacy_sources(self, source_fingerprints: list[str]) -> None:
    self._legacy_fingerprints = list(source_fingerprints)
    self._legacy_banner.setVisible(bool(source_fingerprints))


def clear_workspace(self) -> None:
    self._workspace_label.clear()
    self._tree.clear()
    self._legacy_fingerprints.clear()
    self._legacy_banner.hide()
```

- [ ] **Step 4: Cut panel refresh to central service**

Replace `make_store(idb_path)` with controller service repository. Read each record type once. Notes come from workspace note index/reader. On DB change, cancel old run, bind new identity, clear Knowledge panel and memory caches, resolve ambiguity before enabling persistence.

- [ ] **Step 5: Wire explicit import/sync**

Import opens inventory preview and commits only confirmed selection with UI authority. Sync previews unmanaged/managed diff and never converts deletion into structured delete automatically. Dismiss records source fingerprint in registry.

- [ ] **Step 6: Run UI tests and commit**

Run: `uv run python -m pytest tests/ui/test_memory_workspace_ui.py tests/tools/test_panel_core.py -v`

Expected: PASS.

```bash
git add lucnhan/ui/knowledge_panel.py lucnhan/ui/panel_core.py lucnhan/ui/session_controller_base.py tests/ui/test_memory_workspace_ui.py tests/tools/test_panel_core.py
git commit -m "feat(memory): bind knowledge ui to workspaces"
```

---

### Task 9: Retire runtime JSONL/folder APIs and move coverage into CI root

**Files:**
- Modify: `lucnhan/memory/raw_store.py`
- Modify: `lucnhan/memory/paths.py`
- Modify: `lucnhan/memory/ingest.py`
- Modify: `lucnhan/memory/__init__.py`
- Move/Rewrite: `lucnhan/tests/knowledge/*.py` → `tests/memory/legacy_knowledge/*.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/release.yml`
- Modify: `ci-local.sh`
- Modify: `ci-local.ps1`
- Modify: `DEVELOPMENT.md`

**Interfaces:**
- Removes runtime `KnowledgeRawStore` and `knowledge_paths(idb_path)` exports.
- Keeps entity/address ID helpers used by SQLite ingestion.
- JSONL read/write primitives become private legacy/interchange helpers only.

- [ ] **Step 1: Add an import-discipline test**

```python
def test_runtime_modules_do_not_import_raw_store() -> None:
    forbidden = {
        "lucnhan.agent.loop",
        "lucnhan.agent.loop_commands",
        "lucnhan.agent.system_prompt",
        "lucnhan.ui.panel_core",
        "lucnhan.memory.context",
        "lucnhan.memory.retrieve",
        "lucnhan.memory.report",
    }
    for module_name in forbidden:
        source = inspect.getsource(importlib.import_module(module_name))
        assert "KnowledgeRawStore" not in source
        assert "knowledge_paths(" not in source
```

- [ ] **Step 2: Run and verify it fails before retirement**

Run: `uv run python -m pytest tests/memory/test_runtime_import_discipline.py -v`

Expected: FAIL with current imports.

- [ ] **Step 3: Remove runtime exports and annotate legacy-only modules**

`raw_store.py` is imported only by `legacy.py` and later interchange code. `paths.py` retains `normalize_address`, entity/relation ID builders, and safe-relative-path validators; remove `KnowledgePaths`/folder-layout runtime methods after migration tests use explicit legacy paths.

- [ ] **Step 4: Consolidate knowledge tests under root `tests/`**

Move and rewrite the nested suite to exercise `SQLiteKnowledgeRepository`, `BinaryMemoryService`, workspace notes/reports, retrieval, commands, and panel. Delete obsolete JSONL runtime assertions; keep legacy parser tests under `tests/memory/legacy_knowledge/`.

- [ ] **Step 5: Make test roots explicit everywhere**

Even after migration, change CI/release/local commands to:

```bash
python -m pytest tests/ lucnhan/tests/ -v --tb=short
```

until `lucnhan/tests/` is empty and deleted in a later cleanup commit. Add pytest `testpaths = ["tests", "lucnhan/tests"]` in `pyproject.toml` to prevent a third hidden tree.

- [ ] **Step 6: Run discipline and complete test inventory**

Run: `uv run python -m pytest tests/memory/test_runtime_import_discipline.py tests/memory/legacy_knowledge -v`

Expected: PASS.

Run: `uv run python -m pytest --collect-only -q tests/ lucnhan/tests/`

Expected: collection includes all remaining tests and no duplicate module-name errors.

- [ ] **Step 7: Commit**

```bash
git add lucnhan/memory/raw_store.py lucnhan/memory/paths.py lucnhan/memory/ingest.py lucnhan/memory/__init__.py tests/memory lucnhan/tests/knowledge pyproject.toml .github/workflows/ci.yml .github/workflows/release.yml ci-local.sh ci-local.ps1 DEVELOPMENT.md
git commit -m "refactor(memory): retire runtime jsonl storage"
```

---

### Task 10: Activate central memory and update documentation atomically

**Files:**
- Modify: `lucnhan/core/config.py`
- Modify: `README.md`
- Modify: `ARCHITECTURE.md`
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`
- Modify: `llms.txt`
- Modify: `lucnhan/memory/__init__.py`
- Modify: `tests/memory/test_config.py`
- Create: `tests/memory/test_atomic_cutover.py`

**Interfaces:**
- Changes default: `memory_workspaces_enabled=True`.
- Establishes `MEMORY.md` as canonical runtime contract.

- [ ] **Step 1: Write failing activation invariant test**

```python
from pathlib import Path

from lucnhan.core.config import LucNhanConfig


def test_cutover_defaults_to_central_memory_without_legacy_fallback(tmp_path: Path) -> None:
    config = LucNhanConfig()
    config._config_dir = str(tmp_path / "config")
    assert config.memory_workspaces_enabled is True

    source_files = [
        Path("lucnhan/agent/loop.py"),
        Path("lucnhan/agent/system_prompt.py"),
        Path("lucnhan/agent/loop_commands.py"),
        Path("lucnhan/ui/panel_core.py"),
    ]
    for source_file in source_files:
        source = source_file.read_text(encoding="utf-8")
        assert "RIKUGAN.md" not in source
        assert ".lucnhan-kb" not in source
```

- [ ] **Step 2: Run and verify default is still dark**

Run: `uv run python -m pytest tests/memory/test_atomic_cutover.py -v`

Expected: FAIL because default is False.

- [ ] **Step 3: Enable only after every exit criterion passes**

Set `memory_workspaces_enabled=True`. Existing config files missing the field use True. Add no compatibility switch that re-enables folder reads; a temporary emergency switch may disable persistence entirely, never re-enable legacy storage.

- [ ] **Step 4: Update canonical documentation**

Document:

- central `<config_dir>/memory` layout;
- identity copy/move semantics;
- `MEMORY.md` managed/user regions;
- SQLite authoritative, JSONL interchange-only;
- explicit legacy import;
- main-agent-only writes;
- no folder fallback.

Remove statements that memory lives beside the IDB.

- [ ] **Step 5: Run atomic cutover suite**

Run: `uv run python -m pytest tests/memory tests/agent/test_memory_cutover.py tests/agent/test_memory_write_ownership.py tests/ui/test_memory_workspace_ui.py -v`

Expected: PASS.

- [ ] **Step 6: Run full repository gates**

Run: `uv run python -m pytest tests/ lucnhan/tests/ -q`

Run: `uvx ruff format --check lucnhan/ tests/`

Run: `uvx ruff check lucnhan/ tests/`

Run: `uvx mypy lucnhan/core lucnhan/providers --pretty`

Run: `uv lock --check`

Expected: PASS. Regenerate and commit `uv.lock` if dependency/version metadata changed.

- [ ] **Step 7: Search for forbidden runtime paths**

Run: `git grep -n "RIKUGAN.md\|\.lucnhan-kb\|dirname(.*idb" -- lucnhan ':!lucnhan/memory/legacy.py' ':!lucnhan/memory/raw_store.py'`

Expected: no runtime matches. Documentation may mention `RIKUGAN.md` only as legacy import source.

- [ ] **Step 8: Commit activation**

```bash
git add lucnhan/core/config.py README.md ARCHITECTURE.md AGENTS.md CLAUDE.md llms.txt lucnhan/memory/__init__.py tests/memory/test_config.py tests/memory/test_atomic_cutover.py uv.lock
git commit -m "feat(memory): activate central binary workspaces"
```

---

## Atomic Cutover Exit Checklist

- [ ] Every runtime memory consumer uses `BinaryMemoryService`/workspace repository.
- [ ] No runtime `RIKUGAN.md`, folder `.lucnhan-kb`, or folder `notes/` read/write remains.
- [ ] Managed facts come from SQLite; unmanaged Markdown is labeled/sanitized separately.
- [ ] Approved plans, research notes, reports and pseudo-tools honor write authority.
- [ ] Subagents/Bulk Renamer only emit candidates.
- [ ] Legacy sources are detected but never auto-read; explicit binary import exists.
- [ ] Knowledge UI clears and rebinds on DB identity changes.
- [ ] Runtime JSONL backend is retired; tests run from explicit roots.
- [ ] Central memory is enabled only in the final activation commit.
- [ ] Full tests, static checks, dependency parity and lockfile check pass.
