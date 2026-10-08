# Memory Interchange and Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hoàn thiện full legacy migration, validated JSONL ZIP interchange, backup/recovery, storage hardening, multiprocess stress/performance gates và operational tooling cho central binary/case memory.

**Architecture:** `MemoryBundleExporter/Importer` stream một versioned ZIP contract qua validated entries, stage toàn graph và ID remap trước atomic SQLite commit. `MemoryRecoveryService` chỉ mở existing DB bằng `mode=rw`, phục hồi registry/workspace từ explicit backup hoặc user-reviewed scan; `StorageGuard` tập trung path/permission/symlink/size/local-filesystem checks. Stress suites chạy real processes và failure injection trên registry/workspace/case/projection/import paths.

**Tech Stack:** Python 3.11–3.12 stdlib `zipfile`, `json`, `sqlite3`, SHA-256, portalocker, pytest/multiprocessing, existing central workspace/case services.

## Global Constraints

- Prerequisites: foundation, atomic cutover, and analysis-case plans complete.
- Bundle format: ZIP containing `manifest.json`, `records/*.jsonl`, `MEMORY.md`, optional `notes/**`.
- Limits: 100 MiB compressed, 500 MiB uncompressed, 100,000 records, 1 MiB/JSONL line, 10,000 files.
- Stream ZIP members; không extract vào arbitrary filesystem path.
- Reject absolute/traversal/backslash-confusion/duplicate names, encrypted members, symlink/special-file metadata, unsupported compression/schema, hash/count mismatch.
- Export uses coherent SQLite read transaction and stable file hashes.
- Import target do user chọn; imported `memory_id` không route storage. Preserve `origin_memory_id` provenance.
- Modes: `merge` and `restore-as-new`; both stage/validate full graph before commit.
- One deterministic old-ID→new-ID map rewrites every reference; records+mapping+receipt commit atomically.
- Legacy identity groups assigned individually; address-bearing records không import thẳng vào case.
- Recovery never auto-creates missing existing DB or auto-rebinds identities.
- Memory root phải local filesystem với WAL/locking/atomic replace; unsupported storage rejects writes/degrades visibly.
- Central files owner-only where supported; no symlink/reparse/non-regular targets.
- Canonical user paths không vào prompt/default exports.
- Testing includes real processes, crash/failure injection, performance ceilings and both test roots.

**Spec reference:** `docs/superpowers/specs/2026-07-14-central-memory-workspaces-design.md`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `lucnhan/memory/bundle_schema.py` | Create | Manifest/record envelope/schema limits and validation |
| `lucnhan/memory/bundle_export.py` | Create | Coherent binary/case ZIP export |
| `lucnhan/memory/bundle_import.py` | Create | Streaming validation, staging, ID remap, merge/restore commit |
| `lucnhan/memory/legacy.py` | Modify | Full group assignment, case-target migration, attachments/link preview |
| `lucnhan/memory/storage_guard.py` | Create | Local FS, containment, permissions, symlink, size and regular-file checks |
| `lucnhan/memory/recovery.py` | Create | Backup, corrupt DB degraded mode, registry reviewed recovery |
| `lucnhan/memory/backup.py` | Create | SQLite backup API and workspace backup manifests |
| `lucnhan/memory/sqlite_backend.py` | Modify | Read-only/newer-schema/local-WAL diagnostics and failure injection seam |
| `lucnhan/memory/markdown.py` | Modify | Harden replace/lock/recovery and size caps |
| `lucnhan/memory/notes.py` | Modify | Attachment-aware selected migration and unresolved-link report |
| `lucnhan/memory/service.py` | Modify | Export/import/recovery façade and structured status |
| `lucnhan/agent/loop_commands.py` | Modify | Final import/export/recovery command handlers |
| `lucnhan/ui/knowledge_panel.py`, `lucnhan/ui/panel_core.py` | Modify | Bundle/migration/recovery preview and status UI |
| `lucnhan/cli/headless.py` | Modify | Explicit import/export/link flags and structured exits |
| `scripts/validate_memory_bundle.py` | Create | Offline deterministic bundle validator |
| `tests/memory/interchange/*.py` | Create | Schema/export/import/migration/security tests |
| `tests/memory/recovery/*.py` | Create | Backup/corruption/registry/storage tests |
| `tests/memory/stress/*.py` | Create | Multiprocess/projection/performance stress tests |
| `.github/workflows/ci.yml`, `.github/workflows/release.yml` | Modify | Memory security/stress gates and artifact checks |
| `ci-local.sh`, `ci-local.ps1` | Modify | Matching local gates |
| Documentation | Modify | Export/import/recovery/portability/operator guide |

---

### Task 1: Central storage guard

