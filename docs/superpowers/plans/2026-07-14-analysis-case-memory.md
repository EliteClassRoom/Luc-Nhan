# Analysis Case Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Thêm analysis cases với explicit membership, case-level SQLite memory, promotion có provenance, năm cross-binary relation types và controlled peer retrieval có namespace/citation/token budget.

**Architecture:** `CaseRepository` quản lý case workspace và membership metadata trong registry; `CaseMemoryService` điều phối promotion/source drift; `PeerMemoryRetriever` chỉ mở peer DB read-only/query-only và chỉ eligible khi có direct relation confidence ≥0.7 hoặc exact strong-artifact match. Session đóng băng một active case/generation; UI và commands dùng generated IDs, suggestions không tự mutate membership.

**Tech Stack:** Central memory foundation/cutover modules, SQLite WAL, Python dataclasses/enums, existing keyword retrieval/sanitization, PySide6 UI, pytest/multiprocessing.

## Global Constraints

- Prerequisites: hoàn tất foundation và atomic cutover plans.
- Một binary có thể thuộc nhiều cases; một session active tối đa một case.
- Membership chỉ thay đổi qua explicit UI/command; suggestions không auto-add.
- Case delete là soft delete; member removal không xóa binary facts.
- Five case-level predicates only: `embeds_or_loads`, `communicates_with`, `derived_from`, `same_family_as`, `shares_artifact_with`.
- Symmetric relation endpoints canonicalized bằng sorted `memory_id`; self-relation rejected.
- `shares_artifact_with` cần artifact/source reference.
- Promotion chỉ explicit UI, `/case promote`, hoặc main-agent call theo explicit user request.
- Promotion idempotent theo `(case_id, source_memory_id, source_record_id, source_revision, promotion_kind)`.
- Source drift evaluated lazily; drifted/missing sources excluded khỏi automatic peer injection.
- Automatic peer eligibility: direct relation confidence ≥0.7 hoặc exact strong-artifact match; lexical relevance chỉ rank.
- V1 không provider/embedding/IDA scan cho peer retrieval hay suggestions.
- Auto peer context chỉ current structured facts/entities/case relation summaries; không raw pseudocode, Markdown, notes, reports, observations.
- Character budget từ `knowledge_max_context_chars`: active 55%, case 30%, peers 15%; unused returns active.
- Peer DB mở SQLite URI `mode=ro` + `PRAGMA query_only=ON`; retrieval không tạo DB.
- XML attributes escaped, body truncated trước wrapper, citation có shortened memory ID.
- Peer search/compare chỉ target current members of active case.
- Tests mới nằm dưới root `tests/memory/cases/` và `tests/ui/`.

**Spec reference:** `docs/superpowers/specs/2026-07-14-central-memory-workspaces-design.md`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `lucnhan/memory/case_schema.py` | Create | Predicate enum, case/member/relation/promotion dataclasses |
| `lucnhan/memory/case_repository.py` | Create | Case CRUD/membership/relations/promotions in registry/case DB |
| `lucnhan/memory/case_service.py` | Create | Active binding, promotion, source drift, case narrative/retrieval |
| `lucnhan/memory/suggestions.py` | Create | Exact-signal non-mutating membership suggestions |
| `lucnhan/memory/peer_retrieval.py` | Create | Eligibility/ranking/dedup/budget/read-only retrieval/wrappers |
| `lucnhan/memory/workspace_store.py` | Modify | Source/artifact query and read-only current-record APIs |
| `lucnhan/memory/registry.py` | Modify | Cases/case_members schema and generation-safe operations |
| `lucnhan/memory/manager.py` | Modify | Active case binding/generation validation |
| `lucnhan/state/session.py`, `lucnhan/state/history.py` | Modify | Restore valid active case only |
| `lucnhan/agent/loop.py` | Modify | Case context and explicit promotion pseudo-tool dispatch |
| `lucnhan/agent/loop_commands.py` | Modify | Final `/case` and peer-search command contract |
| `lucnhan/agent/pseudo_tool_schemas.py` | Modify | Explicit promotion schema only when active case |
| `lucnhan/agent/system_prompt.py` | Modify | Namespaced case/peer blocks and policy |
| `lucnhan/ui/knowledge_panel.py` | Modify | Case selector/membership/relation/promotion controls |
| `lucnhan/ui/panel_core.py` | Modify | Case signal wiring and context refresh |
| `lucnhan/core/config.py` | Modify | `case_memory_enabled`, `peer_retrieval_enabled` |
| `tests/memory/cases/test_schema.py` | Create | Predicate semantics/canonicalization |
| `tests/memory/cases/test_repository.py` | Create | CRUD/membership/soft delete/relations |
| `tests/memory/cases/test_binding.py` | Create | Active case and generation/session restore |
| `tests/memory/cases/test_promotion.py` | Create | Explicit promotion/idempotence/source drift |
| `tests/memory/cases/test_suggestions.py` | Create | Exact-only non-mutating suggestions |
| `tests/memory/cases/test_peer_retrieval.py` | Create | Eligibility/ranking/budget/citation/read-only behavior |
| `tests/agent/test_case_commands.py` | Create | Command/pseudo-tool/system prompt behavior |
| `tests/ui/test_case_memory_ui.py` | Create | Selector/membership/relation/promotion UI |

