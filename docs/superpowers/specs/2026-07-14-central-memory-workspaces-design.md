# Central Memory Workspaces and Analysis Cases

**Date:** 2026-07-14  
**Status:** Approved design  
**Scope:** Persistent memory, structured knowledge, research notes, analysis cases, cross-binary retrieval, legacy migration, and headless identity

## 1. Summary

Luc Nhan currently derives `RIKUGAN.md`, `notes/`, and `.lucnhan-kb/` from the parent directory of the active IDB. Multiple IDBs in one directory consequently read and mutate the same persistent artifacts. Structured records carry a `binary_id`, but the physical store and retrieval path are shared, and deterministic IDs such as `func:0x401000` can overwrite records belonging to another binary.

This design replaces folder-scoped persistence with central, identity-aware workspaces under the Luc Nhan configuration directory. Each binary has an isolated workspace. Related binaries may be explicitly grouped into one or more analysis cases, each with its own shared memory and cross-binary relation graph. Cross-binary retrieval is controlled, cited, read-only, and bounded.

SQLite is the authoritative structured store. `MEMORY.md` is the human-readable narrative and managed projection. JSONL is used only for validated import/export. Legacy `RIKUGAN.md` and folder-level knowledge stores are never loaded automatically; users import them explicitly.

## 2. Goals

1. Isolate all persistent artifacts belonging to different IDBs, even when the IDBs share a directory.
2. Preserve a binary's memory across reopen, rename, and unambiguous move.
3. Detach memory when an IDB is copied or saved as a new database.
4. Persist raw-headless analysis by source-binary SHA-256 instead of temporary-IDB path.
5. Allow related binaries to share explicit case-level knowledge without merging binary-local state.
6. Retrieve relevant peer memory automatically only when the binaries share the active case and relevance exceeds a threshold.
7. Preserve provenance and binary namespace for every cross-binary fact, address, symbol, and relation.
8. Support concurrent IDA processes without lost structured updates.
9. Keep Markdown user-editable without making free-form Markdown the transactional database.
10. Migrate ambiguous legacy data only through an explicit preview-and-confirm flow.
11. Restrict persistent writes to the main agent and explicit UI actions.
12. Prevent an in-flight run from redirecting writes to a newly opened IDB.

## 3. Non-goals

1. Automatically infer case membership from a directory or similarity score.
2. Automatically merge every binary fact into case memory.
3. Mutate or open a peer IDB as a side effect of cross-memory retrieval.
4. Treat session transcripts as part of the memory workspace.
5. Provide transparent fallback reads from `RIKUGAN.md`.
6. Use JSONL as the live authoritative store.
7. Build a local memory daemon in the first release.
8. Automatically parse every free-form Markdown edit into structured facts.
9. Implement cloud synchronization in this project phase.

## 4. Current-state problems

### 4.1 Folder-scoped ownership

The current layout is effectively:

```text
<idb_dir>/
├── RIKUGAN.md
├── .lucnhan-kb/
└── notes/
    └── reports/
```

All IDBs in `<idb_dir>` use those paths. This affects:

- prompt memory loading;
- `save_memory`;
- `/memory`, `/knowledge`, and `/report`;
- structured memory/entity/relation storage;
- research notes and indexes;
- generated reports;
- the Knowledge panel.

### 4.2 Record-level identity is insufficient

Structured records carry `binary_id`, but `KnowledgeRawStore` reads and upserts shared physical files without partitioning by that field. IDs derived only from addresses or relation endpoints collide across binaries.

### 4.3 Distributed path construction

System-prompt building, pseudo-tool handlers, commands, research mode, reports, the Knowledge panel, and ingestion code derive paths independently. There is no canonical memory locator.

### 4.4 IDB identity ambiguity

`db_instance_id` persists in an IDA netnode and follows an IDB across reopen and move. Copying the IDB copies that UUID, however. A UUID-only identity merges copies; a path-only identity breaks moves.

### 4.5 Unsafe writer model

`RIKUGAN.md` uses an unlocked check-then-create followed by append. Parallel agents can truncate or lose facts. Process-local JSONL locks do not protect two IDA processes.

## 5. High-level architecture

The canonical memory root is `LucNhanConfig.memory_dir`, resolved as `<LucNhanConfig._config_dir>/memory`. UI, headless bootstrap, tests, import/export, and repositories receive this resolved path through configuration. They must not independently call host config-directory helpers or derive a path beside the IDB. If the root is unavailable, persistence enters a visible disabled/degraded state and never falls back to folder-level storage.