**Files:**
- Create: `lucnhan/memory/storage_guard.py`
- Create: `tests/memory/recovery/test_storage_guard.py`
- Modify: `lucnhan/memory/sqlite_backend.py`
- Modify: `lucnhan/memory/markdown.py`
- Modify: `lucnhan/memory/notes.py`

**Interfaces:**
- Produces: `StoragePolicy`, `StorageUnavailable`, `validate_memory_root()`, `validate_regular_contained_path()`, `ensure_private_directory()`, `bounded_file_size()`.
- Consumed by: bundle/recovery/service and every central write.

- [ ] **Step 1: Write failing path/symlink/permission tests**

Test normal contained file, `..`, absolute path, symlink/reparse target, directory used as file, FIFO on POSIX, oversized file, and memory root where WAL cannot be enabled (inject backend capability result). Assert writes reject before file creation/replacement.

- [ ] **Step 2: Run and verify missing guard**

Run: `uv run python -m pytest tests/memory/recovery/test_storage_guard.py -v`

Expected: FAIL.

- [ ] **Step 3: Implement canonical containment and regular-file policy**

```python
@dataclass(frozen=True)
class StoragePolicy:
    max_fact_bytes: int = 64 * 1024
    max_note_bytes: int = 4 * 1024 * 1024
    max_report_bytes: int = 16 * 1024 * 1024
    max_markdown_bytes: int = 16 * 1024 * 1024


def validate_regular_contained_path(root: Path, candidate: Path, *, allow_missing: bool) -> Path:
    root_real = root.resolve(strict=True)
    parent_real = candidate.parent.resolve(strict=True)
    resolved = parent_real / candidate.name
    if os.path.commonpath((str(root_real), str(resolved))) != str(root_real):
        raise StorageUnavailable("path escapes memory workspace")
    if candidate.exists() or candidate.is_symlink():
        stat = candidate.lstat()
        if stat.S_ISLNK(stat.st_mode) or not stat.S_ISREG(stat.st_mode):
            raise StorageUnavailable("target is not a regular file")
    elif not allow_missing:
        raise FileNotFoundError(candidate)
    return resolved
```

On Windows, reject reparse points using `st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT` when available.

- [ ] **Step 4: Implement owner-only directories**

Create directories with mode `0o700`, files/temp/locks with `0o600` where supported. On Windows, do not claim ACL guarantees; log capability status and still reject reparse/non-regular paths.

- [ ] **Step 5: Integrate guard before DB/file writes**

`open_sqlite()` validates root/local-WAL mode; projector/note/report validate immediately before atomic replace. Unsupported WAL enters explicit `StorageUnavailable` rather than silent rollback journal multiprocess mode.

- [ ] **Step 6: Run tests and commit**

Run: `uv run python -m pytest tests/memory/recovery/test_storage_guard.py tests/memory/test_sqlite_backend.py tests/memory/test_markdown.py -v`

Expected: PASS.

```bash
git add lucnhan/memory/storage_guard.py lucnhan/memory/sqlite_backend.py lucnhan/memory/markdown.py lucnhan/memory/notes.py tests/memory/recovery/test_storage_guard.py
git commit -m "security(memory): harden central storage paths"
```

---

### Task 2: Versioned bundle schema and offline validator

**Files:**
- Create: `lucnhan/memory/bundle_schema.py`
- Create: `scripts/validate_memory_bundle.py`
- Create: `tests/memory/interchange/test_bundle_schema.py`
- Create: `tests/scripts/test_validate_memory_bundle.py`
- Create: `tests/fixtures/memory/valid-v1.zip`
- Create: `tests/fixtures/memory/generate_valid_v1.py`

**Interfaces:**
- Produces: `MemoryBundleManifest`, `BundleRecordEnvelope`, `BundleLimits`, `validate_member_name()`, `validate_manifest()`, `iter_validated_jsonl()`.
- Consumed by: Tasks 3–4.

- [ ] **Step 1: Write failing manifest/member tests**

Test exact schema version, scope `binary|case`, export mode `portable|diagnostic`, record counts/hashes, canonical member names, duplicates, backslash traversal, `../`, absolute/drive paths, NUL, encrypted ZIP, symlink external attrs, unsupported compression, line >1 MiB, total limit overflow.

- [ ] **Step 2: Run and verify missing module/script**

Run: `uv run python -m pytest tests/memory/interchange/test_bundle_schema.py tests/scripts/test_validate_memory_bundle.py -v`

Expected: FAIL.

- [ ] **Step 3: Implement frozen schema contracts and exact limits**

