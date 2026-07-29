# Knowledge UI and Write Path SQLite Migration Design

**Date:** 2026-07-29
**Status:** Draft for user review
**Scope:** Second hardening tranche after the Memory Durability and Orchestra Gate plan (2026-07-22)

## 1. Purpose

This tranche migrates the Knowledge panel read path and the exploration/research write paths from the legacy JSONL `KnowledgeRawStore` to the SQLite `WorkspaceStore` shipped in the Memory Durability tranche. The migration closes the visible gap where `save_memory` tool calls write to SQLite (invisible to the UI panel) while the Knowledge panel reads JSONL (which the tool does not populate).

It intentionally does not delete the JSONL store, rewrite the ranker, migrate the notes subsystem, or extend schema for the remaining graph metadata fields (`importance`, `verified`, `source_refs`, `relation_refs`). Those are deferred to follow-up tranches.

## 2. Confirmed problems

### 2.1 Knowledge panel reads the wrong store

`SessionControllerBase._refresh_knowledge_panel` (in `rikugan/ui/panel_core.py`) calls `make_store(idb_path)` and reads `store.list_memories() / list_entities() / list_relations()`, which all read JSONL files via `KnowledgeRawStore`. The Memory Durability tranche rewired the `save_memory` tool to write SQLite via `BinaryMemoryService.save_fact()`. Facts saved via chat therefore never appear in the Knowledge panel.

### 2.2 Retrieved knowledge section reads the wrong store

`AgentLoop._build_retrieved_knowledge_section` (in `rikugan/agent/loop.py`) also calls `make_store` and feeds JSONL data into the existing ranker. The system prompt's "Retrieved Knowledge" block therefore disagrees with what the Knowledge panel shows and with what `save_memory` persists.

### 2.3 Exploration and research writes still target JSONL only

`ingest_exploration_finding` and `ingest_research_note` (in `rikugan/memory/ingest.py`) continue to write to `KnowledgeRawStore`. Their output is invisible to any future SQLite-only consumer (retrieval adapter, compact projections, cross-binary retrieval) and to the new central-memory prompt source.

### 2.4 Legacy JSONL users have no upgrade path

A user who has accumulated JSONL knowledge from a pre-durability Rikugan version cannot see that data after upgrading to the SQLite-backed memory subsystem. There is no migration path.

## 3. Goals

1. The Knowledge panel reads from the SQLite `WorkspaceStore` whenever a `BinaryMemoryService` is wired, and falls back to JSONL only when central memory is unavailable (ephemeral identity resolution).
2. The Retrieved Knowledge section in the system prompt reads from the same SQLite source via a ranking adapter, producing the same `RetrievalPack` shape the existing ranker emits.
3. Exploration and research findings are dual-written to SQLite (primary) and JSONL (legacy) during a transition period controlled by a module-level flag, so either store failure does not block the other.
4. Users with legacy JSONL data automatically import it into SQLite on the first IDB open after upgrade, idempotently, without deleting the source files.
5. The schema is extended to v3 to carry the graph metadata required for ranking and display: `entity_refs` and `tags` on facts, `tags` on entities, `evidence` on relations.
6. All existing pre-durability and durability tests remain green.

## 4. Non-goals

This tranche does not:

- flip the dual-write flag to `False` (deferred one release cycle after this tranche ships);
- delete `KnowledgeRawStore`, `make_store`, or the JSONL write helpers;
- migrate the notes subsystem (still filesystem-backed under `paths.notes_dir`);
- add `importance`, `verified`, `source_refs`, or `relation_refs` columns;
- rewrite the retrieval ranker (only the data source changes);
- add a user-facing re-import command, runtime Settings flag, or progress UI for legacy import;
- backfill graph metadata on existing SQLite records (defaults are empty);
- change bundle import/export, peer retrieval, case workspace behavior, or any path touched only by the durability tranche.

## 5. Schema v3

`MEMORY_WORKSPACE_SCHEMA_VERSION` increases from 2 to 3. The migration is additive and does not backfill:

```sql
ALTER TABLE facts ADD COLUMN entity_refs TEXT NOT NULL DEFAULT '[]';
ALTER TABLE facts ADD COLUMN tags TEXT NOT NULL DEFAULT '[]';

ALTER TABLE entities ADD COLUMN tags TEXT NOT NULL DEFAULT '[]';

ALTER TABLE relations ADD COLUMN evidence TEXT NOT NULL DEFAULT '';
```

Array columns store `json.dumps(list, ensure_ascii=False, sort_keys=True)`. The repository layer converts to and from Python `list[str]` on every read/write.

The `FactRecord`, `EntityRecord`, and `RelationRecord` frozen dataclasses gain matching fields:

- `FactRecord.entity_refs: list[str]`, `FactRecord.tags: list[str]`
- `EntityRecord.tags: list[str]`
- `RelationRecord.evidence: str`

Existing call sites construct these dataclasses field-by-field (not positionally), so the additive change does not break production callers.

Migration runs through the existing `open_sqlite()` chain. The backup-aware writable open (`open_workspace_for_write`) introduced in the durability tranche produces a verified backup before the v2 → v3 migration runs. Migration failure rolls back the schema change and the `user_version` bump, leaving the database untouched at v2.

Records created via the existing `save_memory` tool path continue to use empty defaults for the new columns, because `save_memory` does not carry graph metadata. Records created via the new `save_exploration_finding` repository method, or imported from legacy JSONL, populate the columns.

Older code that only supports schema v2 must continue to reject a v3 workspace rather than opening it incorrectly, matching the durability tranche's downgrade contract.

## 6. Write paths

### 6.1 Module-level dual-write flag

```python
# rikugan/memory/ingest.py
_LEGACY_JSONL_DUAL_WRITE = True
```

When `True`, exploration and research writes hit SQLite first and JSONL second. When `False`, only SQLite is written. The flag is module-level so it can be flipped in a single commit or monkeypatched in tests.

`save_memory` tool writes remain SQLite-only. They were never dual-write and this tranche does not change that.

### 6.2 Exploration and research dual-write

`ingest_exploration_finding` and `ingest_research_note` gain a keyword-only `memory_service: BinaryMemoryService | None` parameter (default `None`). The existing `(store, paths)` positional parameters remain and stay optional (`KnowledgeRawStore | None`, `KnowledgePaths | None`) so JSONL-only test fixtures continue to work without a service. When `memory_service` is `None` and the dual-write flag is `True`, the function behaves exactly as today: JSONL-only write. When `memory_service` is provided, the SQLite write runs first and the JSONL write follows if the flag allows. Callers in `AgentLoop` pass the loop's wired service; tests can omit it.

The body splits into two private helpers behind the existing public signature:

- `_write_exploration_to_sqlite(memory_service, ...)` calls a new repository method and persists `entity_refs`, `tags`, `evidence`, and confidence.
- `_write_exploration_to_jsonl(store, paths, ...)` is the current logic, unchanged.

The SQLite write runs first. If it raises, the JSONL write still runs and the agent continues (best-effort, matching the current contract). If the JSONL write raises, the SQLite write has already succeeded and the agent continues. Both branches log the error; neither re-raises.

When `memory_service` is `None`, only the JSONL write runs (the current behavior). This preserves the existing exploration/research path when central memory is not wired.

`_LEGACY_JSONL_DUAL_WRITE = False` skips the JSONL write entirely, even when `store` and `paths` are provided.

### 6.3 New repository and service methods