All path and identity decisions flow through three components:

```text
MemoryIdentityResolver
    │  identifies the binary from evidence
    ▼
MemoryRegistry
    │  binds identity evidence to stable workspace IDs
    ▼
MemoryLocator
       returns canonical binary/case workspace paths
```

Consumers receive an immutable `MemoryContext` or `MemoryPaths`. They must not derive memory roots using `dirname(idb_path)`.

### 5.1 Central layout

```text
<config_dir>/memory/
├── registry.db
├── binaries/
│   └── <memory_id>/
│       ├── memory.db
│       ├── MEMORY.md
│       └── notes/
│           └── reports/
├── cases/
│   └── <case_id>/
│       ├── memory.db
│       ├── MEMORY.md
│       └── notes/
│           └── reports/
└── exports/
```

Directory components are generated identifiers. User-provided names, paths, hashes, and netnode values are never joined directly into filesystem paths.

### 5.2 Immutable run binding

At the start of an agent run, Luc Nhan freezes:

```text
binary_memory_id
active_case_id
database_generation
case_binding_generation
```

`database_generation` is process-local and increments only when the active database or resolved binary workspace changes. `case_binding_generation` increments when the active case or membership relevant to the session changes. Record revisions remain separate SQLite optimistic-concurrency values; committing content does not invalidate the run. Every binary write validates `binary_memory_id` and `database_generation`. Every case write additionally validates `active_case_id` and `case_binding_generation`.

Database-open, close, reload, and Save As transitions increment the generation, cancel or invalidate old runs, clear memory/UI caches, resolve identity, and only then restore sessions or enable memory UI. Case switching and membership mutation are disabled while a persistence-capable run is active, or equivalently increment the binding generation so stale case writes are rejected. Database switching cannot cause an old run to resolve against the newly active IDB.

## 6. Binary identity model

### 6.1 Workspace identity versus evidence

`memory_id` is an internal random UUID and is the persistent workspace identity. The following values are evidence used to find or create it:

```json
{
  "db_instance_id": "netnode UUID",
  "filesystem_id": {"volume_or_device": "...", "file_or_inode": "..."},
  "canonical_path": "...",
  "source_sha256": "...",
  "display_name": "..."
}
```

- `db_instance_id` follows the IDB across reopen and move.
- `filesystem_id` distinguishes an existing file object from a copy on the same filesystem.
- `canonical_path` is an alias and fallback, not the primary identity.
- `source_sha256` identifies raw-binary headless inputs.
- `display_name` is UI metadata only.

On Windows, filesystem identity uses volume serial plus file index. On POSIX, it uses `st_dev` plus `st_ino`. If unavailable, the resolver applies the ambiguity policy rather than pretending certainty.

### 6.2 Ordered resolution rules

Identity resolution and evidence binding occur in one `registry.db` `BEGIN IMMEDIATE` transaction. The ordered decision table is:

1. Raw-headless mode: a validated source SHA-256 is authoritative in the separate raw-source evidence namespace.
2. IDB mode: filesystem identity plus a compatible or absent netnode UUID resolves directly.
3. Filesystem identity matching one workspace while UUID points to another is an identity conflict; never resolve silently.
4. UUID match plus a different filesystem identity while the prior file still exists is a copy.
5. UUID match plus a different filesystem identity while the prior file is unavailable is ambiguous.
6. Path-only equality never resolves an existing workspace.
7. Without durable evidence, use an ephemeral `Open without persistence` context rather than create orphan workspaces.

#### Reopen or same-filesystem move

A matching filesystem identity resolves to the same workspace. The registry updates the path alias and `last_seen_at`.

#### Copy or Save As

If `db_instance_id` matches an existing workspace but filesystem identity differs and the original still exists, the new IDB is treated as a copy:

1. create a new `memory_id`;
2. assign and attempt to persist a new IDB netnode UUID;
3. start with an empty binary workspace;
4. optionally suggest the source's cases without joining them automatically.

If netnode persistence fails because the IDB is read-only or unsaved, the workspace may be bound provisionally by durable filesystem identity and marked `uuid_write_pending`. Reopening that file follows the provisional workspace. If neither UUID nor filesystem identity is durable, use `Open without persistence` and surface the failure instead of creating an orphan workspace. Identity resolution must happen before the controller's current automatic UUID creation.

#### Same-path replacement