```python
MEMORY_BUNDLE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ManifestFile:
    name: str
    sha256: str
    uncompressed_size: int
    record_count: int = 0


@dataclass(frozen=True)
class BundleLimits:
    max_compressed_bytes: int = 100 * 1024 * 1024
    max_uncompressed_bytes: int = 500 * 1024 * 1024
    max_records: int = 100_000
    max_jsonl_line_bytes: int = 1024 * 1024
    max_files: int = 10_000


@dataclass(frozen=True)
class MemoryBundleManifest:
    schema_version: int
    scope: Literal["binary", "case"]
    export_mode: Literal["portable", "diagnostic"]
    origin_memory_id: str
    exported_at: str
    files: tuple[ManifestFile, ...]
    record_counts: Mapping[str, int]


@dataclass(frozen=True)
class BundleRecordEnvelope:
    record_type: str
    record_id: str
    origin_memory_id: str
    payload: Mapping[str, object]
```

`origin_memory_id` is provenance only. `portable` is the normal path-redacted export; `diagnostic` is explicit opt-in metadata export and still applies secret/path policy. `validate_manifest()` also requires a complete manifest/file bijection, validates SHA-256 as 64 lowercase hex, rejects duplicate record IDs per record type, sums `record_count` across members, and rejects unsupported/unknown top-level fields rather than ignoring them.

- [ ] **Step 4: Implement ZIP metadata validation before content reads**

Reject duplicate normalized names and any `ZipInfo.flag_bits & 0x1`, symlink mode, directory where a file is expected, unknown record file, member count/size overflow. Permit `ZIP_STORED`/`ZIP_DEFLATED` only.

- [ ] **Step 5: Implement streaming JSONL validator**

Read member line-by-line through `TextIOWrapper`; bound raw bytes/line before JSON parse; validate envelope `{record_type, record_id, origin_memory_id, payload}`; update SHA-256/count and compare manifest.

- [ ] **Step 6: Implement offline script and deterministic valid fixture**

`python scripts/validate_memory_bundle.py bundle.zip` prints deterministic summary and exits 0; invalid bundle prints one sanitized error and exits 1. It imports host-agnostic bundle code only. `generate_valid_v1.py` creates `valid-v1.zip` with fixed timestamps/member order/content; the test regenerates to a temp path and asserts byte equality with the committed fixture so CI input cannot drift silently.

- [ ] **Step 7: Run tests and commit**

Run: `uv run python -m pytest tests/memory/interchange/test_bundle_schema.py tests/scripts/test_validate_memory_bundle.py -v`

Expected: PASS.

```bash
git add lucnhan/memory/bundle_schema.py scripts/validate_memory_bundle.py tests/memory/interchange/test_bundle_schema.py tests/scripts/test_validate_memory_bundle.py tests/fixtures/memory/valid-v1.zip tests/fixtures/memory/generate_valid_v1.py
git commit -m "feat(memory): define portable bundle contract"
```

---

### Task 3: Coherent JSONL ZIP exporter

**Files:**
- Create: `lucnhan/memory/bundle_export.py`
- Create: `tests/memory/interchange/test_bundle_export.py`
- Modify: `lucnhan/memory/service.py`

**Interfaces:**
- Consumes: bundle schema, workspace/case repositories, notes, projector state.
- Produces: `MemoryBundleExporter.export_binary()`, `export_case()` returning `BundleExportResult`.

- [ ] **Step 1: Write failing deterministic export test**

Seed facts/entities/relations/observations/promotions, unmanaged Markdown and notes. Export twice without changes and assert same member names/order/content hashes (manifest timestamp may differ but deterministic record members). Assert no canonical source paths, lock/temp/SQLite files, managed hidden markers, or API keys in bundle.

- [ ] **Step 2: Write failing concurrent snapshot test**

Pause exporter only after `BEGIN` plus an immediate anchoring read (for example `SELECT owner_workspace_id FROM workspace_meta`) has established the SQLite snapshot, commit a fact concurrently, then resume. Assert export is the coherent pre-commit graph, never a head/reference mix. Note/report hashes correspond to bytes exported under workspace lock.

- [ ] **Step 3: Implement record serialization**

Export each logical table to sorted `records/<type>.jsonl`. Every line wraps payload with `origin_memory_id`; source references retain revision/hash. Exclude retired internal routing evidence/canonical paths unless user explicitly selects metadata, and still redact paths by default.

- [ ] **Step 4: Implement coherent snapshot and ZIP writing**

Acquire the workspace document lock first, then `BEGIN` a SQLite deferred read transaction and immediately execute one anchoring read before exposing any test hook or reading records. Keep that lock/snapshot pair until record and document bytes/hashes are staged, avoiding lock-order inversion with writers. Write to a temp ZIP in the validated target directory then atomic replace after validation. Build `manifest.json` last in memory but place it at a known first archive entry. Apply limits while writing.