---

### Task 1: Case schema and predicate invariants

**Files:**
- Create: `lucnhan/memory/case_schema.py`
- Create: `tests/memory/cases/test_schema.py`

**Interfaces:**
- Produces: `CaseRelationType`, `CaseRecord`, `CaseMember`, `CaseRelation`, `PromotionSource`, `CasePromotion`.
- Produces: `canonicalize_relation_endpoints()` and `validate_case_relation()`.
- Consumed by: all later tasks.

- [ ] **Step 1: Write failing relation invariant tests**

```python
from __future__ import annotations

import pytest

from lucnhan.memory.case_schema import (
    CaseRelationType,
    canonicalize_relation_endpoints,
    validate_case_relation,
)
from lucnhan.memory.workspace import new_memory_id


def test_symmetric_endpoints_are_canonicalized() -> None:
    a, b = sorted((new_memory_id(), new_memory_id()))
    assert canonicalize_relation_endpoints(b, CaseRelationType.COMMUNICATES_WITH, a) == (a, b)
    assert canonicalize_relation_endpoints(b, CaseRelationType.DERIVED_FROM, a) == (b, a)


def test_self_relation_and_missing_shared_artifact_are_rejected() -> None:
    member = new_memory_id()
    with pytest.raises(ValueError, match="self relation"):
        validate_case_relation(member, CaseRelationType.SAME_FAMILY_AS, member, artifact_ref="")
    with pytest.raises(ValueError, match="artifact"):
        validate_case_relation(
            new_memory_id(),
            CaseRelationType.SHARES_ARTIFACT_WITH,
            new_memory_id(),
            artifact_ref="",
        )
```

- [ ] **Step 2: Run and verify missing module**

Run: `uv run python -m pytest tests/memory/cases/test_schema.py -v`

Expected: FAIL with missing module.

- [ ] **Step 3: Implement exact enum and directionality**

```python
class CaseRelationType(str, Enum):
    EMBEDS_OR_LOADS = "embeds_or_loads"
    COMMUNICATES_WITH = "communicates_with"
    DERIVED_FROM = "derived_from"
    SAME_FAMILY_AS = "same_family_as"
    SHARES_ARTIFACT_WITH = "shares_artifact_with"


_SYMMETRIC_RELATIONS = frozenset(
    {
        CaseRelationType.COMMUNICATES_WITH,
        CaseRelationType.SAME_FAMILY_AS,
        CaseRelationType.SHARES_ARTIFACT_WITH,
    }
)


def canonicalize_relation_endpoints(
    subject_memory_id: str,
    predicate: CaseRelationType,
    object_memory_id: str,
) -> tuple[str, str]:
    validate_memory_id(subject_memory_id)
    validate_memory_id(object_memory_id)
    if subject_memory_id == object_memory_id:
        raise ValueError("case relation cannot be a self relation")
    if predicate in _SYMMETRIC_RELATIONS:
        return tuple(sorted((subject_memory_id, object_memory_id)))
    return subject_memory_id, object_memory_id
```

`validate_case_relation()` additionally requires `artifact_ref` for `SHARES_ARTIFACT_WITH` and a finite confidence in `[0.0, 1.0]`; reject `NaN`/infinity before comparison or persistence.

- [ ] **Step 4: Add frozen record dataclasses**

Define the exact frozen records in `case_schema.py`:

```python
@dataclass(frozen=True)
class CaseRecord:
    case_id: str
    name: str
    state: Literal["active", "disabled", "deleted"]
    revision: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class CaseMember:
    case_id: str
    memory_id: str
    status: Literal["current", "removed"]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PromotionSource:
    source_memory_id: str
    source_record_id: str
    source_revision: int
    source_hash: str
    namespace_address: str = ""


@dataclass(frozen=True)
class CaseRelation:
    relation_id: str
    case_id: str
    subject_memory_id: str
    predicate: CaseRelationType
    object_memory_id: str
    confidence: float
    sources: tuple[PromotionSource, ...]
    artifact_ref: str = ""
    revision: int = 1
    state: Literal["current", "inactive"] = "current"


@dataclass(frozen=True)
class CasePromotion:
    promotion_id: str
    case_fact_id: str
    case_id: str
    promotion_kind: str
    source: PromotionSource
    revision: int
```

