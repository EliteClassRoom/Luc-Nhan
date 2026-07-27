# Memory Durability and Orchestra Gate Design

**Date:** 2026-07-22  
**Status:** Draft for user review  
**Scope:** First hardening tranche after the AI architecture audit

## 1. Purpose

This tranche prevents silent memory loss, introduces deterministic exact-fact deduplication, temporarily disables the unsafe Orchestra execution path, and ensures both existing test roots protect merge and release workflows.

It intentionally does not combine the remaining hardening work. Effective tool policy, context compaction, prompt trust boundaries, raw-knowledge consolidation, provider parity, and the Orchestra rewrite require separate specifications.

## 2. Confirmed problems

### 2.1 Category-based memory overwrite

`SQLiteKnowledgeRepository.upsert_memory_fact()` currently finds the first fact whose `type` matches the requested category and replaces that fact's content. Categories such as `function_purpose`, `architecture`, and `data_structure` are taxonomy values, not record identities. Saving two independent facts in the same category therefore destroys the first fact's current value.

### 2.2 Unsafe Orchestra path

The current Orchestra implementation executes real tools directly through `ToolRegistry.execute()`, bypassing the standard agent loop's approval, mutation, validation, undo, and sanitization pipeline. Its delegation path also contains a broken relative import, and its one-turn structure does not feed child results into a synthesis turn.

The safe short-term action is to disable `/orchestra` until a shared execution-policy and context-isolation foundation exists.

### 2.3 Incomplete CI collection

GitHub CI, release CI, and `ci-local.sh` invoke pytest only on `tests/`. Existing regression tests under `rikugan/tests/` are not part of those gates.

## 3. Goals

1. Saving two different facts in one category preserves both facts.
2. Saving the same semantic fact again is idempotent and returns the existing fact identity.
3. Existing workspace records, IDs, revisions, timestamps, confidence values, and observations survive migration unchanged.
4. Migration does not merge or delete legacy duplicates.
5. Memory bundle import/export and `MEMORY.md` projection continue to preserve independent records.
6. `/orchestra` cannot call a provider, execute a tool, or start a child agent while disabled.
7. Both test roots run in local, merge, and release gates on Python 3.11 and 3.12.

## 4. Non-goals

This tranche does not:

- expose `correct_memory` or `forget_memory` to the model or UI;
- implement the shared `EffectiveToolPolicy`;
- rewrite context compaction;
- harden all prompt trust boundaries;
- merge legacy raw JSONL knowledge into central SQLite memory;
- rewrite or remove Orchestra internals;
- change provider adapters;
- add model-backed evaluation;
- recover facts already destroyed by previous category-based overwrites.

## 5. Memory identity model

### 5.1 Concepts

Each fact has three separate concepts:

| Field | Meaning |
|---|---|
| `fact_id` | Stable identity of one record |
| `fact_type` | Taxonomy/category such as `function_purpose` |
| `semantic_hash` | Exact semantic-dedup fingerprint for the current fact |

`FactRecord`, `WorkspaceStore.get_fact()`, and `WorkspaceStore.list_facts()` expose `semantic_hash`; it is not a write-only schema detail. `fact_type` must never be used as a unique key or as an implicit correction target.

### 5.2 Canonical representation

The semantic hash is SHA-256 over a length-delimited UTF-8 encoding:

```text
utf8_byte_length(canonical_category) + ":" + canonical_category + canonical_content
```

The decimal byte length disambiguates the category boundary without relying on a sentinel character that content could contain.

Canonical category processing:

1. require a string;
2. Unicode NFC normalization;
3. trim leading/trailing whitespace;
4. collapse internal whitespace runs to one ASCII space;
5. `casefold()`.

Canonical content processing:

1. require a string;
2. normalize CRLF and CR to LF;
3. Unicode NFC normalization;
4. trim leading/trailing whitespace from the whole value;
5. preserve internal whitespace, punctuation, line ordering, and case.

The normalizer does not sort lines, collapse internal content whitespace, strip punctuation, or attempt paraphrase similarity. Near-equivalent facts remain distinct.