- [ ] **Step 5: Add service API and authority**

Export is explicit user/UI/command action. It requires read access but not memory write authority; destination path passes storage guard and existing file replacement requires confirmation.

- [ ] **Step 6: Run tests and commit**

Run: `uv run python -m pytest tests/memory/interchange/test_bundle_export.py -v`

Expected: PASS.

```bash
git add lucnhan/memory/bundle_export.py lucnhan/memory/service.py tests/memory/interchange/test_bundle_export.py
git commit -m "feat(memory): export portable memory bundles"
```

---

### Task 4: Staged importer with graph-wide ID remap

**Files:**
- Create: `lucnhan/memory/bundle_import.py`
- Create: `tests/memory/interchange/test_bundle_import.py`
- Modify: `lucnhan/memory/service.py`
- Modify: `lucnhan/memory/workspace_store.py`

**Interfaces:**
- Produces: `BundleImportMode`, `ImportPreview`, `ImportIdMap`, `MemoryBundleImporter.preview()`, `commit()`.
- Consumed by: commands/UI.

- [ ] **Step 1: Write failing collision/remap test**

Target already contains `fact-11111111111111111111111111111111`; bundle contains the same generated ID, plus entity, relation and promotion source pointing to it. Preview produces a deterministic new generated ID; commit rewrites every reference; receipt stores mapping; rerun same manifest/target is idempotent.

- [ ] **Step 2: Write failing atomic failure tests**

Inject invalid relation after staging and DB failure before receipt. Assert target DB unchanged, no installed notes/Markdown, no receipt. Inject crashes after graph commit, after first file install, and before activation; assert receipt/graph remain coherent, document rows stay pending until reconciliation, and no active note hash points to a missing file. Test imported `memory_id` cannot choose target. Test merge vs restore-as-new target semantics.

- [ ] **Step 3: Implement streaming staging without extraction**

Use temporary SQLite staging DB under validated central temp directory, not in-memory unbounded lists. Parse validated members into staging tables; copy selected notes to staged regular files. Build deterministic mapping by `(target_id_exists, origin_id, bundle_manifest_hash)`.

- [ ] **Step 4: Validate complete graph**

Ensure all entity/relation/source/promotion refs resolve after mapping, case relation members map to selected/current workspaces according to mode, revisions/hash types valid, record limits respected. Unknown record types fail closed.

- [ ] **Step 5: Commit records/mapping/receipt atomically**

For one target DB, one `BEGIN IMMEDIATE` inserts all graph records, deterministic ID mappings, pending document rows and the authoritative receipt. Install staged document bodies under the workspace lock, then use a second short transaction to activate only hashes whose files exist. A crash/failure leaves recoverable `pending` rows and returns committed-with-warning; startup reconciliation activates matching files or removes/quarantines abandoned rows. The atomic guarantee is the SQLite graph/mapping/receipt, not cross-resource all-or-nothing. No active DB row claims a note hash whose file is unavailable.

- [ ] **Step 6: Preserve origin provenance**

Every imported record stores `origin_memory_id`, manifest hash and original record ID; routing always uses selected target ID. Imported `MEMORY.md` managed block is not trusted as facts; structured records drive managed projection and unmanaged content imports as manual note after preview.

- [ ] **Step 7: Run tests and commit**

Run: `uv run python -m pytest tests/memory/interchange/test_bundle_import.py -v`

Expected: PASS.

```bash
git add lucnhan/memory/bundle_import.py lucnhan/memory/service.py lucnhan/memory/workspace_store.py tests/memory/interchange/test_bundle_import.py
git commit -m "feat(memory): import remapped memory bundles"
```

---

### Task 5: Full legacy migration to binary or case targets

**Files:**
- Modify: `lucnhan/memory/legacy.py`
- Modify: `lucnhan/memory/notes.py`
- Create: `tests/memory/interchange/test_legacy_full.py`
- Modify: `tests/memory/test_legacy.py`

**Interfaces:**
- Extends: legacy inventory with per-group assignments, case promotion staging, selected attachments/link report.
- Consumed by: migration UI/commands.

- [ ] **Step 1: Write failing multi-group ownership tests**

Legacy JSONL contains two `binary_id` groups and an unknown group; Markdown has bare address; notes link selected/unselected attachment. Assert preview requires group-by-group mapping, unknown remains staged, address-bearing record cannot map directly to case, and source fingerprint dismissal is per source.

- [ ] **Step 2: Write failing attachment/link tests**

Select two notes and one contained attachment. Assert internal relative links preserved/rewritten, selected attachment copied, outside/unselected links remain text and appear in unresolved report, symlink attachment rejected.