Validate every generated ID, timestamp string, state, finite confidence and source tuple at repository boundaries.

- [ ] **Step 5: Run tests and commit**

Run: `uv run python -m pytest tests/memory/cases/test_schema.py -v`

Expected: PASS.

```bash
git add lucnhan/memory/case_schema.py tests/memory/cases/test_schema.py
git commit -m "feat(memory): define analysis case schema"
```

---

### Task 2: Case registry CRUD and membership

**Files:**
- Modify: `lucnhan/memory/registry.py`
- Create: `lucnhan/memory/case_repository.py`
- Create: `tests/memory/cases/test_repository.py`

**Interfaces:**
- Consumes: case schema and locator.
- Produces exact signatures: `create_case(name: str) -> CaseRecord`, `get_case(case_id: str) -> CaseRecord`, `rename_case(case_id: str, name: str, expected_revision: int) -> CaseRecord`, `soft_delete_case(case_id: str, expected_revision: int) -> CaseRecord`, `retry_create_workspace(case_id: str, expected_revision: int) -> CaseRecord`, `add_member(case_id: str, memory_id: str, expected_case_revision: int) -> CaseMember`, `remove_member(case_id: str, memory_id: str, expected_case_revision: int) -> CaseMember`, `list_cases_for_memory(memory_id: str) -> tuple[CaseRecord, ...]`, `list_members(case_id: str, *, current_only: bool = True) -> tuple[CaseMember, ...]`, `is_current_member(case_id: str, memory_id: str) -> bool`.
- Consumed by: Tasks 3–8.

- [ ] **Step 1: Write failing CRUD/member tests**

```python
from __future__ import annotations

from pathlib import Path

from lucnhan.memory.case_repository import CaseRepository
from lucnhan.memory.registry import MemoryRegistry
from lucnhan.memory.workspace import MemoryLocator, new_memory_id


def test_case_membership_is_explicit_and_soft_delete_is_non_destructive(tmp_path: Path) -> None:
    locator = MemoryLocator(tmp_path)
    registry = MemoryRegistry(locator.registry_database())
    registry.initialize()
    member = registry.create_workspace("binary", "loader.i64", memory_id=new_memory_id())
    cases = CaseRepository(registry, locator)
    case = cases.create_case("Incident 2026-07")

    assert cases.list_members(case.case_id) == []
    cases.add_member(
        case.case_id,
        member.memory_id,
        expected_case_revision=case.revision,
    )
    assert [item.memory_id for item in cases.list_members(case.case_id)] == [member.memory_id]
    current = cases.get_case(case.case_id)

    cases.soft_delete_case(case.case_id, expected_revision=current.revision)
    assert cases.get_case(case.case_id).state == "deleted"
    assert registry.workspace(member.memory_id) is not None
```

- [ ] **Step 2: Run and verify missing repository**

Run: `uv run python -m pytest tests/memory/cases/test_repository.py -v`

Expected: FAIL.

- [ ] **Step 3: Add registry migration and exact case-revision semantics**

```sql
CREATE TABLE cases(
    case_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('active','disabled','deleted')),
    revision INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE case_members(
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    memory_id TEXT NOT NULL REFERENCES workspaces(memory_id),
    status TEXT NOT NULL CHECK(status IN ('current','removed')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(case_id, memory_id)
);
CREATE INDEX ix_case_members_memory ON case_members(memory_id, status);
```

Bump registry schema version and add transactional migration test from v1 to v2.

- [ ] **Step 4: Implement revision-safe CRUD and membership**

Use generated `case_id`. Names are display-only and need not be globally unique. Every successful rename/add/remove/delete executes `UPDATE cases SET revision = revision + 1 ... WHERE case_id = ? AND revision = ?` in the same transaction; zero rows raises `StaleRevisionError`. Re-adding a removed member reactivates the row, does not duplicate it, and increments case revision. `remove_member()` changes status, does not delete row/workspace. Deleting a case sets tombstone; no filesystem deletion.

- [ ] **Step 5: Create case workspace DB on case creation**

