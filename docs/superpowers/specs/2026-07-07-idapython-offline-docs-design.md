# IDAPython Offline Docs Bundle — Design Spec

**Date:** 2026-07-07
**Status:** Approved (Approach B — Manifest + Verify)
**Branch target:** `feat/idapython-offline-docs`
**Origin:** Hex-Rays docs `python.docs.hex-rays.com` returns `403 Forbidden`
on deep-link HTML pages (CDN bot protection). Reviewer subagent in
`docs-review gate` (origin `4295fdc`) wastes turns retrying HTML URLs that
all fail before pivoting to raw RST sources.

---

## Context

**Root cause** (confirmed via log capture, 2026-07-07):

| URL pattern | Result |
|-------------|--------|
| `/ida_<module>/index.html` | `200 OK` (module index) |
| `/ida_<module>/<func>.html` | **`403 Forbidden`** (bot-protected) |
| `/_sources/ida_<module>/index.rst.txt` | `200 OK` (raw Sphinx source) |

The bundled `ida-scripting` skill (`lucnhan/skills/builtins/ida-scripting/`)
ships an `api-reference.md` (~451 lines) covering only ~10 modules. The
docs-review gate's reviewer prompt previously steered the LLM to broken
HTML URLs; prompt fix landed in `lucnhan/agent/agents/ida_docs_reviewer.py`
and `SKILL.md` ("When to fetch more") on this date — but every fetch still
incurs a network round-trip + a turn-budget cost (max 6 turns per script).

**Reliability gap:** users on restricted networks (corporate firewalls,
offline IDA sessions, CDN-blocked regions) cannot run the docs reviewer
at all. Even when online, ~50% of fetches fail on first attempt.

**Goal (locked in brainstorming):** make the docs reviewer fully
**offline-first**. Bundle the entire Hex-Rays Python reference once at
build time, ship it inside the plugin, and serve it via a local tool.

---

## Decisions (đã chốt qua brainstorming)

1. **Mục đích**: Reliability — plugin docs reviewer phải hoạt động đầy đủ
   khi user offline / bị firewall chặn Hex-Rays.
2. **Phạm vi**: All Hex-Rays Python modules (~50+ files: `ida_typeinf`,
   `ida_name`, `ida_hexrays`, `ida_frame`, `ida_funcs`, `ida_bytes`,
   `ida_xref`, `ida_segment`, `ida_kernwin`, `ida_ua`, `idautils`, `idc`,
   `idaapi`, `ida_nalt`, `ida_ida`, `ida_lines`, `ida_gdl`, `ida_search`,
   `ida_loader`, `ida_dbg`, `ida_netnode`, ...).
3. **Format**: Raw RST (canonical, smallest ~500-800 KB, preserves Sphinx
   directives).
4. **Cập nhật**: One-shot build script — manual hoặc CI, không auto-update.
5. **Storage & integration**: Bundle tách riêng tại
   `lucnhan/data/idapython-docs/<module>.rst.txt` + `MANIFEST.json`.
   Truy cập qua tool mới `lookup_idapython_doc(module)` (token-efficient —
   chỉ pay khi agent cần tra cứu).
6. **Module list source**: Auto-discover từ `python.docs.hex-rays.com/`
   index page (no hardcoded list).
7. **Approach**: **B. Manifest + verify** — bundle có `MANIFEST.json` track
   fetch_date, SHA-256, source URLs. Build script hỗ trợ `--verify` để so
   sánh bundle với upstream.
8. **Tool API**: Module-level only (giống `web_fetch` pattern), có pagination
   `offset`/`limit` để handle file > 7600 chars.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│  BUILD TIME (developer/CI machine, not IDA)                            │