Empty normalized category or content is rejected before storage.

### 5.3 Save contract

`save_memory(category, fact, source)` has only two persistence outcomes:

- **created:** no exact semantic match exists; create a new fact with a new `fact_id` and revision 1;
- **deduplicated:** an exact match exists; retain the existing `fact_id`, content, revision, confidence, and timestamps.

`SaveMemoryResult` gains `outcome: Literal["created", "deduplicated"]`; existing `record_id`, `revision`, `projection_dirty`, and `warning` fields remain. Projection success/failure is orthogonal to the persistence outcome.

Every explicit save appends an observation, including a deduplicated save. The observation payload records:

```json
{
  "fact_id": "fact-...",
  "category": "function_purpose",
  "semantic_hash": "...",
  "outcome": "created"
}
```

`outcome` is either `created` or `deduplicated`.

The tool response is compact and does not echo the full fact:

```text
Memory created: fact-abc123 [function_purpose]
```

or:

```text
Memory already exists: fact-abc123 [function_purpose]
```

### 5.4 Future correction contract

Correction is not implicit in save. A later tranche may add an explicit correction API that targets a stable `fact_id`, uses optimistic revision control, creates a new revision, and updates the current semantic hash without changing identity. This tranche does not implement or expose correction behavior.

## 6. Workspace schema v2

`MEMORY_WORKSPACE_SCHEMA_VERSION` increases from 1 to 2. The existing per-revision `fact_revisions.content_hash` remains unchanged; it hashes raw revision content for revision integrity. The new fact-level `semantic_hash` coexists with it and serves canonical exact-dedup lookup only.

Migration v2 is additive and avoids rebuilding the parent `facts` table while `fact_revisions` references it:

```sql
ALTER TABLE facts ADD COLUMN semantic_hash TEXT
    CHECK(semantic_hash IS NULL OR length(semantic_hash) = 64);

-- Backfill every row from its current fact_revisions content,
-- computing the canonical hash in Python.

CREATE INDEX idx_facts_semantic ON facts(semantic_hash);

CREATE TRIGGER facts_semantic_hash_insert_guard
BEFORE INSERT ON facts
WHEN NEW.semantic_hash IS NULL
  OR length(NEW.semantic_hash) != 64
  OR NEW.semantic_hash GLOB '*[^0-9a-f]*'
BEGIN
    SELECT RAISE(ABORT, 'semantic_hash must be a 64-character lowercase SHA-256 hex digest');
END;

CREATE TRIGGER facts_semantic_hash_update_guard
BEFORE UPDATE OF semantic_hash ON facts
WHEN NEW.semantic_hash IS NULL
  OR length(NEW.semantic_hash) != 64
  OR NEW.semantic_hash GLOB '*[^0-9a-f]*'
BEGIN
    SELECT RAISE(ABORT, 'semantic_hash must be a 64-character lowercase SHA-256 hex digest');
END;
```

The migration verifies that no row remains `NULL`, that every hash is exactly 64 lowercase hexadecimal characters, and that `PRAGMA foreign_key_check` is empty before committing. New v2 writes always supply a hash; both SQLite triggers and store validation reject null, wrong-length, non-hex, or uppercase values. `_migrate_v2` derives canonical hashes using a pure helper in a lower-level memory module that imports neither `WorkspaceStore` nor `repository`, avoiding a migration-time import cycle.

The index is deliberately non-unique because legacy workspaces may contain duplicate records. It indexes `semantic_hash` alone because the hash already includes the canonical category and legacy `fact_type` presentation may differ in case or whitespace. Hash lookup only selects candidates: the repository/store re-canonicalizes and compares category plus content before deduplicating, so even a SHA-256 collision cannot merge unequal facts. Migration must not merge, delete, rename, or revise facts.

### 6.1 Backfill

For every fact, migration reads its current revision, derives the canonical semantic hash, and updates `facts.semantic_hash`.

It preserves:

- `fact_id`;
- all revision rows and revision numbers;
- title;
- current content;
- confidence;
- creation timestamps;
- observations;
- entities and relations;
- projection state.

If several legacy facts derive the same semantic hash, all remain present.

After backfill, every v2 row must have a valid 64-character lowercase SHA-256 `semantic_hash`. New inserts and all existing update paths (`WorkspaceStore.put_fact()`, repository ID-based upsert used by bundle import, and case promotion) must compute and persist the canonical current hash. No v2 write may leave a null or malformed hash. ID-based updates are allowed for explicit import/promotion/correction semantics, but they must not perform category-based matching.

### 6.2 Deterministic legacy match

When a new save matches multiple legacy duplicate records, selection is deterministic:

1. oldest `created_at`;
2. lexical `fact_id` as tie-breaker.

No duplicate is automatically tombstoned or merged.

### 6.3 Atomic write primitive

Canonicalization and empty-input rejection happen in the repository before opening a write transaction. The store primitive receives canonical category/content/hash values and repeats basic non-empty/hash-shape validation as defense in depth.

Exact lookup, optional insert, and observation append must occur under the same `BEGIN IMMEDIATE` transaction. A Python-level sequence of `list_memories()`, conditional lookup, and separate insert is not sufficient because two processes can race.

`WorkspaceStore` owns an atomic primitive with an explicit contract equivalent to:

```python
save_fact_if_semantically_absent(
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
) -> tuple[FactRecord, Literal["created", "deduplicated"]]
```

The primitive calls the existing `begin_immediate_with_retry()` on the same connection. The semantic-hash lookup, canonical category/content equality check, deterministic legacy-match selection, insert when absent, and observation append all run while that `BEGIN IMMEDIATE` RESERVED lock is held. Cross-process serialization relies on this mandatory transaction invariant rather than a unique index, because a unique index would reject pre-existing legacy duplicates. No caller may perform the lookup outside this primitive and then call a separate insert.

The repository owns canonicalization, generated record IDs, and result mapping. The store owns transaction boundaries, deterministic lookup, insert, observation append, and conflict behavior.

## 7. Migration safety and rollback

### 7.1 Before rollout

`WorkspaceStore.open()` remains the automatic migration trigger through the existing `open_sqlite()` path; no separate upgrade command is introduced. Because `WorkspaceStore.open()` migrates immediately, backup orchestration must happen before that call.

A new controller-owned helper performs the production open sequence:

1. inspect `PRAGMA user_version` through a short-lived raw read-only SQLite connection that cannot run migrations;
2. if the version is 1, call an extended version of the existing SQLite backup facility before any v2 `WorkspaceStore.open()`;
3. store the backup under `<memory root>/backups/<owner_memory_id>/`, create the directory as needed, and return/report the backup path; backup filenames include nanosecond time or a collision-resistant suffix, are created with exclusive-destination semantics, and must never overwrite an earlier backup;
4. verify that the backup exists, is non-empty, has a readable SQLite header, matches the source owner's `workspace_meta.owner_memory_id`, reports `PRAGMA integrity_check = 'ok'`, and has the manifest hash returned by the backup helper;
5. only after backup verification succeeds, call `WorkspaceStore.open()` and allow automatic migration;
6. if backup or verification fails, abort the writable open and leave the v1 database untouched.

All writable production workspace-open call sites for binary and case workspaces must use this helper, including controller binding, case service/repository access, and the reopen performed after backup restore. Direct `WorkspaceStore.open(..., read_only=False)` remains a low-level/test primitive. Read-only peer opens continue to raise `SchemaMigrationRequired` and never migrate. Tests may use temporary copies instead of user-visible backups.

### 7.2 Failure behavior

Migration runs through the existing `open_sqlite()` migration transaction. Any failure:

- rolls back schema and data changes;
- leaves `PRAGMA user_version` at 1;
- reports an actionable error;
- does not regenerate `MEMORY.md` from a partially migrated database.

### 7.3 Downgrade behavior