Use `MemoryLocator.case(case_id)` and `WorkspaceStore.create(paths, owner_memory_id=case_id, workspace_kind="case")`. If DB creation fails after registry insert, a compensation transaction marks case `disabled`, increments revision, and does not report success. `retry_create_workspace(case_id, expected_revision)` is an explicit UI/service action that creates the missing DB and transitions `disabled → active`; active use/membership/retrieval reject disabled cases. Add failure-injection and retry tests.

- [ ] **Step 6: Run tests and commit**

Run: `uv run python -m pytest tests/memory/cases/test_repository.py tests/memory/test_registry.py -v`

Expected: PASS.

```bash
git add lucnhan/memory/registry.py lucnhan/memory/case_repository.py tests/memory/cases/test_repository.py tests/memory/test_registry.py lucnhan/constants.py
git commit -m "feat(memory): add case membership registry"
```

---

### Task 3: Active-case session binding and generation

**Files:**
- Modify: `lucnhan/memory/manager.py`
- Modify: `lucnhan/state/session.py`
- Modify: `lucnhan/state/history.py`
- Modify: `lucnhan/ui/session_controller_base.py`
- Create: `tests/memory/cases/test_binding.py`

**Interfaces:**
- Consumes: `CaseRepository`.
- Produces: `MemoryWorkspaceManager.set_active_case(case_id)`, `clear_active_case()`, `validate_case_context()`.
- Consumed by: commands/UI/promotion/retrieval.

- [ ] **Step 1: Write failing active-case tests**

Test: nonmember cannot become active; one binary can list two cases; session has one active ID; switching increments `case_binding_generation`; old run context cannot write case; removal/deletion clears active case; restore invalid membership falls back empty.

- [ ] **Step 2: Run and verify failure**

Run: `uv run python -m pytest tests/memory/cases/test_binding.py -v`

Expected: FAIL.

- [ ] **Step 3: Implement binding methods**

```python
def set_active_case(self, case_id: str) -> MemoryRunContext:
    binding = self.require_binary_binding()
    if not self._cases.is_current_member(case_id, binding.memory_id):
        raise CaseMembershipError(f"{binding.memory_id} is not a current member of {case_id}")
    if self._active_case_id != case_id:
        self._active_case_id = case_id
        self._case_binding_generation += 1
    return self.run_context(case_id)
```

Clear on membership removal/soft deletion and update every active tab/session bound to that case. Disable UI switching during persistence-capable run; controller alternatively rejects via generation after race.

- [ ] **Step 4: Filter session restore**

Restore `active_case_id` only when `binary_memory_id` matches the resolved binding and membership is current. Keep invalid case ID in neither active state nor prompt; log a warning.

- [ ] **Step 5: Run tests and commit**

Run: `uv run python -m pytest tests/memory/cases/test_binding.py tests/state/test_memory_binding.py tests/agent/test_session_controller.py -v`

Expected: PASS.

```bash
git add lucnhan/memory/manager.py lucnhan/state/session.py lucnhan/state/history.py lucnhan/ui/session_controller_base.py tests/memory/cases/test_binding.py
git commit -m "feat(memory): bind sessions to active cases"
```

---

### Task 4: Case relations and exact-signal suggestions

**Files:**
- Modify: `lucnhan/memory/case_repository.py`
- Modify: `lucnhan/memory/workspace_store.py`
- Create: `lucnhan/memory/suggestions.py`
- Create: `tests/memory/cases/test_suggestions.py`
- Modify: `tests/memory/cases/test_repository.py`

**Interfaces:**
- Produces: `CaseRepository.put_relation()`, `list_relations()`, `deactivate_relations_for_member()`.
- Produces: `MembershipSuggestionEngine.suggest(candidate_memory_id, case_id) -> list[CaseSuggestion]`.
- Consumed by: peer retrieval and UI.

- [ ] **Step 1: Write failing relation persistence tests**

Assert directed endpoint order, symmetric deduplication, relation source requirements, current membership requirement, remove member deactivates relations, and soft-deleted case rejects writes.

- [ ] **Step 2: Write failing suggestion purity tests**

Seed already-stored exact signals (same directory metadata, exact artifact hash, import/export pair, rare string marker, existing relation). Assert suggestions include evidence and score but membership table remains unchanged. Mock provider/IDA/embedding entrypoints and assert zero calls.

- [ ] **Step 3: Implement case relation tables/API**

Use case workspace `relations` with generated `relation_id`, subject/object memory IDs plus predicate, artifact ref, confidence and typed source joins. Add uniqueness over canonical endpoint/predicate/artifact/current state; repeated writes resolve the current row through that unique semantic key rather than inventing deterministic authoritative IDs. Require both endpoints current members at write time.

- [ ] **Step 4: Implement exact-signal suggestion engine**