│                                                                          │
│  scripts/build_idapython_docs.py                                        │
│   │ 1. GET python.docs.hex-rays.com/   (HTML module index)              │
│   │ 2. Parse module list (stdlib html.parser)                           │
│   │ 3. For each module:                                                 │
│   │    GET /_sources/<module>/index.rst.txt                             │
│   │    Save to lucnhan/data/idapython-docs/<module>.rst.txt             │
│   │ 4. Write MANIFEST.json {version, fetched_at, modules: [{...}]}      │
│   │ 5. Optional: --verify (compare local hash vs upstream HEAD)         │
│   └─→ Committed to git as snapshot                                       │
└─────────────────────────────────────────────────────────────────────────┘
                                  │
                                  │ (read at runtime)
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  RUNTIME (IDA Pro with Luc Nhan loaded)                                  │
│                                                                          │
│  lucnhan/tools/idapython_docs.py                                        │
│   @tool(name="lookup_idapython_doc", ...)                               │
│   def lookup_idapython_doc(module: str) -> str:                         │
│       # Read lucnhan/data/idapython-docs/<module>.rst.txt               │
│       # Return header + content (paginated like web_fetch)              │
│                                                                          │
│  Registry: lucnhan/ida/tools/registry.py                                │
│   _BOOT_TOOL_MODULES += ("lucnhan.tools.idapython_docs",)               │
│                                                                          │
│  Prompt updates:                                                        │
│   - ida_docs_reviewer.py: prefer lookup_idapython_doc over web_fetch   │
│   - ida-scripting/SKILL.md: same                                       │
└─────────────────────────────────────────────────────────────────────────┘
```

**Boundary tường minh:**
- **Build time** không depend IDA → có thể chạy trên máy dev/CI thuần Python.
- **Runtime** không depend network → agent luôn có docs offline.

---

## Components

### Component 1: Build Script (`scripts/build_idapython_docs.py`)

```python
# Usage:
#   python scripts/build_idapython_docs.py          # initial build
#   python scripts/build_idapython_docs.py --verify # check current vs upstream
#   python scripts/build_idapython_docs.py --update # refresh only changed

BASE_URL = "https://python.docs.hex-rays.com"
OUTPUT_DIR = REPO_ROOT / "lucnhan" / "data" / "idapython-docs"
MANIFEST = OUTPUT_DIR / "MANIFEST.json"

# 1. Discover: fetch HTML index, parse module links
modules = discover_modules_from_index()  # -> List[str]

# 2. Fetch loop with progress + retries (3x exponential backoff)
for module in modules:
    text = fetch_with_retry(f"{BASE_URL}/_sources/{module}/index.rst.txt")
    write_file(OUTPUT_DIR / f"{module}.rst.txt", text)
    update_manifest_entry(module, sha256(text))

# 3. Write MANIFEST.json atomically (write to .tmp then rename)
# 4. Print summary: total modules, total KB, failures

# --verify: compare local SHA256 vs upstream HEAD for each module,
#           exit 0 if all match, 1 if any drift
# --update: same as default build but skip unchanged modules (use SHA256 check)
```

**Dependencies:** stdlib only (`urllib.request`, `hashlib`, `json`,
`pathlib`, `argparse`, `re`, `tempfile`, `html.parser`). No `requests`,
no `beautifulsoup4` — zero install burden, runs in CI without
`uv pip install`.

### Component 2: MANIFEST.json

```json
{
  "schema_version": 1,
  "upstream_base": "https://python.docs.hex-rays.com",
  "fetched_at": "2026-07-07T12:34:56Z",
  "module_count": 51,
  "total_bytes": 612340,
  "modules": [
    {
      "name": "ida_typeinf",
      "file": "ida_typeinf.rst.txt",
      "source_url": "https://python.docs.hex-rays.com/_sources/ida_typeinf/index.rst.txt",
      "sha256": "a1b2c3d4...",
      "byte_size": 18432,
      "fetched_at": "2026-07-07T12:34:57Z"
    }
  ]
}
```

Schema versioning allows future format changes without breaking old
bundles. Field `schema_version` is mandatory; readers MUST reject unknown
versions loudly.

### Component 3: Tool (`lucnhan/tools/idapython_docs.py`)

```python
DOCS_DIR = Path(__file__).resolve().parent.parent / "data" / "idapython-docs"