A different filesystem identity and incompatible UUID at the same path creates a new workspace. Path equality never reuses the old memory by itself.

#### Ambiguous cross-volume move

If the UUID matches but the former file no longer exists, the resolver cannot distinguish move from copy-then-delete:

- UI: ask `Link existing memory`, `Start fresh`, or `Open without persistence`;
- headless: default to `Start fresh` and emit a structured warning;
- headless linking requires an explicit option naming the intended workspace.

Linking transfers the workspace's current filesystem/path binding and retires the former binding; it does not keep two current bindings. If the retired file later reappears, it is treated as a copy or ambiguity and cannot silently rejoin.

#### Raw-binary headless input

Before launching IDA, the CLI streams the complete original input through SHA-256 and carries the full lowercase 64-hex digest through bootstrap. It rejects hashing failure or a file whose size/mtime changes while hashing. Raw-source identity is a separate evidence namespace and never automatically merges with a GUI-created IDB workspace. It must not fall back to the temporary IDB path. Identical raw content at another path resolves to the same raw-headless workspace; modified content resolves to a new workspace.

## 7. Registry model

`registry.db` contains routing metadata, not analysis facts. Its implementable core schema is:

```text
workspaces(memory_id PK, kind, state, display_name, created_at, last_seen_at)
identity_evidence(evidence_id PK, memory_id, kind, value, status, created_at, retired_at)
path_aliases(alias_id PK, memory_id, normalized_path, status, last_seen_at)
cases(case_id PK, name, state, revision, created_at, updated_at)
case_members(case_id, memory_id, status, created_at, PRIMARY KEY(case_id, memory_id))
legacy_sources(source_fingerprint PK, path_metadata, state, last_seen_at)
```

Current filesystem identities and raw-source hashes are unique in their evidence namespaces. Copied netnode UUID evidence is not globally unique by itself. Path aliases are non-authoritative many-to-one metadata. The registry uses `PRAGMA user_version`; migrations are transactional, and a newer unsupported schema is opened read-only or rejected without modification.

Case membership references `memory_id`, not path. A binary may belong to multiple cases. A session has at most one `active_case_id`; `No active case` is valid. Case deletion is a soft-delete tombstone in v1; physical deletion is deferred to cleanup/archive. Active sessions fall back to `No active case`. Removing a member never deletes binary facts; its case relations become inactive and are excluded from retrieval. New promotions or relations require all source binaries to be current case members.

Phase 1 bumps both session and manifest schemas. Session history remains in the existing checkpoint subsystem and stores `binary_memory_id` and `active_case_id` as bindings. Restore filters by resolved `memory_id`; it restores the active case only when the memory ID matches and membership remains current. Legacy path/UUID metadata is retained for display/compatibility but never creates or links a memory workspace automatically.

## 8. Binary and case ownership

### 8.1 Binary workspace

A binary workspace owns facts that are meaningful only in that database:

- functions, addresses, symbols, types, comments, and renames;
- decompilation-specific findings;
- imports, strings, data structures, and algorithms;
- binary research notes and reports.

`memory_id` is the sole trust-bearing binary namespace. Structured records use `owner_memory_id`, which equals the owning workspace's `memory_id`, even though the physical database is already isolated. Legacy `binary_id` values are provenance metadata only and cannot select a workspace.

### 8.2 Case workspace

A case workspace owns cross-binary conclusions:

- roles of binaries in a system or campaign;
- execution and loading chains;
- shared protocols and configurations;
- family/version attribution;
- promoted artifacts and relations.

Binary facts do not automatically become case facts. Promotion is explicit and creates a new case record with source references.

### 8.3 Case membership

Users create/select cases and add or remove binaries explicitly. Luc Nhan may suggest membership based on:

- shared directory;
- embedded payload hashes;
- import/export matches;
- rare shared strings, domains, mutexes, or keys;
- binary similarity;
- discovered relations.

Suggestions never modify membership automatically.

## 9. Cross-binary relation model

Version 1 supports five **case-level binary-to-binary** relation types; existing binary-local predicates such as calls, uses-import, and references-string remain unchanged:

1. `embeds_or_loads`: directed loader → loaded binary;
2. `communicates_with`: symmetric;
3. `derived_from`: directed derivative → source;
4. `same_family_as`: symmetric;
5. `shares_artifact_with`: symmetric and requires an artifact/source reference.

Symmetric endpoints are stored in sorted `memory_id` order and self-relations are rejected. Relations include confidence, provenance, and source records. They reference binary `memory_id` values, not paths.