- [ ] **Step 3: Implement assignment model**

```python
@dataclass(frozen=True)
class LegacyGroupAssignment:
    legacy_binary_id: str
    target_memory_id: str


@dataclass(frozen=True)
class LegacyMigrationSelection:
    source_fingerprint: str
    group_assignments: tuple[LegacyGroupAssignment, ...]
    selected_markdown_item_ids: tuple[str, ...]
    selected_note_paths: tuple[str, ...]
    selected_attachment_paths: tuple[str, ...]
    target_case_id: str = ""
```

Case import accepts only case-safe narrative/shared facts or explicit promotions whose source binary assignment exists.

- [ ] **Step 4: Use bundle importer staging/remap machinery**

Translate selected legacy records into a synthetic v1 bundle/staging graph with provenance `legacy_import`. Reuse graph validation/id remap/atomic receipt instead of a second importer.

- [ ] **Step 5: Keep sources untouched and idempotent**

Never delete/move/chmod legacy sources. Same fingerprint+target+selection hash returns prior receipt. Changed source creates a new preview/receipt, not mutation of old receipt.

- [ ] **Step 6: Run tests and commit**

Run: `uv run python -m pytest tests/memory/interchange/test_legacy_full.py tests/memory/test_legacy.py -v`

Expected: PASS.

```bash
git add lucnhan/memory/legacy.py lucnhan/memory/notes.py tests/memory/interchange/test_legacy_full.py tests/memory/test_legacy.py
git commit -m "feat(memory): migrate legacy multi-binary stores"
```

---

### Task 6: Backup and workspace recovery

**Files:**
- Create: `lucnhan/memory/backup.py`
- Create: `lucnhan/memory/recovery.py`
- Create: `tests/memory/recovery/test_backup.py`
- Create: `tests/memory/recovery/test_workspace_recovery.py`
- Modify: `lucnhan/memory/service.py`

**Interfaces:**
- Produces: `MemoryBackupService.create_backup()`, `list_backups()`, `restore_as_new()`.
- Produces: `MemoryRecoveryService.inspect_workspace()`, `open_degraded()`, `scan_workspace_owner_ids()`, `build_registry_recovery_preview()`, `apply_reviewed_binding()`.

- [ ] **Step 1: Write failing SQLite backup consistency test**

Concurrent writer runs while backup uses `Connection.backup()`. Assert backup integrity/user_version/owner ID and coherent records. Backup manifest hashes `memory.db`, `MEMORY.md`, selected notes without paths.

- [ ] **Step 2: Write failing corruption/missing behavior tests**

Cases:

- missing `memory.db` → never recreated, workspace unavailable;
- corrupt `memory.db` + existing `MEMORY.md`/notes → read-only degraded documents;
- missing workspace directory → unavailable;
- corrupt registry → no auto-rebind;
- newer schema → read-only/rejected;
- explicit reviewed owner scan builds preview only.

- [ ] **Step 3: Implement backup through SQLite backup API**

Acquire workspace lock for document snapshot, use `sqlite3.Connection.backup()` into private backup directory, validate integrity, write manifest atomically. `restore_as_new` creates a new generated workspace/case and imports validated backup/bundle; never overwrite live workspace in place.

- [ ] **Step 4: Implement degraded recovery states**

```python
class RecoveryState(str, Enum):
    HEALTHY = "healthy"
    DATABASE_CORRUPT = "database_corrupt"
    DATABASE_MISSING = "database_missing"
    WORKSPACE_MISSING = "workspace_missing"
    REGISTRY_UNAVAILABLE = "registry_unavailable"
    SCHEMA_NEWER = "schema_newer"
```

Open existing DB with URI `mode=rw`; use `PRAGMA integrity_check` only in inspect/recovery. Degraded document reads still sanitize/size-bound content.

- [ ] **Step 5: Implement reviewed registry recovery**

Workspace DB stores immutable owner ID/kind. Scan generated directories without following links, collect owner IDs and metadata, present preview. Only explicit user selection calls `apply_reviewed_binding`; it cannot infer path/netnode association automatically.

- [ ] **Step 6: Run tests and commit**

Run: `uv run python -m pytest tests/memory/recovery/test_backup.py tests/memory/recovery/test_workspace_recovery.py -v`

Expected: PASS.

```bash
git add lucnhan/memory/backup.py lucnhan/memory/recovery.py lucnhan/memory/service.py tests/memory/recovery/test_backup.py tests/memory/recovery/test_workspace_recovery.py
git commit -m "feat(memory): back up and recover workspaces"
```

---

### Task 7: Import/export/recovery commands and UI