@tool(name="lookup_idapython_doc", category="documentation", mutating=False, timeout=5.0)
def lookup_idapython_doc(
    module: Annotated[str, "Module name (e.g. 'ida_typeinf', 'idautils', 'ida_hexrays')"],
    offset: Annotated[int, "Character offset for pagination"] = 0,
    limit: Annotated[int, "Max chars to return (default 7400)"] = 7400,
) -> str:
    """Look up an IDAPython module's documentation from the bundled offline bundle.

    Reads from lucnhan/data/idapython-docs/<module>.rst.txt — works without
    network access. Use this BEFORE web_fetch against python.docs.hex-rays.com
    because the site is bot-protected (403 on deep-link HTML pages).

    Returns raw RST content (same format as the Sphinx source files).
    """
    # Sanitize: reject '..', '/', '\' to prevent path traversal
    safe = sanitize_module_name(module)
    file_path = DOCS_DIR / f"{safe}.rst.txt"
    if not file_path.is_file():
        return format_missing_module_error(module)
    content = file_path.read_text(encoding="utf-8")
    return paginate(content, offset=offset, limit=limit, header=f"[Offline IDAPython docs: {module}; ...]")
```

`format_missing_module_error` returns:
```
[Module 'foo' not in offline bundle]
Available modules (51): ida_api, ida_*, ... (alphabetized, first 20)
Tip: run scripts/build_idapython_docs.py to refresh,
     or fall back to web_fetch() for this module.
```

### Component 4: Tool Registration

Trong `lucnhan/ida/tools/registry.py`, thêm vào `_BOOT_TOOL_MODULES`:

```python
_BOOT_TOOL_MODULES = (
    ...,
    "lucnhan.tools.idapython_docs",  # NEW
)
```

### Component 5: Prompt Updates

Cập nhật 2 file đã sửa trước đó (`ida_docs_reviewer.py` section B +
`SKILL.md` "When to fetch more"):

```
OLD: web_fetch(url="https://python.docs.hex-rays.com/_sources/<module>/index.rst.txt", ...)
NEW: lookup_idapython_doc(module="<module>")
     → Falls back to web_fetch(...) only if module not in bundle (rare).
```

---

## Data Flow

### Flow A: Build time

```
[Dev/CI machine]
    │
    ├─ 1. GET https://python.docs.hex-rays.com/         (HTML module index)
    │     └─ Parse with stdlib html.parser → List[str] module names
    │
    ├─ 2. For each module name (with progress bar via stdlib):
    │     ├─ GET https://python.docs.hex-rays.com/_sources/<module>/index.rst.txt
    │     │   ├─ If 200 OK: read body, compute SHA-256
    │     │   ├─ If 4xx/5xx: log warning, add to "failures" list, continue
    │     │   └─ Retry: 3x exponential backoff (1s, 2s, 4s) on network errors
    │     └─ Write lucnhan/data/idapython-docs/<module>.rst.txt atomically
    │
    ├─ 3. Write MANIFEST.json atomically (tmp file → rename)
    │     └─ If MANIFEST.json exists, preserve schema_version
    │
    └─ 4. Print summary:
          "✓ 51/53 modules fetched (612 KB total, 2 failures: ida_aaa, ida_bbb)"
        Exit 0 if all OK, 1 if any failures, 2 if HTML index 403 (fatal)