Workspace downgrade is not performed in place. If code must be rolled back to a version that only supports schema v1, the operator restores the pre-migration backup by replacing the v2 workspace database with the verified v1 backup while no `WorkspaceStore` connection is open. The existing `restore_from_backup()` helper is not used unchanged for downgrade because it creates/opens the target with the current schema version and can trigger migration. This tranche adds a version-preserving offline rollback helper that validates the backup, atomically replaces the closed target database, preserves the backup's v1 `user_version`, and does not open it through v2 `WorkspaceStore`.

Older code must continue to reject a newer unsupported schema rather than opening it incorrectly.

## 8. Bundle compatibility

The memory bundle schema remains version 1 in this tranche.

`semantic_hash` is an internal derived field and is not required in the portable wire format. Export continues to preserve each current fact's `record_id`, type, title, content, and confidence. Import derives the semantic hash from category and content at the destination.

Import requirements:

1. every origin fact record maps to one destination fact identity;
2. different origin records in the same category remain independent;
3. import must not call category-based save semantics;
4. destination IDs use the existing `<kind>-<hex32>` shape; the hex component is the first 32 hex characters of SHA-256 over a length-delimited canonical tuple `(target_memory_id, import_id, record_type, origin_record_id)`, avoiding ambiguous string concatenation;
5. re-importing the same bundle into the same target no-ops existing matching destination IDs, returns `imported_count = 0`, and does not create revisions or increase fact, entity, relation, or observation counts;
6. a partially imported target resumes by importing only absent deterministic IDs; an existing deterministic ID whose payload conflicts with the bundle aborts the import instead of being overwritten;
7. importing the same bundle into another target produces target-scoped destination IDs;
8. relations use the same deterministic map for their entity endpoints;
9. imported records are sanitized by the existing import hardening boundary; prompt trust-boundary changes remain a later tranche.

The current importer only returns a stable `import_id` while allocating fresh record IDs on every invocation; that behavior does not meet this tranche's idempotency requirement and must be corrected explicitly.

## 9. `MEMORY.md` projection

The managed Markdown format does not change.

The projector renders one entry per `fact_id`. Multiple facts of the same category are preserved. This tranche retains the existing deterministic renderer order `(fact_type, title, fact_id)`; no `ManagedEntry` shape or Markdown ordering migration is introduced.

Legacy exact duplicates remain visible because migration does not silently modify user data. A future explicit deduplication tool may offer a preview and confirmation, but it is outside this tranche.

Projection occurs only after a successful migration or write transaction. Existing cross-process locking, conflict detection, bounded regular-file reads, and atomic replacement remain unchanged.

## 10. Orchestra temporary gate

### 10.1 Behavior

The `/orchestra` command remains recognized so users receive a precise message instead of an unknown-command error:

```text
Orchestra is temporarily disabled while its execution and context isolation contracts are being hardened.
```

The gate is checked in `AgentLoop.run()` immediately after direct-command handling and before skill resolution, persisted-mode resumption, user-message append, system-prompt construction, retrieval, or tool-schema construction. The gate yields one `TurnEvent.text_done()` response and returns.

While disabled, the command must not:

- append the command/request to `SessionState.messages`;
- resolve or inject a skill;
- resume or alter active-mode metadata;
- build a system prompt or tool schemas;
- run knowledge retrieval;
- construct `OrchestraMainAgent`;
- call any provider;
- execute any real or pseudo-tool;
- start a subagent.

A2A remains available because it is a distinct execution path.

### 10.2 Configuration

The gate constant lives in `rikugan.agent.loop`, next to the early command dispatch: `_ORCHESTRA_ENABLED = False`. This placement makes the no-prompt/no-retrieval/no-session-mutation contract enforceable. This tranche does not add a Settings control, persisted config field, or environment override that could present the unsafe path as supported.

Focused legacy tests may override the constant with `monkeypatch.setattr(rikugan.agent.loop, "_ORCHESTRA_ENABLED", True)` only inside the test process. Production defaults remain disabled until the Orchestra rewrite has its own approved design and test gate.

### 10.3 Documentation