```python
@dataclass(frozen=True)
class CaseSuggestion:
    case_id: str
    candidate_memory_id: str
    score: float
    reasons: tuple[str, ...]


_SIGNAL_WEIGHTS = {
    "exact_artifact_hash": 1.0,
    "existing_relation": 0.9,
    "import_export_pair": 0.75,
    "rare_exact_marker": 0.65,
    "same_directory": 0.2,
}
```

Read only registry/workspace-stored metadata; no new scans. Deduplicate reasons and deterministic order by score/case ID.

- [ ] **Step 5: Run tests and commit**

Run: `uv run python -m pytest tests/memory/cases/test_repository.py tests/memory/cases/test_suggestions.py -v`

Expected: PASS.

```bash
git add lucnhan/memory/case_repository.py lucnhan/memory/workspace_store.py lucnhan/memory/suggestions.py tests/memory/cases/test_repository.py tests/memory/cases/test_suggestions.py
git commit -m "feat(memory): add case relations and suggestions"
```

---

### Task 5: Explicit promotion and lazy source drift

**Files:**
- Create: `lucnhan/memory/case_service.py`
- Modify: `lucnhan/memory/case_repository.py`
- Create: `tests/memory/cases/test_promotion.py`

**Interfaces:**
- Consumes: main-agent authority, binary/case repositories, active case context.
- Produces: `CaseMemoryService.promote()`, `evaluate_source_state()`, `list_case_facts(include_drifted)`.
- Consumed by: peer retriever, commands/UI.

- [ ] **Step 1: Write failing explicit/idempotent promotion tests**

Test no active case, no membership, no authority, or implicit retrieval cannot promote. Explicit promotion creates one case fact and one promotion row; same tuple rerun returns same promotion. Source revision change marks lazy state `source_changed`; deleted/missing source state `source_missing`.

- [ ] **Step 2: Run and verify missing service**

Run: `uv run python -m pytest tests/memory/cases/test_promotion.py -v`

Expected: FAIL.

- [ ] **Step 3: Implement exact promotion trigger contract**

```python
def promote(
    self,
    authority: MemoryWriteAuthority,
    context: MemoryRunContext,
    *,
    source_record_id: str,
    promotion_kind: str,
    expected_source_revision: int | None = None,
) -> CasePromotion:
    self._binary_service.require_write_authority(authority)
    self._manager.require_valid_case_context(context)
    source_memory_id = context.binary_memory_id
    self._cases.require_current_member(context.active_case_id, source_memory_id)
    source = self._binary_service.repository.get_current_fact(source_record_id)
    if expected_source_revision is not None and source.revision != expected_source_revision:
        raise StaleRevisionError("selected source revision changed")
    return self._cases.put_promotion_idempotent(
        case_id=context.active_case_id,
        source_memory_id=source_memory_id,
        source_record_id=source_record_id,
        source_revision=source.revision,
        source_hash=source.content_hash,
        promotion_kind=promotion_kind,
        title=source.title,
        content=source.content,
    )
```

Only call this from explicit action/command/request marker; no `save_fact()` or retrieval hook calls it. Direct promotion copies the verified immutable source title/content. A future summarized/derived promotion must be a separate `promotion_kind` with both source and derived-content hashes; it must not overload this API.

- [ ] **Step 4: Implement lazy source evaluator**

Open source DB read-only/query-only. Compare stored `(memory_id, record_id, revision, hash)` to current/exact revision. Cache evaluation by source DB mtime/revision but do not transactionally update binary DB. Automatic lists exclude non-current; explicit lists include status.

- [ ] **Step 5: Run tests and commit**

Run: `uv run python -m pytest tests/memory/cases/test_promotion.py -v`

Expected: PASS.

```bash
git add lucnhan/memory/case_service.py lucnhan/memory/case_repository.py tests/memory/cases/test_promotion.py
git commit -m "feat(memory): promote facts into analysis cases"
```

---

### Task 6: Controlled peer retrieval and wrappers

**Files:**
- Create: `lucnhan/memory/peer_retrieval.py`
- Modify: `lucnhan/memory/context.py`
- Modify: `lucnhan/memory/workspace_store.py`
- Create: `tests/memory/cases/test_peer_retrieval.py`

**Interfaces:**
- Produces: `PeerRetrievalQuery(case_id: str, active_memory_id: str, query: str, max_chars: int)`, `PeerCandidate`, `PeerContextRecord(record_id: str, record_type: str, rendered: str, citations: tuple[str, ...])`, `PeerContextPack(peers: tuple[PeerCandidate, ...], records: tuple[PeerContextRecord, ...], rendered: str, used_chars: int)`, `PeerMemoryRetriever.retrieve()`.
- Produces via `memory/context.py`: `PersistentContextPack(active: str, case: str, peers: str, used_chars: int, source_ids: tuple[str, ...])`.
- Consumed by: Agent prompt task.