**Files:**
- Modify: `lucnhan/agent/loop_commands.py`
- Modify: `lucnhan/ui/knowledge_panel.py`
- Modify: `lucnhan/ui/panel_core.py`
- Modify: `lucnhan/cli/headless.py`
- Create: `tests/agent/test_memory_interchange_commands.py`
- Create: `tests/ui/test_memory_recovery_ui.py`
- Create: `tests/cli/test_memory_interchange_cli.py`

**Interfaces:**
- Consumes: exporter/importer/legacy/recovery/backup service APIs.
- Produces explicit preview/confirmation operations and structured headless exits.

- [ ] **Step 1: Write failing command/CLI contract tests**

Cover:

```text
/memory export-jsonl <output-bundle>
/memory import-jsonl <input-bundle> --mode merge|restore-as-new
/memory import-legacy <source-fingerprint>
/memory backup
/memory recovery-status
```

Headless flags:

```text
--memory-export <path>
--memory-import <path>
--memory-import-mode merge|restore-as-new
--memory-link-workspace <memory_id>
```

Assert invalid path/bundle/ambiguous identity returns structured nonzero result; import requires preview manifest hash + confirmation token, not one-step model call.

- [ ] **Step 2: Write failing UI preview/recovery tests**

Preview displays scope, counts, IDs to remap, unresolved links, size, hashes and warnings. Confirm action includes preview hash to prevent TOCTOU; changed bundle invalidates confirmation. Recovery cannot offer auto-bind.

- [ ] **Step 3: Implement command handlers**

Model-visible command parsing may request preview; only user command/UI confirmation performs import. Export destination overwrite requires approval. Return sanitized errors without canonical source paths by default.

- [ ] **Step 4: Implement UI state machine**

States: `idle → inspecting → preview_ready → confirming → importing → completed|failed`. Recompute bundle hash immediately before commit. Recovery panel exposes documents read-only and reviewed binding choices.

- [ ] **Step 5: Implement headless explicit operations**

CLI validates files outside IDA when possible, passes manifest hash/operation through bootstrap, and exits with documented codes: 0 success, 2 invalid input, 7 interactive approval required, 8 storage unavailable, 9 import/recovery conflict. Never prompt one-shot stdin unexpectedly.

- [ ] **Step 6: Run tests and commit**

Run: `uv run python -m pytest tests/agent/test_memory_interchange_commands.py tests/ui/test_memory_recovery_ui.py tests/cli/test_memory_interchange_cli.py -v`

Expected: PASS.

```bash
git add lucnhan/agent/loop_commands.py lucnhan/ui/knowledge_panel.py lucnhan/ui/panel_core.py lucnhan/cli/headless.py tests/agent/test_memory_interchange_commands.py tests/ui/test_memory_recovery_ui.py tests/cli/test_memory_interchange_cli.py
git commit -m "feat(memory): expose interchange and recovery flows"
```

---

### Task 8: Multiprocess crash and concurrency stress suite

**Files:**
- Create: `tests/memory/stress/_workers.py`
- Create: `tests/memory/stress/test_registry_stress.py`
- Create: `tests/memory/stress/test_workspace_stress.py`
- Create: `tests/memory/stress/test_case_stress.py`
- Create: `tests/memory/stress/test_projection_stress.py`
- Create: `tests/memory/stress/test_import_stress.py`

**Interfaces:**
- Exercises all multiprocess guarantees; produces no production API.

- [ ] **Step 1: Implement spawn-safe worker helpers**

Top-level worker functions receive only strings/primitives, open their own registry/store connections, and report via `multiprocessing.Queue`. Use `multiprocessing.get_context("spawn")` on every platform. Provide deterministic worker modes such as `write_fact`, `write_promotion`, and `crash_after_begin`; for crash injection, the worker calls `os._exit(73)` only after the test hook confirms `BEGIN IMMEDIATE` and an uncommitted insert, so the parent can assert SQLite rollback after the process exits.

- [ ] **Step 2: Add registry first-open/copy race stress**

Launch 8 processes × 25 attempts resolving one raw hash and copied UUID/filesystem pairs. Assert one raw workspace, unique copies, no current durable evidence duplicates, valid integrity.

- [ ] **Step 3: Add workspace fact/revision crash stress**

Launch concurrent fact/entity/relation writers; kill one process after `BEGIN IMMEDIATE` before commit using injectable hook. Assert rollback, no partial graph, all successful revisions present, no lost updates.

- [ ] **Step 4: Add case promotion/relation stress**

Multiple binary processes promote 500 unique facts plus duplicate tuples into one case. Assert idempotent duplicates, exact unique count, valid sources and WAL integrity.

- [ ] **Step 5: Add projection/manual edit race stress**