User-facing and architecture documentation marks Orchestra as experimental and temporarily unavailable. Existing implementation is retained to avoid expanding this tranche into a deletion or rewrite project.

## 11. CI collection

All local, merge, and release test gates collect both roots:

```bash
python -m pytest tests/ rikugan/tests/ --tb=short -q
```

Before changing workflow files, the combined suite must pass with the same dependency set on:

- Python 3.11;
- Python 3.12.

Fixture or import conflicts are fixed before the workflow begins enforcing both roots.

`pyproject.toml` becomes the single source of truth for collection with `[tool.pytest.ini_options]` and `testpaths = ["tests", "rikugan/tests"]`. The rollout explicitly verifies cross-root `conftest.py` behavior, Qt module cleanup, import package names, and fixture compatibility before CI enforcement. `rikugan/tests` is upgraded to a package by adding an empty `__init__.py` unconditionally — the two roots already share a duplicate basename (`tests/test_ida_docs_review_prompt.py` and `rikugan/tests/test_ida_docs_review_prompt.py` both exist), and under pytest's default prepend import mode the bare-basename collision would shadow one of the two roots. The `__init__.py` forces package-qualified imports (`rikugan.tests.<name>`) and matches the existing mypy override at `pyproject.toml` (`module = ["rikugan.tests.test_settings_dialog_fixes"]`).

CI invokes pytest without a narrower positional root so the configured `testpaths` cannot be bypassed accidentally. A collection smoke assertion verifies that representative tests from each root are present. This tranche does not add a percentage coverage gate.

## 12. Error handling

- Invalid or empty normalized facts fail before opening a write transaction.
- Lock acquisition follows the existing bounded retry behavior.
- Atomic dedup conflict returns the deterministic existing record; it does not retry a partially executed transaction.
- Observation insertion is part of the same store-owned transaction as create/dedup resolution. A failed observation rolls back the save operation.
- Creating a brand-new workspace at schema v2 does not create a migration backup; only an existing v1 database entering a writable v2 open does.
- Migration errors identify the workspace; the pre-migration backup remains available for rollback. Backup/verification failure occurs before migration and leaves the original v1 database untouched.
- Orchestra gate returns a normal user-visible event, not a provider error.
- Combined test-root collection failures block merge/release once the workflows are updated.

## 13. Test strategy

Implementation follows test-driven development.

### 13.1 Repository and service tests

1. Two different facts in the same category both remain current.
2. An exact semantic duplicate returns the same `fact_id`.
3. CRLF/LF, whole-value boundary whitespace, category whitespace/case, and Unicode NFC normalize as specified.
4. Internal whitespace, punctuation, case, line order, and near-equivalent wording remain distinct.
5. Dedup preserves revision, confidence, content, and creation time.
6. Every save appends one observation with the correct outcome.
7. Two concurrent saves of the same semantic fact create no new duplicate record.
8. Two concurrent saves of different facts in one category preserve both.
9. A forced semantic-hash collision between unequal canonical inputs preserves both facts.
10. `SaveMemoryResult.outcome` and tool text distinguish `created` from `deduplicated` while retaining projection warning semantics.
11. Tool results are compact and do not echo long fact content.

### 13.2 Migration tests

1. A v1 workspace upgrades to v2.
2. Record count, IDs, revisions, revision rows, confidence, timestamps, and observations remain unchanged.
3. Legacy duplicate records are not merged.
4. Every migrated fact receives the expected semantic hash; no hash is `NULL`, uppercase, non-hex, or wrong-length.
5. `FactRecord`, `get_fact()`, and `list_facts()` expose the migrated hash.
6. Every v2 write path, including ID-based bundle import and case promotion, maintains the current semantic hash.
7. Insert/update guards reject missing or malformed hashes, and hash-collision fixtures do not deduplicate unequal canonical facts.
8. `PRAGMA foreign_key_check` remains empty after migration.
9. Injected migration failure rolls back both schema changes and `user_version`.
10. Production v1 open creates a uniquely named backup before the migration-triggering `WorkspaceStore.open()` call.
11. Backup verification checks owner identity, SQLite integrity, non-empty size, and the returned manifest hash.
12. Backup failure or verification failure aborts the writable open and leaves the v1 database unchanged.
13. All binary/case writable-open and post-restore reopen call sites use the backup-aware helper.
14. Version-preserving offline rollback restores the verified v1 backup without triggering v2 migration.
15. Read-only open of a v1 workspace reports migration required.
16. Older schema support rejects a v2 workspace.