- [ ] **Step 1: Write failing eligibility tests**

Seed four peers:

- direct relation confidence `0.70` → eligible;
- direct relation `0.69` → ineligible;
- exact artifact hash with no relation → eligible;
- high lexical similarity only → ineligible.

Assert max three peers, max five facts/peer, deterministic tie order.

- [ ] **Step 2: Write failing content-exclusion/read-only tests**

Seed fact, entity, relation, observation, unmanaged Markdown, note, report, superseded fact and drifted promotion. Assert auto context includes only current fact/entity/relation summary. Delete a peer DB and assert retrieval does not recreate it. For an existing peer, assert `PRAGMA query_only == 1` and that an insert fails with `sqlite3.OperationalError`, proving actual SQLite URI `mode=ro` plus query-only defense.

- [ ] **Step 3: Write failing wrapper safety/budget tests**

Use display name containing quotes/`</peer_memory>` and oversized content. Assert XML attributes escaped, body sanitized/truncated before wrapper, closing tag retained, citation includes shortened ID, 55/30/15 allocation and redistribution exact by character count.

- [ ] **Step 4: Implement eligibility and ranking**

```python
@dataclass(frozen=True)
class PeerCandidate:
    memory_id: str
    eligibility_reason: str
    eligibility_score: float
    ranking_score: float


import math


def _eligible(relation_confidence: float | None, exact_artifact_match: bool) -> bool:
    relation_is_strong = (
        relation_confidence is not None
        and math.isfinite(relation_confidence)
        and relation_confidence >= 0.7
    )
    return exact_artifact_match or relation_is_strong
```

Ranking adds bounded local lexical/current-goal score, record confidence and freshness. No provider/embedding.

- [ ] **Step 5: Implement budget allocator and safe rendering**

```python
@dataclass(frozen=True)
class ContextAllocation:
    active_chars: int
    case_chars: int
    peer_chars: int


def allocate_context(total: int, *, has_case: bool, has_peers: bool) -> ContextAllocation:
    case = int(total * 0.30) if has_case else 0
    peer = int(total * 0.15) if has_case and has_peers else 0
    return ContextAllocation(total - case - peer, case, peer)
```

Divide peer allocation among at most three peers, then five records each. Escape attributes with `html.escape(value, quote=True)`. Truncate sanitized bodies before wrapping.

- [ ] **Step 6: Run tests and commit**

Run: `uv run python -m pytest tests/memory/cases/test_peer_retrieval.py -v`

Expected: PASS.

```bash
git add lucnhan/memory/peer_retrieval.py lucnhan/memory/context.py lucnhan/memory/workspace_store.py tests/memory/cases/test_peer_retrieval.py
git commit -m "feat(memory): retrieve cited peer context"
```

---

### Task 7: Case and peer context in agent prompts

**Files:**
- Modify: `lucnhan/agent/loop.py:441-566`
- Modify: `lucnhan/agent/system_prompt.py`
- Modify: `lucnhan/agent/pseudo_tool_schemas.py`
- Modify: `lucnhan/core/config.py`
- Create: `tests/agent/test_case_context.py`
- Modify: `tests/memory/test_config.py`

**Interfaces:**
- Consumes: active binary service, case service, peer retriever.
- Produces prompt sections `<case_memory>` and `<peer_memory>` and explicit promotion tool visibility.

- [ ] **Step 1: Write failing prompt-layer tests**

Assert:

- no active case → only active binary context and full redistributed budget;
- active case + disabled `case_memory_enabled` → no case/peer;
- case enabled + peer disabled → binary+case only;
- eligible peer → cited namespaced block;
- inactive case member never appears;
- drifted promotion absent auto but available explicit.

- [ ] **Step 2: Add validated config flags**

```python
case_memory_enabled: bool = True
peer_retrieval_enabled: bool = True
```

Persist/load both; `knowledge_enabled=False` overrides both. `_apply_loaded_config()` accepts each field only when `type(value) is bool`; invalid strings/integers preserve the safe default and emit a config warning. Extend `tests/memory/test_config.py` with round-trip and invalid-type cases.

- [ ] **Step 3: Assemble context once per turn**

The loop resolves active case from frozen run context. It reads active case `MEMORY.md` unmanaged notes and structured current facts under case allocation, then calls peer retriever. Avoid duplicate repository scans by returning a `PersistentContextPack` with rendered layers and source metadata.