```

**Concurrency:** Single-threaded by default (sequential fetches ≈ 51
requests × ~200ms = ~10s). `--parallel N` flag deferred to v2 (not v1).

**Atomicity:** Every file write uses `tempfile.NamedTemporaryFile` +
`os.replace()`. MANIFEST.json is the last write — if build crashes
mid-way, old bundle stays valid.

### Flow B: Runtime — agent calls `lookup_idapython_doc`

```
[IDA Pro / Luc Nhan agent loop]
    │
    ├─ 1. Agent emits tool call: lookup_idapython_doc(module="ida_typeinf")
    │
    ├─ 2. Tool handler:
    │     ├─ Sanitize module name (allow [a-z0-9_]+ only, reject "..", "/", "\")
    │     ├─ Path: lucnhan/data/idapython-docs/<module>.rst.txt
    │     ├─ if file exists: read utf-8, paginate, return header + content
    │     └─ if missing: return error message listing available modules
    │
    └─ 3. Tool result flows back to agent via normal TurnEvent → TOOL_RESULT
```

### Flow C: `--verify` mode

```
[Dev/CI machine]
    │
    ├─ 1. Load local MANIFEST.json
    │
    ├─ 2. GET https://python.docs.hex-rays.com/         (re-fetch index)
    │     └─ Parse current module list from upstream
    │
    ├─ 3. For each (local_module, upstream_module):
    │     ├─ If local but not upstream: MISSING (upstream removed)
    │     ├─ If upstream but not local: NEW (upstream added)
    │     ├─ If both: GET HEAD, compare SHA-256
    │     │     ├─ Match: OK
    │     │     └─ Mismatch: DRIFT (local=<hash> remote=<hash>)
    │     └─ Aggregate counts
    │
    └─ 4. Print summary + exit codes:
         - 0: no drift, no missing
         - 1: drift or missing detected
         - 2: network error during verify
```

---

## Error Handling

### Build time

| Mode | Behavior |
|------|----------|
| **Network timeout / connection refused** | Retry 3x with exponential backoff (1s, 2s, 4s). If still fails, log warning + add to failures list, continue with next module. Exit 1 if any failures. |
| **HTTP 404 (module exists in index but source URL broken)** | Skip with warning. Bundle will be partial. Exit 1. |
| **HTTP 5xx (Hex-Rays server error)** | Retry 3x same as timeout. Then skip with warning. |
| **HTML index 403 (CDN blocks our build)** | **Fatal** — fail fast with clear message: "Hex-Rays returned 403 on the module index. Try again later or run from a different IP/CDN." Exit 2 (distinct from "partial failure"). |
| **Filesystem: read-only disk / no space** | `OSError` propagates with clear message including the path. Exit 3. |
| **HTML parsing fails (unexpected format)** | Try stdlib regex fallback. If still fails, fatal: "Cannot parse module list — Hex-Rays changed page structure. Please update the parser." |
| **Atomic write interrupted (kill -9 mid-write)** | Old files remain valid because we use `tempfile + os.replace`. Incomplete `.tmp` files may exist but are not referenced by MANIFEST.json. |

### `--verify` mode

| Mode | Behavior |
|------|----------|
| **Module missing upstream (deleted from Sphinx)** | Print `MISSING: ida_foo` line. Exit 1 if any MISSING. |
| **Module added upstream** | Print `NEW: ida_bar`. Exit 0 (informational). |
| **Module content differs upstream (drift)** | Print `DRIFT: ida_baz (local=a1b2... remote=c3d4...)`. Exit 1. |
| **Network failure during verify** | Print `OFFLINE: cannot reach upstream`. Exit 2. |

### Runtime tool errors

| Mode | Behavior |
|------|----------|
| **Module name not in bundle** (typo, new module) | Return helpful message listing available modules (first 20 alphabetized + total count). Suggest running build script. |
| **Path traversal attempt** (`module="../../../etc/passwd"`) | Sanitize to `[a-z0-9_]+` regex. If fails → return `"Error: invalid module name"`. No filesystem access to outside DOCS_DIR. |
| **MANIFEST.json missing or corrupt** | Tool still works (reads files directly). Log warning at startup. Bundle still usable. (Manifest is informational, not required for runtime correctness.) |
| **RST file truncated / zero bytes** | Detect via `os.stat().st_size == 0` on the file directly (no dependency on MANIFEST, which is informational). Log warning + return whatever content is present. |
| **Module file larger than tool result limit** (>7600 chars) | Auto-paginate: return first 7400 chars + header noting total + "use offset=N to continue". Same UX as `web_fetch`. |

### Security note

- Tool **only** reads from `DOCS_DIR`. Never joins user input untrusted. Sanitization regex prevents traversal even if I forget to validate path.
- Bundle **may** contain RST directives that look like code injection (`:py:function:`), but they're inert when read as plain text — RST is not executable.

---

## Testing Strategy

Phủ 3 layers: unit tests (mocked, luôn chạy trong CI), integration tests
(real fetch có flag), và prompt regression tests.

### Layer 1: Unit tests (mocked)

**File:** `tests/test_idapython_docs_tool.py` (mới)

```python
class TestLookupIdapythonDoc(unittest.TestCase):
    def test_reads_existing_module(self):
    def test_module_not_found_lists_available(self):
    def test_path_traversal_rejected(self):
    def test_pagination_works(self):
    def test_empty_file_returns_empty_marker(self):
    def test_manifest_missing_does_not_break_tool(self):
```

**File:** `tests/test_build_idapython_docs.py` (mới)

```python
class TestBuildScript(unittest.TestCase):
    def test_discover_modules_parses_index_html(self):
    def test_discover_handles_unexpected_html(self):
    def test_fetch_with_retry_retries_on_timeout(self):
    def test_fetch_marks_404_as_skip(self):
    def test_manifest_atomic_write(self):
    def test_manifest_schema_version_preserved(self):
    def test_verify_reports_drift(self):
    def test_verify_reports_new_module(self):
    def test_verify_reports_missing(self):
    def test_cli_invocation(self):
```

### Layer 2: Integration test (real network, opt-in)

**File:** `tests/test_build_idapython_docs_integration.py`

```python
@pytest.mark.integration
@pytest.mark.skipif(not os.getenv("RUN_NETWORK_TESTS"), reason="opt-in via RUN_NETWORK_TESTS=1")
def test_real_fetch_one_module(tmp_path):
    """Fetch one real module end-to-end. Skipped unless RUN_NETWORK_TESTS=1."""
```

This is the only test that hits real Hex-Rays. CI does NOT run it by
default; developers run locally before commit if they want to verify the
bundle still works.

### Layer 3: Prompt regression tests (extend existing)

**File:** `tests/test_ida_docs_review_prompt.py` (extend)

Thêm 4 tests:

```python
def test_prompt_prefers_lookup_tool_over_web_fetch(self):
def test_prompt_demotes_web_fetch_to_fallback(self):
def test_skill_prefers_lookup_tool_over_web_fetch(self):
def test_skill_demotes_web_fetch_to_fallback(self):
```

### Coverage matrix

| Component | Unit | Integration |
|-----------|------|-------------|
| Build script (discover/fetch/manifest) | ✅ mocked HTTP | ✅ real HTTP opt-in |
| Build script (--verify) | ✅ mocked HTTP | — |
| Tool (read/paginate/errors) | ✅ mocked FS | — |
| Prompt guidance | ✅ string search | — |
| Path traversal security | ✅ explicit test | — |

**Total new tests:** ~21 (10 build + 6 tool + 4 prompt extension + 1 integration)

**Coverage target:** ≥80% for new code per project rules.

---

## Risks & Open Questions

1. **Hex-Rays page structure may change**: parser uses stdlib html.parser
   and may break if Sphinx output changes. Mitigation: regex fallback in
   parser, loud error message.
2. **Bundle size**: estimated ~500-800 KB raw RST for ~50 modules.
   Acceptable for a plugin (compare: `api-reference.md` is already ~15 KB
   bundled). May want git LFS if repo size matters.
3. **Module list discovery**: depends on Hex-Rays keeping
   `python.docs.hex-rays.com/` as an index page. If they remove it,
   fall back to hardcoded list (deferred to v2).
4. **No `--update` flag in v1**: script only does full build or `--verify`.
   `--update` (refresh only changed) deferred to v2 since verify+rebuild
   is fast enough (~10s).

---

## Out of scope (deferred)

- Topic/substring search inside modules (v2 if needed)
- Cross-module search / inverted index (v2)
- Auto-update at plugin startup (user explicitly chose manual)
- `web_fetch` integration / cache layer (separate concern, may be v3)
- Lazy online fallback when module missing (violates offline-first)