Run projector processes while another process edits unmanaged region using lock/expected hash. Assert one managed region, all committed facts, preserved successful user edits, conflicts explicitly marked, no temp remnants.

- [ ] **Step 6: Add import/export race stress**

Export during writes, import same bundle concurrently twice, inject crash before receipt. Assert coherent export, one receipt/idempotent graph, rollback on pre-receipt crash.

- [ ] **Step 7: Run stress suite repeatedly**

Run on Windows PowerShell:

```powershell
1..3 | ForEach-Object { uv run python -m pytest tests/memory/stress -v; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } }
```

Run on POSIX:

```bash
for run in 1 2 3; do uv run python -m pytest tests/memory/stress -v || exit $?; done
```

Expected: all three passes; no intermittent lock/integrity failures.

- [ ] **Step 8: Commit**

```bash
git add tests/memory/stress
git commit -m "test(memory): stress multiprocess persistence"
```

---

### Task 9: Performance indexes and bounded-work gates

**Files:**
- Modify: `lucnhan/memory/workspace_store.py`
- Modify: `lucnhan/memory/case_repository.py`
- Modify: `lucnhan/memory/peer_retrieval.py`
- Modify: `lucnhan/memory/bundle_import.py`
- Create: `tests/memory/stress/test_performance_bounds.py`

**Interfaces:**
- Adds query indexes and measurable workload ceilings without behavior change.

- [ ] **Step 1: Write query-plan/index tests**

Seed 50k facts/entities/relations and use `EXPLAIN QUERY PLAN` to assert current-state/type/source/artifact/member queries use named indexes, not full table scans on hot paths.

- [ ] **Step 2: Write bounded-work tests**

Assert peer retrieval reads max eligible peers/records, Markdown parser caps file size before allocation, bundle importer streams max line/records, notes reconciliation caps files, UI list calls use pagination/limits.

- [ ] **Step 3: Add exact indexes**

Add migrations/indexes:

```sql
CREATE INDEX ix_facts_state_type ON facts(state, type);
CREATE INDEX ix_fact_revisions_hash ON fact_revisions(content_hash);
CREATE INDEX ix_sources_artifact ON sources(artifact);
CREATE INDEX ix_relations_predicate_endpoints ON relations(predicate, subject_id, object_id, state);
CREATE INDEX ix_promotions_source ON promotions(source_memory_id, source_record_id, source_revision);
CREATE INDEX ix_note_index_state ON note_index(state, relative_path);
```

Case registry already indexes membership; add relation confidence/state index.

- [ ] **Step 4: Add practical benchmark ceilings**

On local CI, 50k-record exact lookup/retrieval must complete under a generous 2 seconds per operation and remain under configured result caps. Mark tests deterministic and skip timing assertion only on explicitly detected debug/coverage mode; still assert bounded query/result counts.

- [ ] **Step 5: Run tests and commit**

Run: `uv run python -m pytest tests/memory/stress/test_performance_bounds.py -v`

Expected: PASS.

```bash
git add lucnhan/memory/workspace_store.py lucnhan/memory/case_repository.py lucnhan/memory/peer_retrieval.py lucnhan/memory/bundle_import.py tests/memory/stress/test_performance_bounds.py
git commit -m "perf(memory): index workspace retrieval paths"
```

---

### Task 10: CI/release gates and operator documentation

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/release.yml`
- Modify: `ci-local.sh`
- Modify: `ci-local.ps1`
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `ARCHITECTURE.md`
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`
- Modify: `DEVELOPMENT.md`
- Modify: `llms.txt`
- Create: `docs/MEMORY_MIGRATION.md`
- Create: `docs/MEMORY_RECOVERY.md`

**Interfaces:**
- Produces reproducible release/security/operator contract.

- [ ] **Step 1: Add CI bundle/security/stress jobs**

CI matrix 3.11/3.12 runs all tests. Add a dedicated memory-security job that runs:

```bash
python -m pytest tests/memory/interchange tests/memory/recovery -v
python -m pytest tests/memory/stress -q
python scripts/validate_memory_bundle.py tests/fixtures/memory/valid-v1.zip
```

Release verification runs the same schema validator and ensures plugin archive contains runtime portalocker dependency metadata but no user memory DB/Markdown/bundle artifacts.

- [ ] **Step 2: Add coverage non-regression gate**

Configure pytest coverage for `lucnhan/memory` with an initial measured baseline committed after implementation and `fail_under` equal to that baseline; require ≥80% changed-code coverage in PR tooling or a documented diff-cover command. Do not claim global 80% until achieved.

- [ ] **Step 3: Add lock/dependency consistency checks**

CI runs `uv lock --check` and a test comparing `pyproject.toml` runtime dependencies with `ida-plugin.json`. Update `uv.lock` once and keep clean checkout immutable under `--locked` commands.