`SQLiteKnowledgeRepository.save_exploration_finding` mirrors `save_memory_fact` and adds the graph metadata:

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
) -> SavedKnowledgeMemory
```

`BinaryMemoryService.save_exploration_finding` wraps it with the existing authority check and projection behavior, returning the existing `SaveMemoryResult`.

`WorkspaceStore.save_fact_if_semantically_absent` (from the durability tranche) gains optional keyword-only `entity_refs` and `tags` parameters that flow into the `INSERT INTO facts` statement. The atomic lookup-insert-observe transaction invariant is preserved.

Entities and relations gain symmetric extensions on `WorkspaceStore.put_entity` / `put_relation` (optional keyword-only `tags` and `evidence` respectively), with matching `EntityRecord.tags` / `RelationRecord.evidence` fields populated on read.

## 7. Legacy JSONL import

### 7.1 Adapter

A new module `rikugan/memory/jsonl_migration.py` provides:

```python
def jsonl_to_bundle_envelopes(
    store: KnowledgeRawStore,
    paths: KnowledgePaths,
) -> list[dict[str, Any]]
```

Each envelope matches the bundle wire format consumed by `import_workspace_bundle`:

```json
{
  "record_type": "fact",
  "record_id": "<origin_jsonl_id>",
  "payload": {
    "type": "...",
    "title": "...",
    "content": "...",
    "confidence": 0.7,
    "entity_refs": ["func:0x401000"],
    "tags": ["function_purpose"]
  }
}
```

Entities and relations emit analogous envelopes. The adapter does not synthesize fields the JSONL store does not carry (for example, the bundle's optional `confidence` defaults are applied downstream by `import_workspace_bundle`).

`write_envelopes_to_temp_bundle(envelopes, origin_memory_id)` materializes the envelopes into a temporary ZIP file on disk, in the same layout `export_workspace` produces (`manifest.json` + `records/facts.jsonl` + ...). The caller is responsible for deleting the temp file.

### 7.2 Trigger

`maybe_import_legacy_jsonl(workspace_store, owner_memory_id, jsonl_paths)` runs once per workspace, gated by the `workspace_meta` key `legacy_jsonl_imported`:

1. If `workspace_meta` already has the key, return immediately.
2. Build a `KnowledgeRawStore` from `jsonl_paths`. If it has no records, set the marker and return.
3. Convert records to envelopes via the adapter.
4. Write a temp bundle, call `import_workspace_bundle(bundle_path, repo)`, then delete the temp file in `finally`.
5. Set `workspace_meta` key `legacy_jsonl_imported` to the current ISO timestamp and commit.

The trigger runs from `SessionControllerBase._wire_central_memory` immediately after the writable workspace open succeeds, before the loop's first turn. It does not run when `memory_service` is not wired (ephemeral identity resolution).

### 7.3 Idempotency

`import_workspace_bundle` (shipped in the durability tranche) is record-count idempotent via deterministic destination IDs and payload matching. Re-running the trigger therefore does not duplicate records. The marker additionally prevents the trigger from re-running on every IDB open.

The deterministic ID source is `deterministic_import_record_id(target_memory_id, "legacy-jsonl-import", record_type, origin_id)`. The same JSONL record imported into two different targets produces two different destination IDs, matching the bundle import contract.

The adapter does not delete JSONL files. They remain on disk as a fallback if a future SQLite regression requires recovery.

## 8. Knowledge panel read path

`SessionControllerBase._refresh_knowledge_panel` prefers SQLite when `self._ctrl.memory_service` is not `None`:

- Read `service.repository.list_memories()`, `list_entities()`, `list_relations()`, and `count_observations()`.
- Use `service.paths.notes` for the notes directory.
- Call `panel.populate` and `panel.set_counts` exactly as today.

When `memory_service` is `None`, the panel falls back to the existing `make_store` JSONL path. The fallback is the only production code path that continues to call `make_store` after this tranche.

The Knowledge panel subscribes to a new event so it refreshes after a successful `save_memory` tool call. `AgentLoop._handle_save_memory_tool` emits the event when the save outcome is `created` (a new record) or `deduplicated` (a new observation landed). The event type is a new `TurnEventType.MEMORY_SAVED` constant. The existing 50 ms debounce in `_on_knowledge_event_refresh` coalesces bursts. Reusing `KNOWLEDGE_RETRIEVED` is rejected because retrieval is a read-side concept and conflating it with a write event would confuse the event stream semantics.

## 9. Retrieved knowledge section

`AgentLoop._build_retrieved_knowledge_section` prefers SQLite via a new adapter:

```python
# rikugan/memory/sqlite_retrieval.py
def repository_to_retrieval_pack(
    repo: SQLiteKnowledgeRepository,
    *,
    current_address: str | None,
    current_function: str | None,
    active_mode: str,
    active_goal: str,
    budget: ContextBudget,
) -> RetrievalPack
```

The adapter reads repository records (already returned as `KnowledgeMemory` / `KnowledgeEntity` / `KnowledgeRelation` dataclasses) and feeds them into the existing ranker via a refactored entry point:

- `retrieve.build_retrieval_pack_from_records(memories, entities, relations, notes, ...)` — accepts dataclasses, ranks, returns `RetrievalPack`.
- `retrieve.build_retrieval_pack(store, paths, ...)` — thin wrapper that calls `store.list_*()` and delegates to the new entry point. Existing JSONL callers continue to work.

When `memory_service` is `None`, the section falls back to the existing `make_store` JSONL path.

The ranker itself is unchanged. The adapter is responsible only for sourcing data and constructing the same inputs the ranker already consumes.

## 10. Error handling

- Schema v3 migration failure rolls back the schema change and `user_version`, leaving the workspace at v2. The backup produced by `open_workspace_for_write` remains on disk.
- Auto-import failure leaves the marker unset. The next IDB open retries the import. Partial bundle imports are not possible because `import_workspace_bundle` validates the full envelope set before writing (stage-validate-write contract from the durability tranche).
- SQLite write failure in dual-write path logs the error and continues to the JSONL write. JSONL write failure logs the error and leaves the SQLite write intact. Neither re-raises into the agent loop.
- Knowledge panel refresh failure logs at debug level and falls back to the existing "Failed to load knowledge" placeholder. The panel never raises into the UI thread.
- Retrieved knowledge section failure returns an empty string. The agent turn continues without a Retrieved Knowledge block, matching today's behavior when `knowledge_enabled` is False or the store is empty.
- Auto-import is best-effort: if it raises, `_wire_central_memory` logs the error and continues without setting the marker. The next IDB open retries.
- A v3 workspace opened by older code is rejected with the existing `SchemaMigrationRequired` error, matching the durability tranche's downgrade contract.

## 11. Test strategy

Implementation follows test-driven development. New modules require failing tests first; existing modules require regression tests for every behavior change.

### 11.1 Schema v3 migration

- A v2 workspace upgrades to v3 with default-empty graph columns.
- Injected migration failure rolls back the schema change and `user_version`.
- `NOT NULL` constraints reject malformed inserts.
- `FactRecord`, `EntityRecord`, `RelationRecord` expose the new fields.
- `put_fact`, `put_entity`, `put_relation`, `save_fact_if_semantically_absent` round-trip the new fields.

### 11.2 JSONL → bundle adapter

- Adapter preserves every field the JSONL store carries.
- Empty store produces an empty envelope list.
- Temp bundle is a valid ZIP that passes `validate_manifest`.
- Adapter does not synthesize fields the source does not carry.

### 11.3 Auto-import

- Idempotent via the `legacy_jsonl_imported` marker.
- Imports records once; SQLite gains the records and the marker is set.
- Records with a matching semantic hash at the destination are skipped.
- JSONL files remain on disk after import.
- A failed import leaves the marker unset so the next open retries.
- Bundle import conflict (deterministic ID exists with different payload) raises `BundleImportConflictError` before any write.

### 11.4 Dual-write ingest

- Flag `True` writes both stores.
- Flag `False` writes only SQLite.
- SQLite failure does not block JSONL.
- JSONL failure does not block SQLite.
- `save_exploration_finding` persists `entity_refs` and `tags`.
- Research notes follow the same pattern.

### 11.5 Knowledge panel read

- Panel reads SQLite when `memory_service` is wired.
- Panel falls back to JSONL when `memory_service` is `None`.
- A `save_memory` tool call surfaces in the panel after the new refresh event.

### 11.6 Retrieved knowledge section

- The SQLite adapter returns the same `RetrievalPack` the JSONL ranker returns for equivalent inputs.
- The section uses SQLite when `memory_service` is wired.
- The section falls back to JSONL otherwise.

### 11.7 Integration

- A pre-durability user (JSONL only) upgrades, opens the IDB, and sees legacy data in the panel via SQLite.
- JSONL files are untouched after the upgrade.
- The next IDB open does not re-import (marker set).

### 11.8 Regression

- `tests/memory/test_workspace_migration_v2.py` remains green.
- `tests/memory/test_bundle_import.py` remains green.
- `tests/memory/test_repository.py` remains green.
- `tests/knowledge/test_ingest.py` remains green (JSONL path intact under dual-write).
- `tests/agent/test_exploration_loop.py` remains green.

## 12. Rollout order

1. Add schema v3 migration, extend record dataclasses, extend store write/read methods.
2. Add `SQLiteKnowledgeRepository.save_exploration_finding` and `BinaryMemoryService.save_exploration_finding`.
3. Add the JSONL → bundle adapter and temp bundle writer.
4. Add the auto-import trigger and wire it into `_wire_central_memory`.
5. Refactor `ingest_exploration_finding` and `ingest_research_note` for dual-write, behind the module flag.
6. Refactor the ranker into `build_retrieval_pack_from_records` and add the SQLite retrieval adapter.
7. Migrate `_refresh_knowledge_panel` to prefer SQLite.
8. Migrate `_build_retrieved_knowledge_section` to prefer SQLite.
9. Emit the panel-refresh event from `_handle_save_memory_tool` on `created` and `deduplicated` outcomes.
10. Run focused tests, dual-root full suite, and the regression suite.

## 13. Success criteria

The tranche is complete only when:

- the Knowledge panel shows `save_memory` tool output immediately after a successful save, with no manual refresh;
- the Knowledge panel shows legacy JSONL records on the first IDB open after upgrade, with JSONL files preserved on disk;
- exploration and research findings are present in both SQLite and JSONL while `_LEGACY_JSONL_DUAL_WRITE` is `True`;
- the system prompt's Retrieved Knowledge block reflects the same records the Knowledge panel shows;
- the schema is at v3 with graph metadata columns and a clean migration path from v2;
- all pre-existing tests remain green, including the durability tranche's tests;
- no unrelated subsystem is partially rewritten.

## 14. Follow-up specifications

After this tranche ships and one release cycle confirms SQLite reliability:

1. Flip `_LEGACY_JSONL_DUAL_WRITE` to `False` and remove the JSONL write helpers from the ingest path.
2. Delete `KnowledgeRawStore` and the `make_store` fallback once no production read path uses it.
3. Add the remaining graph metadata columns (`importance`, `verified`, `source_refs`, `relation_refs`) when a UI or retrieval consumer requires them.
4. Migrate the notes subsystem into SQLite when notes need full-text search or versioning.
5. Expose a user-facing re-import command for force-refreshing the SQLite store from JSONL.