### 13.3 Bundle and projection tests

1. Bundle round-trip preserves multiple facts in one category.
2. Import does not overwrite an earlier same-category fact.
3. Re-import of the same bundle into the same target returns `imported_count = 0`, creates no revisions/observations, and does not increase fact, entity, or relation counts.
4. Partial replay imports only absent deterministic records; a conflicting existing deterministic ID aborts without overwriting it.
5. Import into another target produces target-scoped IDs while preserving graph references.
6. Imported destination facts derive valid semantic hashes.
7. Projection renders every independent fact.
8. Projection retains the existing deterministic `(fact_type, title, fact_id)` order.
9. Existing managed/unmanaged Markdown preservation remains green.

### 13.4 Orchestra gate tests

1. `/orchestra` returns exactly one temporary-disable `TEXT_DONE` event and no turn/provider events.
2. No provider method is called.
3. No tool or child-agent factory is called.
4. Skill resolution, system-prompt construction, tool-schema construction, and knowledge retrieval are not called.
5. The `/orchestra` command/request is not appended to `SessionState.messages`, and active-mode metadata is unchanged.
6. `/a2a` remains unaffected.

### 13.5 CI tests

1. Both test roots collect under Python 3.11.
2. Both test roots collect under Python 3.12.
3. Local CI, merge CI, and release CI use the same root list.
4. A smoke assertion protects root discovery.

## 14. Rollout order

1. Add failing repository tests for same-category preservation and exact dedup.
2. Add schema-v2 migration, expose `semantic_hash` through store records, and cover every v2 write path.
3. Add the backup-aware writable-open helper and migrate all binary/case production open sites before enabling automatic v2 migration there.
4. Add the atomic store primitive and update repository/service behavior.
5. Make tool responses compact.
6. Correct bundle-import destination-ID idempotency and verify bundle/projection compatibility.
7. Add and enable the early Orchestra temporary gate.
8. Add pytest `testpaths` as the single collection source and run both roots on Python 3.11 and 3.12 with frozen dependencies.
9. Resolve cross-root `conftest.py`, Qt cleanup, import-name, and fixture conflicts; add `rikugan/tests/__init__.py` unconditionally (the duplicate-basename collision is already present in the repo, not hypothetical).
10. Add the representative-test collection assertion.
11. Update local, merge, and release CI to rely on configured `testpaths` without a narrower positional root.
12. Run lint, type checking, focused tests, both full test roots, and migration/backup rollback tests.

## 15. Success criteria

The tranche is complete only when:

- no save path updates a fact solely because its category matches;
- exact semantic duplicate saves are idempotent under concurrency;
- a v1-to-v2 migration preserves every legacy record and revision and is preceded by a verified production backup;
- every v2 current fact, including import and case-promotion writes, has a valid canonical semantic hash;
- bundle re-import is record-count idempotent and Markdown projection preserves independent same-category facts;
- `/orchestra` exits before session, prompt, retrieval, provider, tool, or child-agent side effects while gated;
- both test roots pass on supported Python versions and are enforced in merge/release CI;
- no unrelated hardening subsystem is partially rewritten in this tranche.

## 16. Follow-up specifications

After this tranche is complete, hardening proceeds through separate design cycles:

1. shared `EffectiveToolPolicy` for normal, pseudo-tool, subagent, and Orchestra paths;
2. protocol-safe compaction with archival/provider-context separation;
3. persistent-input trust and token-budget boundaries;
4. raw-knowledge consolidation into central memory;
5. provider semantic parity;
6. Orchestra multi-turn rewrite with scoped capabilities, cancellation, budgets, and structured child results.