- [ ] **Step 4: Write migration/operator docs**

`MEMORY_MIGRATION.md` documents explicit inventory, group assignment, binary/case targets, unresolved links, source preservation, no fallback. `MEMORY_RECOVERY.md` documents backups, degraded states, reviewed registry recovery, bundle validation and exit/error codes.

- [ ] **Step 5: Update architecture/security invariants**

Document central IDs/evidence, SQLite authoritative model, `MEMORY.md`, JSONL ZIP only, cases/peer retrieval, main-agent authority, local-FS requirement, limits, backup/recovery and no legacy auto-read.

- [ ] **Step 6: Run complete local gate**

Run: `uv lock --check`

Run: `uv run python -m pytest tests/ lucnhan/tests/ -q`

Run: `uvx ruff format --check lucnhan/ tests/ scripts/`

Run: `uvx ruff check lucnhan/ tests/ scripts/`

Run: `uvx mypy lucnhan/core lucnhan/providers --pretty`

Run: `./ci-local.ps1` on Windows or `./ci-local.sh` on POSIX.

Expected: PASS and clean tracked tree except planned docs/code.

- [ ] **Step 7: Commit**

```bash
git add .github/workflows/ci.yml .github/workflows/release.yml ci-local.sh ci-local.ps1 pyproject.toml uv.lock README.md ARCHITECTURE.md AGENTS.md CLAUDE.md DEVELOPMENT.md llms.txt docs/MEMORY_MIGRATION.md docs/MEMORY_RECOVERY.md
git commit -m "docs(memory): document interchange and recovery"
```

---

### Task 11: Final end-to-end release rehearsal

**Files:**
- Modify: `tests/memory/interchange/test_bundle_import.py`
- Modify: `tests/memory/recovery/test_workspace_recovery.py`
- Modify: `tests/memory/stress/test_performance_bounds.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Final verification only.

- [ ] **Step 1: Rehearse binary project lifecycle**

Automated scenario:

1. create/open IDB workspace;
2. save fact/note/report;
3. rename/move and retain workspace;
4. copy and detach;
5. create case/add both;
6. promote relation/fact;
7. export binary and case bundles;
8. import restore-as-new;
9. compare provenance/data/counts;
10. corrupt source DB and recover documents/backup.

Assert no original paths in prompt/default bundle.

- [ ] **Step 2: Rehearse raw-headless lifecycle**

Same raw bytes at two paths resolve one raw workspace, modified bytes detach, import/export works without temp-IDB identity, malformed bootstrap hash fails closed.

- [ ] **Step 3: Rehearse hostile inputs**

Run fixtures for ZIP bomb metadata, traversal, duplicate names, symlink, oversized line, malformed JSON, graph dangling refs, prompt-injection Markdown/record/display names and SQL-looking strings. Assert bounded rejection/sanitized storage, no filesystem escape.

- [ ] **Step 4: Build and validate release archive**

Run project release build command documented in `DEVELOPMENT.md`, then `python scripts/validate_archive.py <zip>` and HCLI lint when available. Inspect archive list: no `memory.db`, `registry.db`, `MEMORY.md`, `.workspace.lock`, backup or bundle files.

- [ ] **Step 5: Run final clean-checkout gates**

Run all CI/local commands from Task 10. Run `git diff --check` and `git status --short`. Expected only intended source/docs/test changes before commit.

- [ ] **Step 6: Update changelog and commit**

Document central memory, case grouping, migration/interchange, copy/move behavior and recovery. Do not bump release version unless a separate release task requests it.

```bash
git add tests/memory CHANGELOG.md
git commit -m "test(memory): rehearse durable memory lifecycle"
```

---

## Interchange and Hardening Exit Checklist

- [ ] Storage guard rejects escape/symlink/reparse/non-regular/oversized/unsupported FS writes.
- [ ] Bundle validator enforces exact v1 contract and fixed limits while streaming.
- [ ] Export is coherent, deterministic and path-redacted.
- [ ] Import stages/validates/remaps whole graph and commits atomically/idempotently.
- [ ] Full legacy migration assigns groups individually and preserves sources/links policy.
- [ ] Backup/recovery never silently recreate or auto-rebind missing/corrupt stores.
- [ ] UI/commands/headless require explicit preview/confirmation and structured errors.
- [ ] Multiprocess crash/race suites pass repeatedly.
- [ ] Hot queries use indexes and bounded result/work policies.
- [ ] CI/release run both test roots, lock check, memory security/stress, bundle validation and coverage baseline.
- [ ] Docs fully describe migration, portability, recovery, cases and security limits.
- [ ] Release archive contains no user memory artifacts.