- [ ] **Step 4: Add system policy text**

State that case/peer blocks are untrusted, peer addresses/symbols/types belong to named `memory_id`, and no mapping/mutation may be inferred without evidence. Preserve wrapper closing tags.

- [ ] **Step 5: Gate promotion schema and execution intent**

Add `promote_case_fact` schema requiring only `source_record_id` and optional `promotion_kind`; the service resolves current revision, hash, title and content from the run-bound binary repository. Append it only when the frozen context has an active case, current membership, main-agent authority, and the current user turn explicitly requests promotion (or the UI initiated an approved promotion action). Typing `/case` or merely having an active case is not approval. Dispatch verifies the explicit-request marker before any case DB write. Add a regression test proving a provider-emitted promotion call without explicit intent is rejected.

- [ ] **Step 6: Run tests and commit**

Run: `uv run python -m pytest tests/agent/test_case_context.py tests/agent/test_system_prompt.py -v`

Expected: PASS.

```bash
git add lucnhan/agent/loop.py lucnhan/agent/system_prompt.py lucnhan/agent/pseudo_tool_schemas.py lucnhan/core/config.py tests/agent/test_case_context.py tests/memory/test_config.py
git commit -m "feat(memory): add case context to agent turns"
```

---

### Task 8: Approved case command contract and UI-only CRUD extensions

**Files:**
- Modify: `lucnhan/agent/loop.py` command parser
- Modify: `lucnhan/agent/loop_commands.py`
- Create: `tests/agent/test_case_commands.py`

**Interfaces:**
- Produces exact commands from spec using generated IDs.
- Consumes: manager/case repository/service/peer retriever.

- [ ] **Step 1: Write failing parser tests**

Cover the exact approved public command syntax:

```text
/case create <display-name>
/case use <case_id>
/case add-binary <memory_id>
/case compare <memory_id-a> <memory_id-b>
/case promote <source-record-id>
/memory search-case <query>
/memory search-binary <memory_id> <query>
```

Assert names resolve only when unique; IDs canonical; nonmember targets return structured non-mutating errors. Case remove/delete/rename and relation authoring remain explicit Knowledge UI/service actions in v1; do not silently add extra slash-command grammar without a spec amendment.

- [ ] **Step 2: Write failing command integration tests**

Assert create does not add member automatically; add is explicit; use validates membership; compare/search require active case/current member; promote requires confirmation/explicit request and resolves the current immutable source revision internally (UI may provide `expected_source_revision` to reject a stale selection). Separate service/UI tests verify that member removal clears an active case and case deletion remains soft.

- [ ] **Step 3: Implement command dataclasses and parsers**

Use a dedicated frozen `_ParsedCaseCommand(action: str, args: tuple[str, ...])` rather than ad-hoc string branches. Parse with `shlex.split` for quoted display names, enforce at most 8 arguments, 256 characters per display/query argument, and existing prompt-input sanitization. Never accept filesystem paths in case commands.

- [ ] **Step 4: Implement handlers with structured error kinds**

Return `case_not_found`, `case_ambiguous`, `not_a_member`, `no_active_case`, `confirmation_required`, or `storage_unavailable`. UI-only relation validation may additionally return `invalid_relation`. Destructive delete/remove uses the UI service approval flow, never direct model dispatch.

- [ ] **Step 5: Run tests and commit**

Run: `uv run python -m pytest tests/agent/test_case_commands.py -v`

Expected: PASS.

```bash
git add lucnhan/agent/loop.py lucnhan/agent/loop_commands.py tests/agent/test_case_commands.py
git commit -m "feat(memory): add analysis case commands"
```

---

### Task 9: Case selector, membership, relation, promotion UI

**Files:**
- Modify: `lucnhan/ui/knowledge_panel.py`
- Modify: `lucnhan/ui/panel_core.py`
- Modify: `lucnhan/ui/session_controller_base.py`
- Create: `tests/ui/test_case_memory_ui.py`

**Interfaces:**
- Consumes: case repository/service/suggestions and active binding manager.
- Produces UI signals for explicit case operations; no autonomous membership mutation.

- [ ] **Step 1: Write failing selector/membership tests**

Assert case selector includes `No active case`, duplicate names show short IDs, selection disabled while run active, selecting invalid/nonmember case rejected, member add/remove explicit, suggestions visually distinct and do not mutate registry. Inject a slow/busy repository call and prove the Qt callback returns immediately while completion arrives through the queue.

- [ ] **Step 2: Write failing promotion/relation tests**