Bare cross-binary addresses are invalid. Every address-bearing source includes:

```text
memory_id
binary display name
address
source record ID
```

## 10. Retrieval model

When analyzing a binary, prompt memory is assembled in four layers:

1. active binary memory — always available;
2. active case memory — available when an active case exists;
3. automatic peer retrieval — only above a relevance threshold;
4. explicit case/binary search — user or agent requested.

### 10.1 Automatic peer retrieval

Peer candidates must belong to the active case. A peer is automatically eligible only when it has either:

- a direct current case relation with confidence at least `0.7`; or
- an exact strong-artifact match.

Strong artifacts are exact content or embedded-payload hashes, import/export pairs, public keys, protocol constants, and configuration markers. Local lexical/current-goal relevance, record confidence, and freshness rank eligible peers but cannot independently make a peer eligible. V1 performs no remote embedding/provider call for retrieval. Ties are ordered by score, then `memory_id`.

Automatic peer context may include only current structured facts, entities needed for citation, and relation summaries. It excludes raw pseudocode, unmanaged Markdown, notes, reports, observations, superseded records, and source-drifted promotions. Defaults:

- at most three peer binaries per turn;
- at most five records per peer;
- deduplicate against active binary and case context;
- if no peer is eligible, return its allocation to active-binary context.

V1 membership suggestions likewise use only already-stored exact signals: directory metadata, hashes, import/export pairs, rare exact strings/configuration markers, and existing case relations. Suggestions trigger no IDA scan, provider call, remote embedding, or new binary-similarity computation.

### 10.2 Context character budget

The 55/30/15 percentages apply to `knowledge_max_context_chars` (default 12,000 characters) unless a future exact tokenizer replaces character accounting:

| Layer | Allocation |
|---|---:|
| Active binary | 55% |
| Active case | 30% |
| Peer binaries | 15% |

If there is no active case, or case/peer context is disabled or empty, unused characters return to active-binary context. Unmanaged `MEMORY.md` content counts within its binary/case layer. `knowledge_enabled` disables all layers; new `case_memory_enabled` and `peer_retrieval_enabled` settings independently gate case and automatic peer content.

### 10.3 Namespace and citation

Peer databases open in SQLite read-only/query-only mode and retrieval never creates a missing database. Peer content is truncated before wrapping so valid closing tags are retained. Generated IDs are validated, all XML attributes are escaped, and `display_name` is treated as untrusted text:

```xml
<peer_memory
  memory_id="..."
  display_name="payload.dll"
  case_id="..."
  source_record_id="..."
>
...
</peer_memory>
```

The system prompt states that peer addresses, symbols, types, and function names belong to the named workspace and must not be applied to the active binary without mapping evidence. UI results include a shortened memory ID to distinguish duplicate names:

```text
[payload.dll · mem-42c3 · 0x180013A20 · record-456]
```

Cross-memory retrieval is read-only. It does not open or mutate peer IDBs. Explicit peer search and comparison also require an active case and current membership; unrelated workspaces are not enumerated or searched by default.

## 11. Authoritative persistence

### 11.1 SQLite roles and core schema

- `registry.db`: identities, path aliases, cases, membership, and legacy-source inventory/dismissal state.
- `binaries/<memory_id>/memory.db`: binary facts, entities, local relations, observations, sources, note index, projection state, promotions, and authoritative import receipts.
- `cases/<case_id>/memory.db`: case facts, cross-binary relations, provenance, promotions, note index, projection state, and authoritative import receipts.

Free-form note/report files are authoritative for their document bodies; SQLite is authoritative for their index, owner/provenance, revision/hash, and relations. Both binary and case workspaces include `notes/reports/`. Writes use the workspace lock, atomic replacement, expected-hash conflict detection, and collision-safe filename allocation. On open, index reconciliation detects files written before a crash or stale DB entries.

The workspace schema includes, at minimum:

```text
facts(fact_id PK, owner_memory_id, current_revision, state, type, title, created_at, updated_at)
fact_revisions(fact_id, revision, content, content_hash, confidence, created_at,
               PRIMARY KEY(fact_id, revision))
entities(entity_id PK, owner_memory_id, type, canonical_name, state, revision, ...)
entity_aliases(entity_id, alias, ...)
relations(relation_id PK, owner_memory_id, subject_id, predicate, object_id,
          state, revision, confidence, ...)
observations(observation_id PK, owner_memory_id, kind, payload, created_at)
sources(source_id PK, owner_memory_id, source_kind, address, artifact, metadata, ...)
note_index(note_id PK, relative_path, content_hash, revision, state, ...)
projection_state(singleton PK, managed_hash, unmanaged_hash, projection_dirty,
                 projection_conflict, projected_revision, ...)
promotions(promotion_id PK, case_id, source_memory_id, source_record_id,
           source_revision, source_hash, state, ...)
import_receipts(import_id PK, source_fingerprint, target_scope, manifest_hash,
                imported_at, mapping_json, ...)
```

Case source keys are composite `(source_memory_id, source_record_id, source_revision)` because record IDs are not globally unique. Integer expected revisions, not timestamps alone, enforce optimistic concurrency.

SQLite is preferred over runtime JSONL because case writes can originate from multiple IDA processes and may need to commit facts, relations, sources, and observations atomically.

Each database uses:

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
```

The memory root must be on a local filesystem supporting SQLite locking and atomic replacement. If WAL/locking cannot be enabled reliably, Luc Nhan enters a visible single-writer/degraded mode or rejects persistent writes rather than claiming multiprocess safety. Writes are short `BEGIN IMMEDIATE` transactions with bounded retry. No provider or IDA API call occurs while a transaction is open. Databases use `PRAGMA user_version`; transactional migrations reject or open read-only any newer unsupported schema.

### 11.2 `MEMORY.md`

`MEMORY.md` replaces `RIKUGAN.md` as the only canonical Markdown filename in binary and case workspaces. It is a human-readable narrative plus managed projection:

```markdown
# Memory

<!-- lucnhan:managed:start -->
## Confirmed Facts

- [protocol] Uses RC4 for C2 traffic.

## Current Understanding

Generated summary of structured memory.
<!-- lucnhan:managed:end -->

## User Notes