Assert promote preview shows source binary/address/revision/hash; confirmation required; relation form enforces predicate direction/artifact requirement; source-drift warning visible; removal/deletion clears peer rows immediately.

- [ ] **Step 3: Add exact signals**

```python
case_create_requested = Signal(str)
case_select_requested = Signal(str)
case_member_add_requested = Signal(str, str)
case_member_remove_requested = Signal(str, str)
case_relation_requested = Signal(object)
case_promotion_requested = Signal(object)
case_delete_requested = Signal(str)
```

Signals carry validated IDs/data objects, not paths.

- [ ] **Step 4: Wire controller actions and generation safety**

Controller performs membership/active-case operations, increments generation, updates all tabs bound to same binary, clears peer context cache and refreshes panel. Run-active case switch is disabled and handler still rejects stale races. All registry/case/peer SQLite work runs in the existing background worker/queue pattern; Qt callbacks only submit requests and render immutable results, so `busy_timeout` never blocks the UI thread.

- [ ] **Step 5: Run UI tests and commit**

Run: `uv run python -m pytest tests/ui/test_case_memory_ui.py tests/tools/test_panel_core.py -v`

Expected: PASS.

```bash
git add lucnhan/ui/knowledge_panel.py lucnhan/ui/panel_core.py lucnhan/ui/session_controller_base.py tests/ui/test_case_memory_ui.py tests/tools/test_panel_core.py
git commit -m "feat(memory): add analysis case ui"
```

---

### Task 10: Analysis-case integration and concurrency gate

**Files:**
- Modify: `tests/memory/cases/test_repository.py`
- Modify: `tests/memory/cases/test_promotion.py`
- Modify: `tests/memory/cases/test_peer_retrieval.py`
- Modify: `README.md`, `ARCHITECTURE.md`, `AGENTS.md`, `CLAUDE.md`, `llms.txt`

**Interfaces:**
- Consumes all case tasks.
- Produces release-ready case feature.

- [ ] **Step 1: Add two-process shared-case write test**

Two binary processes concurrently promote different facts and relations into one case. Assert both commits survive, no duplicate promotion, relation endpoints valid, case `MEMORY.md` has one managed region and both summaries.

- [ ] **Step 2: Add end-to-end loader/payload scenario**

Create loader/payload workspaces, explicit case membership, direct `embeds_or_loads` relation, promoted case fact, peer fact at address. Build loader prompt and assert active/case/peer layering, payload namespace/citation, no peer raw note/pseudocode, exact budget.

- [ ] **Step 3: Add member-removal/source-drift scenario**

Remove payload member during a stale run. Assert old context case write rejected, relation inactive, automatic peer disappears, explicit historical case search labels source/member state without mutation.

- [ ] **Step 4: Update documentation**

Document manual grouping, active-case selector, five relations, explicit promotion, controlled retrieval, citations, budgets, commands and privacy controls. State same-folder is suggestion only.

- [ ] **Step 5: Run all case and regression tests**

Run: `uv run python -m pytest tests/memory/cases tests/agent/test_case_context.py tests/agent/test_case_commands.py tests/ui/test_case_memory_ui.py -v`

Expected: PASS.

Run: `uv run python -m pytest tests/ lucnhan/tests/ -q`

Expected: PASS.

- [ ] **Step 6: Run static checks and commit**

Run: `uvx ruff format --check lucnhan/ tests/`

Run: `uvx ruff check lucnhan/ tests/`

Run: `uvx mypy lucnhan/core lucnhan/providers --pretty`

Expected: PASS.

```bash
git add tests/memory/cases tests/agent/test_case_context.py tests/agent/test_case_commands.py tests/ui/test_case_memory_ui.py README.md ARCHITECTURE.md AGENTS.md CLAUDE.md llms.txt
git commit -m "test(memory): verify analysis case workflows"
```

---

## Analysis Case Exit Checklist

- [ ] Cases/membership use generated IDs and explicit actions.
- [ ] One binary may join multiple cases; one session has one active case.
- [ ] Soft delete/removal preserve binary data and deactivate relations.
- [ ] Five relation semantics and source constraints are enforced.
- [ ] Suggestions use stored exact signals and never mutate membership.
- [ ] Promotion is explicit, idempotent, authority-bound, provenance-preserving.
- [ ] Source drift is evaluated lazily and excluded from auto injection.
- [ ] Peer eligibility threshold/exact artifact rules are deterministic.
- [ ] Peer DBs are read-only/query-only and never created by retrieval.
- [ ] Namespace, citation, truncation and character budgets pass tests.
- [ ] Commands/UI reject unrelated targets and stale generations.
- [ ] Two-process case promotion/relation integration passes.