Free-form user content.
```

Luc Nhan regenerates only the managed region. Projection is deterministic and local: it invokes no provider, embedding service, MCP server, or IDA API. Any LLM-generated summary is a separate explicit main-agent write. Every managed entry carries a hidden stable record ID and revision marker so reordering and duplicate text can be reconciled safely. Content outside the managed region is preserved. Manual unmanaged edits are sanitized and immediately readable by the prompt loader, but they affect structured SQLite data only through explicit Sync/Import with a preview.

Prompt assembly reads authoritative managed facts from SQLite only and reads only content outside the managed region from `MEMORY.md`, labeling it `manual_notes`. Unsynced edits inside the managed region are previewable but do not enter prompt context as authoritative facts. Duplicate, missing, reversed, or nested managed delimiters set `projection_conflict` and prohibit automatic regeneration.

If the managed region was edited concurrently, Luc Nhan reports a projection conflict and does not overwrite it.

### 11.3 Projection flow

After a successful SQLite commit:

1. acquire the workspace's portable cross-process lock exclusively with a bounded timeout;
2. re-read the latest committed SQLite state after acquiring the lock;
3. read `MEMORY.md` and hash the managed and unmanaged regions separately with SHA-256;
4. preserve the latest unmanaged region;
5. render the managed region from current committed state;
6. verify the unmanaged/file hash is unchanged;
7. write a temporary file in the same directory, reserving output space for valid closing delimiters;
8. flush/fsync when supported;
9. re-check containment and reject symlinks/reparse points/non-regular targets;
10. atomically replace `MEMORY.md`;
11. update projection hash and dirty status in SQLite;
12. release the lock.

The implementation adds one portable file-locking runtime dependency to both `pyproject.toml` and `ida-plugin.json`. The same protocol protects note/report writes that may collide. Lock timeout or SQLite-commit-success/Markdown-failure leaves the fact saved, marks `projection_dirty`, and reports that Markdown is stale; it never rolls back committed structured data.

### 11.4 Explicit Markdown sync

External edits set an `Unsynced edits` state. Sync presents:

- candidate new facts;
- changed managed entries;
- free-form note sections;
- conflicts.

Rules:

- adding a structured fact requires confirmation;
- changing a fact creates a new revision and supersedes the old one;
- deleting Markdown text does not delete a structured fact automatically;
- free-form sections import as notes unless explicitly mapped;
- imported items use provenance `manual_markdown_sync`.

### 11.5 JSONL interchange

JSONL is not the live store. Validated JSONL bundles support export, backup, transfer, and import. A bundle contains a manifest, record streams, `MEMORY.md`, and optional notes. Imports enforce archive containment, symlink rejection, record/line/size limits, schema validation, and provenance preservation.

## 12. Write and promotion flows

### 12.1 `save_memory`

Only the main agent may commit persistent facts:

1. use the run-bound binary workspace;
2. sanitize and validate the fact/category;
3. transactionally insert/upsert fact, source, and observation;
4. commit;
5. project the managed Markdown region;
6. emit success only after the SQLite commit.

If projection fails, return success-with-warning rather than false failure or rollback.

### 12.2 Case promotion

Promotion occurs only through an explicit UI action, `/case promote`, or a main-agent promotion call made in response to an explicit user request. It is never a side effect of saving or retrieving. It requires an active case and current source membership, and is idempotent by `(case_id, source_memory_id, source_record_id, source_revision, promotion_kind)`.

Promotion creates a distinct case fact with one or more binary sources. It does not move or mutate the binary fact. A promotion stores source memory ID, record ID, revision, and content hash. Because binary and case facts live in separate databases, source drift is evaluated lazily on case open/retrieval by comparing this tuple with the source database. The result may be cached but is not transactionally propagated. Missing or changed sources are excluded from automatic peer injection until reviewed while remaining visible to explicit search.

### 12.3 Main-agent-only persistence

Every persistent repository method requires a non-serializable write-authority object supplied out-of-band by the main controller or explicit UI handler. Actor identity is never accepted from LLM arguments. Subagents and Bulk Renamer receive no write authority; direct and indirect persistence or auto-ingestion is rejected or converted into candidate events. Exploration/research children return candidate facts to the parent. Subagents may receive a read-only snapshot scoped to the active binary/case.

## 13. Legacy migration

Authoritative import receipts live in the target `memory.db` and commit in the same transaction as imported records. `registry.db` stores only legacy-source inventory and dismissal state. Optional human-readable receipt exports belong under `exports/` and are never authoritative.

Legacy sources are:

```text
<idb_dir>/RIKUGAN.md
<idb_dir>/.lucnhan-kb/
<idb_dir>/notes/
```

Runtime never reads them automatically and never falls back to them when `MEMORY.md` is missing.

### 13.1 UI flow

When legacy sources are detected, UI offers:

```text
Legacy Luc Nhan memory detected
[Inspect] [Import…] [Dismiss]
```

Import flow:

1. choose current binary or active case as target;
2. inventory Markdown sections, legacy JSONL groups, notes, and reports;
3. preview ownership, provenance, and conflicts;
4. select records/files;
5. confirm;
6. import into the central workspace;
7. write a migration receipt.

The importer:

- never deletes or moves source data;
- is idempotent by source hash and target;
- assigns provenance `legacy_import`;
- previews and assigns each legacy `binary_id` group separately;
- requires address-bearing or binary-local records to map to an existing/new binary workspace before any explicit case promotion;
- leaves unknown groups staged until assignment;
- records dismissal per source fingerprint, not globally;
- builds one deterministic old-ID → new-ID map and rewrites every relation, entity reference, promotion source, and receipt through it;
- validates the complete graph before imported records, mapping, and receipt commit atomically;
- generates new IDs on collision instead of overwriting;
- validates all imported paths and content.

Headless detects and reports legacy data but does not import without an explicit manifest/command.

### 13.2 JSONL bundle contract

Portable interchange is a ZIP bundle streamed without extraction to arbitrary paths:

```text
manifest.json
records/*.jsonl
MEMORY.md
notes/**
```

`manifest.json` declares schema version, scope, hashes, counts, and export mode. Export reads a coherent SQLite snapshot. Import supports explicit `merge` and `restore-as-new` modes; the selected target controls routing, never an imported `memory_id`. Remapped records retain `origin_memory_id` as provenance. Default hard limits are 100 MiB compressed, 500 MiB uncompressed, 100,000 records, 1 MiB per JSONL line, and 10,000 files. The importer rejects path traversal, absolute paths, duplicate ZIP names, symlinks, special files, hash/count mismatches, and unsupported schemas.

## 14. Error handling and recovery

### 14.1 Ambiguous identity

UI requires a decision. Headless starts fresh unless explicitly linked. No nearest-name or same-folder heuristic silently selects a workspace.

### 14.2 SQLite busy

Use bounded retry. If the database remains busy, do not claim success. Preserve the candidate in session state for retry and report the affected workspace.

### 14.3 SQLite corruption, registry loss, or missing workspace

Open existing SQLite files with `mode=rw` so missing databases are never silently recreated. If only `memory.db` is corrupt but the registered workspace directory exists, enter read-only degraded mode and expose its `MEMORY.md` and notes. If the directory itself is missing, report it unavailable. If `registry.db` is corrupt or missing, recover only from explicit backups or a user-reviewed scan of immutable owner IDs stored inside workspace databases; never auto-rebind identity evidence. Run integrity checks in recovery flow, not every turn.

### 14.4 Projection conflict

Never overwrite a changed Markdown file. Offer review, explicit sync, managed-region regeneration, or keeping the file unchanged.

### 14.5 Source drift

Promoted case facts whose sources change are marked and ranked with reduced confidence until reviewed.

## 15. Security boundaries

1. Treat every memory source as untrusted: Markdown, SQLite records, JSONL, legacy data, case data, peer data, and notes.
2. Sanitize all content before prompt insertion and wrap it in scope-specific tags.
3. Parameterize all SQL and keep extension loading disabled.
4. Do not open SQLite databases supplied directly by an untrusted import archive; import through validated interchange records.
5. Generated UUIDs are the only filesystem directory identifiers.
6. Create central workspace directories owner-only where supported; reject symlinks/reparse points and non-regular files for databases, Markdown, locks, notes, and reports.
7. Validate note slugs and containment immediately before each write/atomic replace; cap facts, notes, reports, and projection sizes.
8. Reject archive traversal, absolute paths, special files, and symlinks.
9. Do not include canonical user paths in prompts or default exports.
10. Peer memory cannot assert that it belongs to the active binary; scope comes from registry metadata, not content.
11. A run bound to an old workspace cannot write after database switch.
12. Case retrieval and case promotion do not confer IDB mutation authority.

## 16. UI and command behavior

Required UI concepts:

- current binary workspace identity/status;
- case selector with `No active case`;
- case create/rename/delete and membership management;
- non-mutating membership suggestions;
- legacy import banner and preview;
- Markdown unsynced/projection-dirty/conflict indicators;
- cited peer retrieval results;
- explicit fact promotion;
- identity ambiguity dialog.

Command contract:

```text
/memory
/memory search-case <query>
/memory search-binary <memory_id> <query>
/memory sync
/memory export-jsonl <output-bundle>
/memory import-jsonl <input-bundle> --mode merge|restore-as-new
/case create <display-name>
/case use <case_id>
/case add-binary <memory_id>
/case compare <memory_id-a> <memory_id-b>
/case promote <source-record-id>
```

Generated IDs are canonical command selectors. Display names are accepted only when they resolve uniquely; otherwise commands return a non-mutating ambiguity error listing IDs. Destructive actions require explicit confirmation. Import/export paths pass canonical containment and regular-file checks. UI and headless implementations return structured error kinds for unavailable persistence, ambiguity, missing membership, busy storage, conflicts, approval required, and invalid bundles. Peer search/compare targets must belong to the active case.

## 17. Testing strategy

### 17.1 Identity

- reopen resolves to the same workspace;
- rename and same-filesystem move follow memory;
- copy and Save As create a new workspace and UUID;
- same-path replacement starts fresh;
- ambiguous cross-volume move prompts in UI and starts fresh headlessly;
- raw inputs resolve by SHA-256;
- malformed identity values cannot escape the central root;
- full ordered evidence-conflict matrix on Windows/POSIX;
- simultaneous first-open resolution from two processes;
- read-only/unsaved IDBs and netnode UUID-write failure;
- retired binding reappearance and provisional binding reopen;
- registry schema upgrade and newer-schema read-only/rejection.

### 17.2 Isolation

- two IDBs in one directory use different DBs, Markdown, notes, and reports;
- equal addresses cannot overwrite another binary's entity;
- commands, prompt building, reports, retrieval, and Knowledge panel use the resolved workspace;
- database switch clears stale UI/cache bindings;
- same-path replacement does not use the old store.

### 17.3 Cases and retrieval

- manual membership and suggestion-only behavior;
- one binary in multiple cases;
- one active case per session;
- no active case means no peer retrieval;
- promotion preserves provenance;
- source revision marks case facts;
- peer addresses always include binary namespace;
- threshold, deduplication, per-peer caps, and token budget;
- no cross-retrieval from inactive cases;
- case deletion/member removal and binding-generation rejection during a run;
- source-drift lazy evaluation and exclusion from automatic injection;
- peer DBs open read-only/query-only;
- escaped citation attributes and retained wrapper closing tags under truncation.

### 17.4 Concurrency

Use two real processes, not only threads:

- concurrent binary writes;
- two binaries promoting into one case;
- writer crash inside a transaction;
- busy timeout and retry;
- concurrent Markdown projections;
- manual edit racing with projection;
- no lost update and no partial Markdown;
- central-root permission failure, unsupported WAL/locking filesystem, and lock timeout;
- malformed/duplicate/nested managed markers and deterministic projection after concurrent commits;
- registry corruption and missing workspace recovery behavior.

### 17.5 Migration and interchange

- detect all legacy paths without auto-loading them;
- manual import to binary and case;
- idempotence by source hash;
- ID collision does not overwrite;
- free-form Markdown preservation;
- source files remain untouched;
- headless requires explicit import;
- malicious/truncated/oversized JSONL and traversal archives are rejected;
- export/import round trip preserves provenance and relations.

### 17.6 Security

- role markers and closing tags are neutralized in every source;
- peer content cannot forge active scope;
- SQL injection strings remain data;
- subagents lack write capability;
- an old run cannot write after database switch;
- SQLite extension loading is disabled.

### 17.7 Compatibility

- maintain the configured prompt line/token cap for Markdown;
- manual edits become visible by mtime/hash;
- user sections survive projection;
- imported note trees preserve relative links among selected files; preview reports external/unselected links as potentially unresolved, and only explicitly selected validated attachments are copied;
- docs and pseudo-tool output refer to `MEMORY.md`, never runtime `RIKUGAN.md`.

## 18. Rollout plan

### Phase 1 — Dark scaffolding: identity and binary storage

Implement registry, resolver, locator, central paths, per-binary SQLite, `MEMORY.md`, run-bound contexts, session/manifest schema updates, and copy/move detection behind a disabled feature boundary. This phase is not user-enabled or shipped independently.

### Phase 2 — Atomic binary-memory activation

Cut over every reader/writer together: system-prompt loading, `save_memory`, `/memory`, approved-plan persistence, exploration/research guidance, pseudo-tool descriptions, structured retrieval, auto-ingestion, research notes, reports, Knowledge UI, tests, and documentation. Retire `KnowledgeRawStore` as a runtime backend; JSONL code remains only for validated interchange. Include legacy detection plus explicit import before activation. There is no dual write and no transparent fallback.

Exit criteria for the first enabled release:

- two IDBs in one directory share no binary analysis records, Markdown, notes, reports, or workspace DB;
- multiprocess writes and projection conflicts pass integration tests;
- legacy sources can be explicitly inspected/imported without automatic loading.

### Phase 3 — Analysis cases

Add case CRUD, soft deletion, membership, active-case generation, case SQLite, explicit promotion, five relation types, source-drift evaluation, cited controlled peer retrieval, and character budgets.

### Phase 4 — Interchange and expanded migration tooling

Add validated JSONL ZIP export/import, migration receipts, graph remapping, additional preview/recovery UX, and attachment-aware note migration. Do not add automatic fallback.

### Phase 5 — Operational hardening

Add extended multiprocess stress tests, registry/workspace recovery, indexes/performance tuning, cleanup/archive controls, documentation, and optional backup workflows. Correctness-critical lock/conflict handling is already required before Phase 2 activation and is not deferred here.

## 19. Success criteria

The feature is complete when:

1. IDBs in one directory share no binary analysis records, Markdown, notes, reports, or workspace databases unless explicitly related through a case; `registry.db` contains shared routing and membership metadata only.
2. Rename/move preserves binary memory while copy/Save As detaches it.
3. Raw-binary headless memory survives temporary-IDB lifecycle through source hash identity.
4. Related binaries cross-read only through the active case and controlled retrieval.
5. Every peer fact has binary namespace and provenance.
6. Concurrent processes do not lose structured updates.
7. `MEMORY.md` preserves user content and does not serve as a transactional database.
8. SQLite is authoritative and JSONL is interchange-only.
9. `RIKUGAN.md` is absent from runtime read/write behavior.
10. Legacy data is imported explicitly and sources remain untouched.
11. Subagents cannot write persistent memory directly.
12. Runs cannot redirect writes across a database switch.
13. Case promotions preserve sources and surface source drift.
14. All consumers use the central resolver/locator rather than deriving paths independently.
