# IDAPython Offline Docs — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bundle the Hex-Rays Python reference (`/_sources/<module>/index.rst.txt`) into the plugin so the docs-reviewer subagent has authoritative IDAPython docs offline, eliminating 403-induced turn waste and supporting fully-offline IDA sessions.

**Architecture:** Two cooperating halves — (1) `scripts/build_idapython_docs.py` runs at dev/CI time to fetch and write raw RST files + MANIFEST.json, (2) `lucnhan/tools/idapython_docs.py` exposes a `lookup_idapython_doc(module)` tool that reads the bundle at runtime with no network dependency. Prompts prefer the tool over `web_fetch` for Hex-Rays docs.

**Tech Stack:** Python 3.10+, stdlib only for build script (`urllib.request`, `hashlib`, `json`, `pathlib`, `argparse`, `tempfile`, `html.parser`). Tool uses existing `@tool` decorator + `ida_docs_reviewer.py` prompt patterns.

**Spec:** `docs/superpowers/specs/2026-07-07-idapython-offline-docs-design.md`

## Global Constraints

- **Stdlib-only for build script** — no `requests`, no `beautifulsoup4`. Tests must run without `uv pip install`.
- **Path traversal prevention** — tool MUST sanitize module name to `[a-z0-9_]+` regex; reject all other inputs without filesystem access.
- **Atomic writes** — every file write uses `tempfile.NamedTemporaryFile` + `os.replace()`; partial writes never leave the bundle inconsistent.
- **MANIFEST schema versioning** — `schema_version` field is mandatory; bundle committed with `schema_version: 1`.
- **Bundle size** — committed `lucnhan/data/idapython-docs/` ~500-800 KB raw RST across ~50 modules (acceptable; idiomatic for plugin distribution).
- **Gitignore** — `*.tmp` and `*.tmp.*` in `lucnhan/data/` excluded; committed `.rst.txt` files and `MANIFEST.json` stay.
- **Python style** (project rules): `from __future__ import annotations`, type hints on all signatures, no mutation, f-strings, hex `f"0x{ea:x}"`, no magic numbers, no bare `except:`.
- **Reviewer prompt format** — keep YAML-friendly plain markdown; backticks-for-code; never invent APIs.
- **Test framework** — `unittest` (matches existing `tests/test_idapython_docs_gate.py`); pytest as runner.
- **Coverage target** — ≥80% for new code per project rules.
- **Conventional commits** — `feat(...)`, `fix(...)`, `test(...)`, `docs(...)`, `chore(...)` formats.

---

## File Map

| Path | Status | Responsibility |
|------|--------|----------------|
| `scripts/build_idapython_docs.py` | CREATE | Build-time CLI: discover/fetch/manifest/verify |
| `lucnhan/tools/idapython_docs.py` | CREATE | Runtime tool: read + paginate + error |
| `lucnhan/data/idapython-docs/<module>.rst.txt` | CREATE (build) | One file per module (~50), raw RST |
| `lucnhan/data/idapython-docs/MANIFEST.json` | CREATE (build) | Bundle metadata: schema_version, fetch_date, hashes |
| `tests/test_idapython_docs_tool.py` | CREATE | Tool unit tests (mocked filesystem) |
| `tests/test_build_idapython_docs.py` | CREATE | Build script unit tests (mocked HTTP) |
| `tests/test_build_idapython_docs_integration.py` | CREATE | Opt-in real-fetch integration test |
| `tests/test_ida_docs_review_prompt.py` | MODIFY | Add 4 prompt regression tests (extend existing) |
| `lucnhan/ida/tools/registry.py` | MODIFY | Register new tool in `_BOOT_TOOL_MODULES` |
| `lucnhan/agent/agents/ida_docs_reviewer.py` | MODIFY | Update prompt section B to prefer new tool |
| `lucnhan/skills/builtins/ida-scripting/SKILL.md` | MODIFY | Update "When to fetch more" section |
| `.gitignore` | MODIFY | Ignore `lucnhan/data/**/*.tmp` atomic-write remnants |

---

## Task 1: Build script — HTML index parser

**Files:**
- Create: `scripts/__init__.py` (empty marker file — Python 3.3+ supports implicit packages, but explicit is clearer)
- Create: `scripts/build_idapython_docs.py`
- Test: `tests/test_build_idapython_docs.py`

**Interfaces:**
- Consumes: stdlib (`urllib.request`, `html.parser`, `re`)
- Produces: `discover_modules_from_index(html_content: str) -> list[str]` — public function for testing and reuse

- [ ] **Step 1: Create scripts/ directory + write failing test**

Create `scripts/__init__.py` (empty), then `scripts/build_idapython_docs.py` with just the imports + a stub that raises `NotImplementedError`. Then create test file:

```python
"""Unit tests for scripts/build_idapython_docs.py"""
from __future__ import annotations

import unittest

from scripts.build_idapython_docs import discover_modules_from_index


class TestDiscoverModules(unittest.TestCase):
    def test_parses_module_links_from_index_html(self):
        # Hex-Rays index page has <a href="ida_typeinf/"> links for each module
        html = """
        <html><body>
        <a href="ida_typeinf/">ida_typeinf</a>
        <a href="ida_name/">ida_name</a>
        <a href="idautils/">idautils</a>
        <a href="idaapi/">idaapi</a>
        <a href="https://example.com/external/">skip me</a>
        <a href="#fragment">skip me too</a>
        </body></html>
        """
        result = discover_modules_from_index(html)
        self.assertEqual(
            sorted(result),
            ["ida_name", "ida_typeinf", "idaapi", "idautils"],
        )

    def test_empty_html_returns_empty_list(self):
        self.assertEqual(discover_modules_from_index(""), [])

    def test_malformed_html_no_modules_returns_empty(self):
        # If no <a href="<module>/"> matches, parser returns empty
        html = "<html><body><p>no modules here</p></body></html>"
        self.assertEqual(discover_modules_from_index(html), [])
```

- [ ] **Step 2: Run tests to verify RED**

Run: `pytest tests/test_build_idapython_docs.py -v`
Expected: FAIL with `ImportError` or `ModuleNotFoundError` for `scripts.build_idapython_docs`, then `AttributeError: module 'scripts.build_idapython_docs' has no attribute 'discover_modules_from_index'`.

Note: if `scripts/` is not a package, the test will fail with import error. Ensure `scripts/__init__.py` exists.

- [ ] **Step 3: Implement `discover_modules_from_index`**

Replace `scripts/build_idapython_docs.py` content with:

```python
"""Build the offline IDAPython docs bundle from Hex-Rays upstream.

Runs at dev/CI time (NOT inside IDA). Produces raw RST files + MANIFEST.json
under lucnhan/data/idapython-docs/. See:
docs/superpowers/specs/2026-07-07-idapython-offline-docs-design.md
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants (no magic numbers; values from spec section Components)
# ---------------------------------------------------------------------------

BASE_URL: str = "https://python.docs.hex-rays.com"
UPSTREAM_INDEX_URL: str = f"{BASE_URL}/"
SOURCES_URL_TEMPLATE: str = f"{BASE_URL}/_sources/{{module}}/index.rst.txt"

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
OUTPUT_DIR: Path = REPO_ROOT / "lucnhan" / "data" / "idapython-docs"
MANIFEST_PATH: Path = OUTPUT_DIR / "MANIFEST.json"

MANIFEST_SCHEMA_VERSION: int = 1

HTTP_TIMEOUT_SECONDS: float = 30.0
MAX_RETRIES: int = 3
INITIAL_BACKOFF_SECONDS: float = 1.0
BACKOFF_MULTIPLIER: float = 2.0

# Module names are [a-z0-9_]+ per spec; we accept that here, and a Sphinx
# index page contains <a href="ida_typeinf/"> style entries.
_MODULE_LINK_RE: re.Pattern[str] = re.compile(
    r'<a\s+href="(?P<module>[a-z0-9_]+)/?"'
)


# ---------------------------------------------------------------------------
# HTML index parser — Task 1 deliverable
# ---------------------------------------------------------------------------


def discover_modules_from_index(html_content: str) -> list[str]:
    """Parse the Hex-Rays index page and return module names.

    Args:
        html_content: Raw HTML of https://python.docs.hex-rays.com/

    Returns:
        Sorted list of unique module names matching the Sphinx index
        pattern (e.g. ``["ida_name", "ida_typeinf", "idautils"]``).
        External URLs, fragments, and non-module hrefs are skipped.
    """
    modules = set(_MODULE_LINK_RE.findall(html_content))
    return sorted(modules)


__all__ = [
    "BASE_URL",
    "UPSTREAM_INDEX_URL",
    "SOURCES_URL_TEMPLATE",
    "OUTPUT_DIR",
    "MANIFEST_PATH",
    "MANIFEST_SCHEMA_VERSION",
    "HTTP_TIMEOUT_SECONDS",
    "MAX_RETRIES",
    "INITIAL_BACKOFF_SECONDS",
    "BACKOFF_MULTIPLIER",
    "discover_modules_from_index",
]
```

- [ ] **Step 4: Run tests to verify GREEN**

Run: `pytest tests/test_build_idapython_docs.py -v`
Expected: 3 passed.

- [ ] **Step 5: Lint + format**

Run: `python -m ruff check scripts/build_idapython_docs.py tests/test_build_idapython_docs.py`
Run: `python -m ruff format scripts/build_idapython_docs.py tests/test_build_idapython_docs.py`

- [ ] **Step 6: Commit**

```bash
git add scripts/__init__.py scripts/build_idapython_docs.py tests/test_build_idapython_docs.py
git commit -m "feat(scripts): parse Hex-Rays docs index — discover_modules_from_index()"
```

---

## Task 2: Build script — fetch with retry

**Files:**
- Modify: `scripts/build_idapython_docs.py`
- Modify: `tests/test_build_idapython_docs.py`

**Interfaces:**
- Consumes: `urllib.request.urlopen`, `urllib.error.HTTPError`, `urllib.error.URLError`, `socket.timeout`
- Produces: `fetch_with_retry(url: str, max_retries: int = MAX_RETRIES) -> str | None`

Returns the response body on success, or `None` on persistent failure (4xx/5xx/network timeout after retries). The function does NOT raise — it logs warnings and returns None so the build loop can continue.

- [ ] **Step 1: Add failing tests**

Append to `tests/test_build_idapython_docs.py`:

```python
from unittest.mock import patch, MagicMock
from scripts.build_idapython_docs import fetch_with_retry


class TestFetchWithRetry(unittest.TestCase):
    def test_successful_fetch_returns_body(self):
        mock_response = MagicMock()
        mock_response.read.return_value = b"ida_typeinf module docs"
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_response):
            result = fetch_with_retry("https://example.com/test")
        self.assertEqual(result, "ida_typeinf module docs")

    def test_retries_on_timeout_then_succeeds(self):
        # First 2 calls raise timeout, 3rd succeeds
        mock_response = MagicMock()
        mock_response.read.return_value = b"success"
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch(
            "urllib.request.urlopen",
            side_effect=[TimeoutError("net"), TimeoutError("net"), mock_response],
        ):
            result = fetch_with_retry("https://example.com/test", max_retries=3)
        self.assertEqual(result, "success")

    def test_persistent_timeout_returns_none(self):
        with patch("urllib.request.urlopen", side_effect=TimeoutError("net")):
            result = fetch_with_retry("https://example.com/test", max_retries=2)
        self.assertIsNone(result)

    def test_http_404_returns_none_no_retry(self):
        # 4xx is not retried — module path is genuinely wrong
        error = urllib.error.HTTPError(
            "https://example.com/x", 404, "Not Found", {}, None
        )
        with patch("urllib.request.urlopen", side_effect=error):
            result = fetch_with_retry("https://example.com/x", max_retries=3)
        self.assertIsNone(result)
```

Note: add `import urllib.error` at the top of the test file.

- [ ] **Step 2: Run tests to verify RED**

Run: `pytest tests/test_build_idapython_docs.py::TestFetchWithRetry -v`
Expected: FAIL with `AttributeError: module 'scripts.build_idapython_docs' has no attribute 'fetch_with_retry'`

- [ ] **Step 3: Implement `fetch_with_retry`**

Append to `scripts/build_idapython_docs.py` (before `__all__`):

```python
def fetch_with_retry(
    url: str,
    max_retries: int = MAX_RETRIES,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> str | None:
    """Fetch ``url`` with exponential backoff on transient network errors.

    Retryable errors (raise and retry up to ``max_retries`` times):
        socket.timeout, ConnectionError, TimeoutError,
        urllib.error.URLError (network-level).

    Non-retryable (return None immediately):
        urllib.error.HTTPError with 4xx (client error — bad path,
        retrying won't help). 5xx is treated as retryable.

    Returns:
        Response body as UTF-8 string, or ``None`` if all attempts fail.
    """
    last_error: BaseException | None = None
    backoff = INITIAL_BACKOFF_SECONDS
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # 4xx is permanent (bad URL/path); 5xx is transient
            if 400 <= exc.code < 500:
                print(f"[skip] {url}: HTTP {exc.code}", file=sys.stderr)
                return None
            last_error = exc
        except (TimeoutError, ConnectionError, urllib.error.URLError, OSError) as exc:
            last_error = exc
        # Backoff before next attempt (skip on last attempt)
        if attempt < max_retries - 1:
            time.sleep(backoff)
            backoff *= BACKOFF_MULTIPLIER
    print(f"[fail] {url}: {type(last_error).__name__}: {last_error}", file=sys.stderr)
    return None
```

And add `"fetch_with_retry"` to the `__all__` list at the bottom.

- [ ] **Step 4: Run tests to verify GREEN**

Run: `pytest tests/test_build_idapython_docs.py::TestFetchWithRetry -v`
Expected: 4 passed.

- [ ] **Step 5: Lint + format**

Run: `python -m ruff check scripts/build_idapython_docs.py tests/test_build_idapython_docs.py --fix`
Run: `python -m ruff format scripts/build_idapython_docs.py tests/test_build_idapython_docs.py`

- [ ] **Step 6: Commit**

```bash
git add scripts/build_idapython_docs.py tests/test_build_idapython_docs.py
git commit -m "feat(scripts): fetch_with_retry with exponential backoff for build-time fetches"
```

---

## Task 3: Build script — atomic write + SHA-256 helper

**Files:**
- Modify: `scripts/build_idapython_docs.py`
- Modify: `tests/test_build_idapython_docs.py`

**Interfaces:**
- Produces: `write_atomic(path: Path, content: str | bytes) -> None` — writes via temp + os.replace
- Produces: `sha256_text(text: str) -> str` — hex digest of UTF-8 bytes

- [ ] **Step 1: Add failing tests**

Append to `tests/test_build_idapython_docs.py`:

```python
from pathlib import Path
import tempfile
from scripts.build_idapython_docs import write_atomic, sha256_text


class TestHelpers(unittest.TestCase):
    def test_write_atomic_creates_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "subdir" / "out.txt"
            write_atomic(target, "hello world")
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_text(encoding="utf-8"), "hello world")

    def test_write_atomic_overwrites_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.txt"
            target.write_text("old")
            write_atomic(target, "new")
            self.assertEqual(target.read_text(encoding="utf-8"), "new")

    def test_write_atomic_no_tmp_files_left_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.txt"
            write_atomic(target, "content")
            leftovers = list(Path(tmp).glob("*.tmp*"))
            self.assertEqual(leftovers, [], msg=f"leftover tmp files: {leftovers}")

    def test_sha256_text_deterministic(self):
        h1 = sha256_text("hello")
        h2 = sha256_text("hello")
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)  # SHA-256 hex = 64 chars
```

- [ ] **Step 2: Run tests to verify RED**

Run: `pytest tests/test_build_idapython_docs.py::TestHelpers -v`
Expected: FAIL with `ImportError` for `write_atomic` and `sha256_text`.

- [ ] **Step 3: Implement helpers**

Append to `scripts/build_idapython_docs.py`:

```python
def write_atomic(path: Path, content: str | bytes) -> None:
    """Write ``content`` to ``path`` atomically (no partial reads ever).

    Strategy: write to ``path.with_suffix(path.suffix + ".tmp")`` in the
    same directory, fsync, then ``os.replace()`` (POSIX-atomic, atomic
    on Windows for same-volume). If the parent directory does not exist,
    create it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = content.encode("utf-8") if isinstance(content, str) else content
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp_path, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        # Best-effort cleanup of leftover temp file
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def sha256_text(text: str) -> str:
    """Return hex SHA-256 digest of ``text`` (UTF-8 encoded)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
```

Add `"write_atomic"` and `"sha256_text"` to `__all__`.

- [ ] **Step 4: Run tests to verify GREEN**

Run: `pytest tests/test_build_idapython_docs.py::TestHelpers -v`
Expected: 4 passed.

- [ ] **Step 5: Lint + format**

Run: `python -m ruff check scripts/build_idapython_docs.py tests/test_build_idapython_docs.py --fix`
Run: `python -m ruff format scripts/build_idapython_docs.py tests/test_build_idapython_docs.py`

- [ ] **Step 6: Commit**

```bash
git add scripts/build_idapython_docs.py tests/test_build_idapython_docs.py
git commit -m "feat(scripts): atomic write + sha256 helpers for bundle artifacts"
```

---

## Task 4: Build script — MANIFEST write with schema versioning

**Files:**
- Modify: `scripts/build_idapython_docs.py`
- Modify: `tests/test_build_idapython_docs.py`

**Interfaces:**
- Produces: `ManifestEntry` dataclass (frozen): `name: str`, `file: str`, `source_url: str`, `sha256: str`, `byte_size: int`, `fetched_at: str`
- Produces: `Manifest` dataclass (frozen): `schema_version: int`, `upstream_base: str`, `fetched_at: str`, `module_count: int`, `total_bytes: int`, `modules: tuple[ManifestEntry, ...]`
- Produces: `write_manifest(manifest: Manifest, path: Path = MANIFEST_PATH) -> None` — atomic write + JSON dump
- Produces: `load_manifest(path: Path = MANIFEST_PATH) -> Manifest | None` — read or None on missing/corrupt

- [ ] **Step 1: Add failing tests**

Append to `tests/test_build_idapython_docs.py`:

```python
import json
import dataclasses
from scripts.build_idapython_docs import (
    Manifest, ManifestEntry, write_manifest, load_manifest, MANIFEST_SCHEMA_VERSION,
)


def _sample_entry(name: str = "ida_typeinf") -> ManifestEntry:
    return ManifestEntry(
        name=name,
        file=f"{name}.rst.txt",
        source_url=f"https://python.docs.hex-rays.com/_sources/{name}/index.rst.txt",
        sha256="a" * 64,
        byte_size=12345,
        fetched_at="2026-07-07T00:00:00Z",
    )


class TestManifestRoundTrip(unittest.TestCase):
    def test_write_then_load_returns_equal_manifest(self):
        manifest = Manifest(
            schema_version=MANIFEST_SCHEMA_VERSION,
            upstream_base="https://python.docs.hex-rays.com",
            fetched_at="2026-07-07T00:00:00Z",
            module_count=2,
            total_bytes=24690,
            modules=(_sample_entry("ida_typeinf"), _sample_entry("ida_name")),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "MANIFEST.json"
            write_manifest(manifest, path=path)
            loaded = load_manifest(path=path)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded, manifest)

    def test_write_atomic_no_tmp_leftovers(self):
        manifest = Manifest(
            schema_version=MANIFEST_SCHEMA_VERSION,
            upstream_base="https://python.docs.hex-rays.com",
            fetched_at="2026-07-07T00:00:00Z",
            module_count=1,
            total_bytes=100,
            modules=(_sample_entry(),),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "MANIFEST.json"
            write_manifest(manifest, path=path)
            leftovers = list(Path(tmp).glob("*.tmp*"))
            self.assertEqual(leftovers, [])

    def test_load_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = load_manifest(path=Path(tmp) / "MANIFEST.json")
            self.assertIsNone(result)

    def test_load_corrupt_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "MANIFEST.json"
            path.write_text("not valid json {{{", encoding="utf-8")
            self.assertIsNone(load_manifest(path=path))

    def test_schema_version_preserved_across_writes(self):
        # If existing MANIFEST exists with schema_version=N, write_manifest
        # does NOT bump to current MANIFEST_SCHEMA_VERSION blindly —
        # we just preserve what's passed in.
        manifest = Manifest(
            schema_version=99,  # arbitrary
            upstream_base="https://x",
            fetched_at="2026-07-07T00:00:00Z",
            module_count=0,
            total_bytes=0,
            modules=(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "MANIFEST.json"
            write_manifest(manifest, path=path)
            loaded = load_manifest(path=path)
            self.assertEqual(loaded.schema_version, 99)

    def test_json_is_human_readable(self):
        # Pretty-printed with sort_keys for stable diffs
        manifest = Manifest(
            schema_version=1, upstream_base="https://x", fetched_at="t",
            module_count=1, total_bytes=10, modules=(_sample_entry(),),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "MANIFEST.json"
            write_manifest(manifest, path=path)
            raw = path.read_text(encoding="utf-8")
            # Pretty-printed = multiple lines
            self.assertGreater(raw.count("\n"), 5)
            # JSON parseable
            data = json.loads(raw)
            self.assertEqual(data["schema_version"], 1)
```

- [ ] **Step 2: Run tests to verify RED**

Run: `pytest tests/test_build_idapython_docs.py::TestManifestRoundTrip -v`
Expected: FAIL with `ImportError` for `Manifest`/`ManifestEntry`/`write_manifest`/`load_manifest`.

- [ ] **Step 3: Implement manifest dataclasses + I/O**

Append to `scripts/build_idapython_docs.py`:

```python
import dataclasses


@dataclasses.dataclass(frozen=True)
class ManifestEntry:
    """One module's metadata."""
    name: str
    file: str
    source_url: str
    sha256: str
    byte_size: int
    fetched_at: str


@dataclasses.dataclass(frozen=True)
class Manifest:
    """Bundle manifest — schema_version=1."""
    schema_version: int
    upstream_base: str
    fetched_at: str
    module_count: int
    total_bytes: int
    modules: tuple[ManifestEntry, ...]


def write_manifest(manifest: Manifest, path: Path = MANIFEST_PATH) -> None:
    """Write ``manifest`` as pretty-printed JSON to ``path`` atomically."""
    payload = json.dumps(dataclasses.asdict(manifest), indent=2, sort_keys=True)
    write_atomic(path, payload)


def load_manifest(path: Path = MANIFEST_PATH) -> Manifest | None:
    """Load manifest from ``path``, or return ``None`` on missing/corrupt."""
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    try:
        modules_tuple = tuple(ManifestEntry(**m) for m in data["modules"])
        return Manifest(
            schema_version=data["schema_version"],
            upstream_base=data["upstream_base"],
            fetched_at=data["fetched_at"],
            module_count=data["module_count"],
            total_bytes=data["total_bytes"],
            modules=modules_tuple,
        )
    except (KeyError, TypeError):
        return None
```

Add `"Manifest"`, `"ManifestEntry"`, `"write_manifest"`, `"load_manifest"` to `__all__`.

- [ ] **Step 4: Run tests to verify GREEN**

Run: `pytest tests/test_build_idapython_docs.py::TestManifestRoundTrip -v`
Expected: 6 passed.

- [ ] **Step 5: Lint + format**

Run: `python -m ruff check scripts/build_idapython_docs.py tests/test_build_idapython_docs.py --fix`
Run: `python -m ruff format scripts/build_build_idapython_docs.py tests/test_build_idapython_docs.py`

Note: fix ruff path if typo (should be `build_idapython_docs`).

- [ ] **Step 6: Commit**

```bash
git add scripts/build_idapython_docs.py tests/test_build_idapython_docs.py
git commit -m "feat(scripts): Manifest + ManifestEntry dataclasses with atomic JSON I/O"
```

---

## Task 5: Build script — main build loop + CLI

**Files:**
- Modify: `scripts/build_idapython_docs.py`
- Modify: `tests/test_build_idapython_docs.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `build_bundle(output_dir: Path = OUTPUT_DIR, *, now: str | None = None) -> tuple[int, int]` — returns (success_count, failed_count)
- Produces: `main(argv: list[str] | None = None) -> int` — argparse entry point with exit codes

CLI:
```
python scripts/build_idapython_docs.py           # full build
python scripts/build_idapython_docs.py --verify  # verify-only mode
```

- [ ] **Step 1: Add .gitignore entries**

Modify `.gitignore`, append:

```
# Atomic-write remnants in bundle directory
lucnhan/data/**/*.tmp
lucnhan/data/**/*.tmp.*
```

- [ ] **Step 2: Add failing tests**

Append to `tests/test_build_idapython_docs.py`:

```python
from unittest.mock import patch
import datetime
from scripts.build_idapython_docs import build_bundle


class TestBuildBundle(unittest.TestCase):
    def test_build_writes_one_file_per_module(self):
        # Mock upstream: index lists 2 modules, each RST returns known content
        index_html = '<a href="ida_typeinf/">x</a><a href="ida_name/">y</a>'
        with patch(
            "scripts.build_idapython_docs.urllib.request.urlopen"
        ) as mock_urlopen:
            mock_urlopen.side_effect = [
                _fake_response(index_html),
                _fake_response("# ida_typeinf docs"),
                _fake_response("# ida_name docs"),
            ]
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "bundle"
                success, failed = build_bundle(output_dir=out, now="2026-07-07T00:00:00Z")
        self.assertEqual(success, 2)
        self.assertEqual(failed, 0)
        self.assertTrue((out / "ida_typeinf.rst.txt").is_file())
        self.assertTrue((out / "ida_name.rst.txt").is_file())
        self.assertTrue((out / "MANIFEST.json").is_file())

    def test_build_skips_failed_modules_and_continues(self):
        index_html = '<a href="ida_ok/">x</a><a href="ida_404/">y</a>'
        with patch(
            "scripts.build_idapython_docs.urllib.request.urlopen"
        ) as mock_urlopen:
            # 1st call: index. 2nd: 200 for ida_ok. 3rd: 404 for ida_404.
            mock_urlopen.side_effect = [
                _fake_response(index_html),
                _fake_response("# ida_ok docs"),
                urllib.error.HTTPError("u", 404, "NF", {}, None),
            ]
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "bundle"
                success, failed = build_bundle(output_dir=out, now="2026-07-07T00:00:00Z")
        self.assertEqual(success, 1)
        self.assertEqual(failed, 1)

    def test_build_writes_valid_manifest(self):
        index_html = '<a href="ida_typeinf/">x</a>'
        with patch(
            "scripts.build_idapython_docs.urllib.request.urlopen"
        ) as mock_urlopen:
            mock_urlopen.side_effect = [
                _fake_response(index_html),
                _fake_response("# ida_typeinf docs"),
            ]
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "bundle"
                build_bundle(output_dir=out, now="2026-07-07T00:00:00Z")
        loaded = load_manifest(path=out / "MANIFEST.json")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.module_count, 1)
        self.assertEqual(loaded.modules[0].name, "ida_typeinf")


def _fake_response(body: str) -> MagicMock:
    mock = MagicMock()
    mock.read.return_value = body.encode("utf-8")
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    return mock
```

Add `from urllib.error import HTTPError` at the top if not already imported.

- [ ] **Step 3: Run tests to verify RED**

Run: `pytest tests/test_build_idapython_docs.py::TestBuildBundle -v`
Expected: FAIL with `ImportError` for `build_bundle`.

- [ ] **Step 4: Implement build_bundle + main**

Append to `scripts/build_idapython_docs.py`:

```python
def _fetch_index_html(timeout: float = HTTP_TIMEOUT_SECONDS) -> str:
    """Fetch and decode the Hex-Rays module index HTML page.

    Raises urllib.error.URLError on network failure so caller can decide.
    """
    with urllib.request.urlopen(UPSTREAM_INDEX_URL, timeout=timeout) as resp:  # noqa: S310
        return resp.read().decode("utf-8")


def build_bundle(
    output_dir: Path = OUTPUT_DIR,
    *,
    now: str | None = None,
    max_retries: int = MAX_RETRIES,
) -> tuple[int, int]:
    """Fetch all modules from upstream and write bundle to ``output_dir``.

    Returns:
        (success_count, failed_count). Both ints >= 0.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = now or (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    index_html = _fetch_index_html()
    modules = discover_modules_from_index(index_html)
    if not modules:
        print("[fatal] No modules discovered from index page", file=sys.stderr)
        return (0, 0)

    entries: list[ManifestEntry] = []
    success_count = 0
    failed_count = 0
    for module in modules:
        url = SOURCES_URL_TEMPLATE.format(module=module)
        body = fetch_with_retry(url, max_retries=max_retries)
        if body is None:
            failed_count += 1
            continue
        sha = sha256_text(body)
        write_atomic(output_dir / f"{module}.rst.txt", body)
        entries.append(
            ManifestEntry(
                name=module,
                file=f"{module}.rst.txt",
                source_url=url,
                sha256=sha,
                byte_size=len(body.encode("utf-8")),
                fetched_at=timestamp,
            )
        )
        success_count += 1

    manifest = Manifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        upstream_base=BASE_URL,
        fetched_at=timestamp,
        module_count=len(entries),
        total_bytes=sum(e.byte_size for e in entries),
        modules=tuple(entries),
    )
    write_manifest(manifest, path=output_dir / "MANIFEST.json")

    print(
        f"[ok] {success_count}/{len(modules)} modules fetched "
        f"({manifest.total_bytes:,} bytes, {failed_count} failures)",
    )
    return (success_count, failed_count)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns process exit code."""
    parser = argparse.ArgumentParser(
        prog="build_idapython_docs",
        description="Build the offline IDAPython docs bundle from Hex-Rays upstream.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Compare local bundle against upstream; exit 1 if drift.",
    )
    args = parser.parse_args(argv)

    if args.verify:
        return _run_verify()  # Implemented in Task 6

    success, failed = build_bundle()
    if failed > 0:
        return 1
    if success == 0:
        return 2  # Nothing succeeded
    return 0


def _run_verify() -> int:
    """Stub for Task 6 — full implementation in next task."""
    raise NotImplementedError("verify mode implemented in Task 6")
```

Add `import datetime` and update `__all__` to include `build_bundle`, `main`.

- [ ] **Step 5: Run tests to verify GREEN**

Run: `pytest tests/test_build_idapython_docs.py::TestBuildBundle -v`
Expected: 3 passed.

- [ ] **Step 6: Lint + format**

Run: `python -m ruff check scripts/build_idapython_docs.py tests/test_build_idapython_docs.py --fix`
Run: `python -m ruff format scripts/build_idapython_docs.py tests/test_build_idapython_docs.py`

- [ ] **Step 7: Commit**

```bash
git add scripts/build_idapython_docs.py tests/test_build_idapython_docs.py .gitignore
git commit -m "feat(scripts): full build loop + CLI for offline docs bundle"
```

---

## Task 6: Build script — --verify mode

**Files:**
- Modify: `scripts/build_idapython_docs.py`
- Modify: `tests/test_build_idapython_docs.py`

**Interfaces:**
- Produces: `verify_bundle(output_dir: Path = OUTPUT_DIR) -> tuple[int, int, int]` — returns (drift_count, new_count, missing_count)
- Replaces: `_run_verify()` to call `verify_bundle` and map to exit codes

- [ ] **Step 1: Add failing tests**

Append to `tests/test_build_idapython_docs.py`:

```python
from scripts.build_idapython_docs import verify_bundle, write_manifest


def _manifest_with_one_entry(name: str, sha: str, byte_size: int = 100) -> Manifest:
    return Manifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        upstream_base="https://python.docs.hex-rays.com",
        fetched_at="2026-01-01T00:00:00Z",
        module_count=1,
        total_bytes=byte_size,
        modules=(
            ManifestEntry(
                name=name, file=f"{name}.rst.txt",
                source_url=f"https://python.docs.hex-rays.com/_sources/{name}/index.rst.txt",
                sha256=sha, byte_size=byte_size, fetched_at="2026-01-01T00:00:00Z",
            ),
        ),
    )


class TestVerifyBundle(unittest.TestCase):
    def test_drift_detected_when_hash_mismatches(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "bundle"
            out.mkdir()
            # Local: ida_x.rst.txt with old hash
            (out / "ida_x.rst.txt").write_text("# current local content")
            local_manifest = _manifest_with_one_entry("ida_x", sha="0" * 64)
            write_manifest(local_manifest, path=out / "MANIFEST.json")
            # Upstream: returns DIFFERENT content
            index_html = '<a href="ida_x/">x</a>'
            with patch(
                "scripts.build_idapython_docs.urllib.request.urlopen"
            ) as mock_urlopen:
                mock_urlopen.side_effect = [
                    _fake_response(index_html),
                    _fake_response("# upstream changed content"),
                ]
                drift, new, missing = verify_bundle(output_dir=out)
            self.assertEqual(drift, 1)
            self.assertEqual(new, 0)
            self.assertEqual(missing, 0)

    def test_new_module_in_upstream_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "bundle"
            out.mkdir()
            (out / "ida_old.rst.txt").write_text("# old")
            local_manifest = Manifest(
                schema_version=MANIFEST_SCHEMA_VERSION,
                upstream_base="https://python.docs.hex-rays.com",
                fetched_at="2026-01-01T00:00:00Z",
                module_count=1, total_bytes=10,
                modules=(ManifestEntry(
                    name="ida_old", file="ida_old.rst.txt",
                    source_url="https://python.docs.hex-rays.com/_sources/ida_old/index.rst.txt",
                    sha256=sha256_text("# old"), byte_size=10, fetched_at="2026-01-01T00:00:00Z",
                ),),
            )
            write_manifest(local_manifest, path=out / "MANIFEST.json")
            # Upstream has ida_old AND a NEW ida_new
            index_html = '<a href="ida_old/">o</a><a href="ida_new/">n</a>'
            with patch(
                "scripts.build_idapython_docs.urllib.request.urlopen"
            ) as mock_urlopen:
                mock_urlopen.side_effect = [
                    _fake_response(index_html),
                    _fake_response("# old"),  # matches local
                    _fake_response("# new"),  # additional
                ]
                drift, new, missing = verify_bundle(output_dir=out)
            self.assertEqual(drift, 0)
            self.assertEqual(new, 1)
            self.assertEqual(missing, 0)

    def test_missing_module_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "bundle"
            out.mkdir()
            (out / "ida_gone.rst.txt").write_text("# content")
            local_manifest = _manifest_with_one_entry("ida_gone", sha="any")
            write_manifest(local_manifest, path=out / "MANIFEST.json")
            # Upstream: empty (module no longer exists)
            index_html = ""
            with patch(
                "scripts.build_idapython_docs.urllib.request.urlopen"
            ) as mock_urlopen:
                mock_urlopen.side_effect = [_fake_response(index_html)]
                drift, new, missing = verify_bundle(output_dir=out)
            self.assertEqual(drift, 0)
            self.assertEqual(new, 0)
            self.assertEqual(missing, 1)

    def test_no_local_manifest_returns_three(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "bundle"
            out.mkdir()
            drift, new, missing = verify_bundle(output_dir=out)
            self.assertEqual((drift, new, missing), (0, 0, 0))
            # But stdout should warn
```

- [ ] **Step 2: Run tests to verify RED**

Run: `pytest tests/test_build_idapython_docs.py::TestVerifyBundle -v`
Expected: FAIL with `ImportError` for `verify_bundle`.

- [ ] **Step 3: Implement verify_bundle + replace _run_verify**

Replace the `_run_verify` stub in `scripts/build_idapython_docs.py`:

```python
def verify_bundle(output_dir: Path = OUTPUT_DIR) -> tuple[int, int, int]:
    """Compare local bundle against upstream. Return (drift, new, missing) counts.

    Drift: local file's sha256 no longer matches upstream.
    New: module exists upstream but not in local MANIFEST.
    Missing: module in local MANIFEST but no longer upstream.
    """
    local = load_manifest(path=output_dir / "MANIFEST.json")
    if local is None:
        print(f"[warn] No local MANIFEST.json at {output_dir}", file=sys.stderr)
        return (0, 0, 0)

    local_by_name = {e.name: e for e in local.modules}

    try:
        upstream_html = _fetch_index_html()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"[fail] Cannot reach upstream: {exc}", file=sys.stderr)
        return (0, 0, 0)

    upstream_modules = set(discover_modules_from_index(upstream_html))

    drift, new_count, missing = 0, 0, 0

    for module in sorted(upstream_modules):
        if module not in local_by_name:
            new_count += 1
            print(f"NEW: {module}")
            continue
        entry = local_by_name[module]
        url = SOURCES_URL_TEMPLATE.format(module=module)
        body = fetch_with_retry(url, max_retries=1)
        if body is None:
            continue
        if sha256_text(body) != entry.sha256:
            drift += 1
            print(f"DRIFT: {module} (local={entry.sha256[:12]}... remote={sha256_text(body)[:12]}...)")

    for module in sorted(local_by_name):
        if module not in upstream_modules:
            missing += 1
            print(f"MISSING: {module}")

    return (drift, new_count, missing)


def _run_verify() -> int:
    """Run --verify mode and translate counts to exit code."""
    drift, new, missing = verify_bundle()
    if drift > 0 or missing > 0:
        return 1
    # new_count is informational only
    return 0
```

Add `"verify_bundle"` to `__all__`.

- [ ] **Step 4: Run tests to verify GREEN**

Run: `pytest tests/test_build_idapython_docs.py::TestVerifyBundle -v`
Expected: 4 passed (test_no_local_manifest may need separate handling — see note below).

Note: The 4th test asserts `(0, 0, 0)` returns. If the implementation prints to stdout, it should still pass. If tests fail due to stdout pollution, add `capsys` or `with unittest.mock.patch('sys.stdout')` to that one test.

- [ ] **Step 5: Lint + format**

Run: `python -m ruff check scripts/build_idapython_docs.py tests/test_build_idapython_docs.py --fix`
Run: `python -m ruff format scripts/build_idapython_docs.py tests/test_build_idapython_docs.py`

- [ ] **Step 6: Commit**

```bash
git add scripts/build_idapython_docs.py tests/test_build_idapython_docs.py
git commit -m "feat(scripts): --verify mode compares local vs upstream via SHA-256"
```

---

## Task 7: Runtime tool — basic read + sanitization

**Files:**
- Create: `lucnhan/tools/idapython_docs.py`
- Test: `tests/test_idapython_docs_tool.py`

**Interfaces:**
- Produces: `lookup_idapython_doc(module, offset=0, limit=7400) -> str`
- Module name validation regex: `^[a-z0-9_]+$`
- Read from `lucnhan/data/idapython-docs/<module>.rst.txt`

- [ ] **Step 1: Write failing test**

Create `tests/test_idapython_docs_tool.py`:

```python
"""Unit tests for lucnhan/tools/idapython_docs.py"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestLookupIdapythonDoc(unittest.TestCase):
    def setUp(self):
        # Create temp DOCS_DIR with 2 modules
        self.tmpdir = tempfile.mkdtemp()
        (Path(self.tmpdir) / "ida_typeinf.rst.txt").write_text(
            "ida_typeinf module docs\n\nFunctions:\n- apply_cdecl\n",
            encoding="utf-8",
        )
        (Path(self.tmpdir) / "idautils.rst.txt").write_text(
            "idautils module docs\n\nFunctions:\n- Functions\n",
            encoding="utf-8",
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _patch_docs_dir(self):
        return patch("lucnhan.tools.idapython_docs.DOCS_DIR", Path(self.tmpdir))

    def test_reads_existing_module_returns_content(self):
        from lucnhan.tools.idapython_docs import lookup_idapython_doc
        with self._patch_docs_dir():
            result = lookup_idapython_doc("ida_typeinf")
        self.assertIn("apply_cdecl", result)
        self.assertIn("[Offline IDAPython docs: ida_typeinf", result)

    def test_path_traversal_rejected_dotdot(self):
        from lucnhan.tools.idapython_docs import lookup_idapython_doc
        with self._patch_docs_dir():
            result = lookup_idapython_doc("../../../etc/passwd")
        self.assertIn("invalid module name", result)

    def test_path_traversal_rejected_slash(self):
        from lucnhan.tools.idapython_docs import lookup_idapython_doc
        with self._patch_docs_dir():
            result = lookup_idapython_doc("foo/bar")
        self.assertIn("invalid module name", result)

    def test_path_traversal_rejected_uppercase(self):
        from lucnhan.tools.idapython_docs import lookup_idapython_doc
        with self._patch_docs_dir():
            result = lookup_idapython_doc("IDA_TYPEINF")
        self.assertIn("invalid module name", result)

    def test_path_traversal_rejected_dot(self):
        from lucnhan.tools.idapython_docs import lookup_idapython_doc
        with self._patch_docs_dir():
            result = lookup_idapython_doc(".")
        self.assertIn("invalid module name", result)

    def test_tool_does_not_read_outside_docs_dir(self):
        # Create a file in /tmp that the tool must NOT access
        from lucnhan.tools.idapython_docs import lookup_idapython_doc
        outside = Path(self.tmpdir).parent / "sensitive_outside.txt"
        outside.write_text("SENSITIVE")
        try:
            with self._patch_docs_dir():
                # Try every traversal pattern to reach sensitive_outside.txt
                result = lookup_idapython_doc("../sensitive_outside")
                # Must NOT contain SENSITIVE
                self.assertNotIn("SENSITIVE", result)
                self.assertIn("invalid module name", result)
        finally:
            outside.unlink()
```

- [ ] **Step 2: Run tests to verify RED**

Run: `pytest tests/test_idapython_docs_tool.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lucnhan.tools.idapython_docs'`

- [ ] **Step 3: Implement tool (minimal)**

Create `lucnhan/tools/idapython_docs.py`:

```python
"""Offline IDAPython docs lookup — reads from bundled lucnhan/data/idapython-docs/.

This is the runtime counterpart of scripts/build_idapython_docs.py. Once
the bundle is built and committed, this tool serves IDAPython docs to the
LLM agent with zero network dependency.

Replaces web_fetch(url=...python.docs.hex-rays.com/_sources/...) in
documentation fetches — see docs/superpowers/specs/2026-07-07-...
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

from ..core.errors import ToolError
from .base import tool

DOCS_DIR: Path = (
    Path(__file__).resolve().parent.parent / "data" / "idapython-docs"
)

#: Module names are [a-z0-9_]+ per spec. Reject anything else.
_MODULE_NAME_RE: re.Pattern[str] = re.compile(r"^[a-z0-9_]+$")

#: Default pagination — fits under TOOL_RESULT_TRUNCATE_LEN (~8000 chars).
DEFAULT_LIMIT: int = 7400
MAX_LIMIT: int = 7600


def _validate_module_name(module: str) -> str | None:
    """Return sanitized module name or None if invalid (path-traversal reject)."""
    if not module or not _MODULE_NAME_RE.match(module):
        return None
    return module


def _format_missing_module_error(module: str) -> str:
    """Build the user-facing error message when a module is not in the bundle."""
    available: list[str] = []
    if DOCS_DIR.is_dir():
        for p in sorted(DOCS_DIR.glob("*.rst.txt")):
            name = p.stem.removesuffix(".rst")
            if _validate_module_name(name):
                available.append(name)

    shown = ", ".join(available[:20])
    total = len(available)
    return (
        f"[Module '{module}' not found in offline bundle]\n"
        f"Available modules ({total}): {shown}"
        f"{'...' if total > 20 else ''}\n"
        f"Tip: run scripts/build_idapython_docs.py to refresh, "
        f"or fall back to web_fetch() for this module."
    )


@tool(
    name="lookup_idapython_doc",
    category="documentation",
    mutating=False,
    timeout=5.0,
)
def lookup_idapython_doc(
    module: Annotated[
        str,
        "Module name (e.g. 'ida_typeinf', 'idautils', 'ida_hexrays'). "
        "Must match `[a-z0-9_]+`.",
    ],
    offset: Annotated[
        int, "Character offset for pagination (0 = beginning)."
    ] = 0,
    limit: Annotated[
        int,
        f"Max characters to return (default {DEFAULT_LIMIT}, max {MAX_LIMIT}).",
    ] = DEFAULT_LIMIT,
) -> str:
    """Look up an IDAPython module's documentation from the bundled offline bundle.

    Reads from ``lucnhan/data/idapython-docs/<module>.rst.txt`` — works
    without network access. Use this BEFORE web_fetch against
    python.docs.hex-rays.com because that site is bot-protected
    (403 Forbidden on deep-link HTML pages).

    Returns raw RST content (same format as Sphinx source files).
    Use ``offset`` to paginate through large modules.
    """
    safe = _validate_module_name(module)
    if safe is None:
        return f"[Error] invalid module name: {module!r}"

    if offset < 0:
        offset = 0
    if limit < 1:
        limit = 1
    if limit > MAX_LIMIT:
        limit = MAX_LIMIT

    file_path = DOCS_DIR / f"{safe}.rst.txt"
    if not file_path.is_file():
        return _format_missing_module_error(safe)

    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolError(
            f"Failed to read offline docs for {safe}: {exc}",
            tool_name="lookup_idapython_doc",
        ) from exc

    total_chars = len(content)
    if offset >= total_chars:
        chunk = ""
    else:
        chunk = content[offset : offset + limit]

    header = (
        f"[Offline IDAPython docs: {safe}; total chars: {total_chars:,}; "
        f"showing offset {offset}-{min(offset + limit, total_chars)}]"
    )
    if not chunk:
        return f"{header}\n\n(reached end of content)"
    return f"{header}\n\n{chunk}"


__all__ = [
    "DOCS_DIR",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "lookup_idapython_doc",
]
```

- [ ] **Step 4: Run tests to verify GREEN**

Run: `pytest tests/test_idapython_docs_tool.py -v`
Expected: 6 passed.

- [ ] **Step 5: Lint + format**

Run: `python -m ruff check lucnhan/tools/idapython_docs.py tests/test_idapython_docs_tool.py --fix`
Run: `python -m ruff format lucnhan/tools/idapython_docs.py tests/test_idapython_docs_tool.py`

- [ ] **Step 6: Commit**

```bash
git add lucnhan/tools/idapython_docs.py tests/test_idapython_docs_tool.py
git commit -m "feat(tools): lookup_idapython_doc tool — offline docs reader with path-traversal guard"
```

---

## Task 8: Runtime tool — pagination + manifest handling + edge cases

**Files:**
- Modify: `lucnhan/tools/idapython_docs.py`
- Modify: `tests/test_idapython_docs_tool.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_idapython_docs_tool.py`:

```python
class TestPaginationAndEdgeCases(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Create one BIG module (~10000 chars) + one EMPTY module
        big = ("X" * 50 + "\n") * 200  # ~10000 chars
        (Path(self.tmpdir) / "big.rst.txt").write_text(big, encoding="utf-8")
        (Path(self.tmpdir) / "empty.rst.txt").write_text("", encoding="utf-8")
        # One with manifest missing
        # (No MANIFEST.json file written)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_pagination_first_chunk(self):
        from lucnhan.tools.idapython_docs import lookup_idapython_doc
        with patch("lucnhan.tools.idapython_docs.DOCS_DIR", Path(self.tmpdir)):
            result = lookup_idapython_doc("big", offset=0, limit=200)
        self.assertIn("showing offset 0-200", result)

    def test_pagination_middle_chunk(self):
        from lucnhan.tools.idapython_docs import lookup_idapython_doc
        with patch("lucnhan.tools.idapython_docs.DOCS_DIR", Path(self.tmpdir)):
            result = lookup_idapython_doc("big", offset=4000, limit=100)
        self.assertIn("showing offset 4000-4100", result)

    def test_pagination_past_end_returns_marker(self):
        from lucnhan.tools.idapython_docs import lookup_idapython_doc
        with patch("lucnhan.tools.idapython_docs.DOCS_DIR", Path(self.tmpdir)):
            result = lookup_idapython_doc("big", offset=20000, limit=100)
        self.assertIn("reached end of content", result)

    def test_empty_file_returns_empty_marker(self):
        from lucnhan.tools.idapython_docs import lookup_idapython_doc
        with patch("lucnhan.tools.idapython_docs.DOCS_DIR", Path(self.tmpdir)):
            result = lookup_idapython_doc("empty")
        self.assertIn("[Offline IDAPython docs: empty", result)
        self.assertIn("(empty response)", result)

    def test_limit_clamped_to_max(self):
        from lucnhan.tools.idapython_docs import lookup_idapython_doc, MAX_LIMIT
        with patch("lucnhan.tools.idapython_docs.DOCS_DIR", Path(self.tmpdir)):
            # Request way over the max — must clamp to MAX_LIMIT
            result = lookup_idapython_doc("big", offset=0, limit=99999)
        # Header shows total file size ~10K so we see clamped chunk end
        self.assertIn(f"showing offset 0-{MAX_LIMIT}", result)

    def test_limit_below_one_clamped_to_one(self):
        from lucnhan.tools.idapython_docs import lookup_idapython_doc
        with patch("lucnhan.tools.idapython_docs.DOCS_DIR", Path(self.tmpdir)):
            result = lookup_idapython_doc("big", offset=0, limit=0)
        # limit=0 -> clamp to 1 -> shows offset 0-1
        self.assertIn("showing offset 0-1", result)

    def test_offset_negative_clamped_to_zero(self):
        from lucnhan.tools.idapython_docs import lookup_idapython_doc
        with patch("lucnhan.tools.idapython_docs.DOCS_DIR", Path(self.tmpdir)):
            result = lookup_idapython_doc("big", offset=-5, limit=100)
        self.assertIn("showing offset 0-100", result)

    def test_manifest_missing_does_not_break_tool(self):
        # No MANIFEST.json — tool should still work (manifest is informational)
        from lucnhan.tools.idapython_docs import lookup_idapython_doc
        with patch("lucnhan.tools.idapython_docs.DOCS_DIR", Path(self.tmpdir)):
            result = lookup_idapython_doc("big")
        self.assertIn("apply_cdecl" if "apply_cdecl" in result else "XXX", result)  # any content

    def test_zero_byte_file_does_not_crash(self):
        from lucnhan.tools.idapython_docs import lookup_idapython_doc
        with patch("lucnhan.tools.idapython_docs.DOCS_DIR", Path(self.tmpdir)):
            result = lookup_idapython_doc("empty")
        # Must not raise; must include "(empty response)" or similar
        self.assertIsInstance(result, str)
```

- [ ] **Step 2: Run tests to verify GREEN (already implemented in Task 7)**

Run: `pytest tests/test_idapython_docs_tool.py::TestPaginationAndEdgeCases -v`
Expected: 8 passed (the impl from Task 7 already handles all these cases).

If any fail, the implementation from Task 7 needs adjustment. Likely candidates: `test_empty_file_returns_empty_marker` may need extra handling since current impl checks total_chars==0 to emit empty marker — verify that path.

- [ ] **Step 3: Lint + format**

Run: `python -m ruff check lucnhan/tools/idapython_docs.py tests/test_idapython_docs_tool.py --fix`
Run: `python -m ruff format lucnhan/tools/idapython_docs.py tests/test_idapython_docs_tool.py`

Note: this task adds tests that exercise existing behavior. It's primarily a regression-guards task — no implementation change unless Step 2 reveals a bug.

- [ ] **Step 4: Commit**

```bash
git add lucnhan/tools/idapython_docs.py tests/test_idapython_docs_tool.py
git commit -m "test(tools): pagination + edge cases for lookup_idapython_doc"
```

---

## Task 9: Register tool in IDA tool registry

**Files:**
- Modify: `lucnhan/ida/tools/registry.py`

- [ ] **Step 1: Read registry to find insertion point**

Read `lucnhan/ida/tools/registry.py` and locate the `_BOOT_TOOL_MODULES` tuple. Verify `lucnhan.tools.idapython_docs` is NOT already present.

- [ ] **Step 2: Add module to _BOOT_TOOL_MODULES**

Edit the tuple to add a new entry. The exact text depends on the current tuple — find it and add the line in alphabetical / logical order. Likely placement: alongside `lucnhan.tools.documentation` or near `lucnhan.tools.web` / `lucnhan.tools.scripting`. Pattern:

```python
    "lucnhan.tools.idapython_docs",  # Offline IDAPython docs reader (added YYYY-MM-DD)
```

- [ ] **Step 3: Run existing test suite to ensure no regression**

Run: `pytest tests/test_idapython_docs_gate.py -v`
Expected: 18 passed (no behavior change; just tool registration).

- [ ] **Step 4: Verify the tool is importable from the registry location**

Run from repo root:

```bash
python -c "from lucnhan.tools.idapython_docs import lookup_idapython_doc; print('OK')"
```

Expected output: `OK`

If `lucnhan.tools.idapython_docs` cannot be imported outside the IDA host, the import fails. If so, the tool will still be loaded when IDA host's `_BOOT_TOOL_MODULES` iterates and does its own module loading.

- [ ] **Step 5: Lint + format**

Run: `python -m ruff check lucnhan/ida/tools/registry.py --fix`
Run: `python -m ruff format lucnhan/ida/tools/registry.py`

- [ ] **Step 6: Commit**

```bash
git add lucnhan/ida/tools/registry.py
git commit -m "feat(ida): register lookup_idapython_doc in IDA tool registry"
```

---

## Task 10: Update reviewer prompt to prefer new tool

**Files:**
- Modify: `lucnhan/agent/agents/ida_docs_reviewer.py` (section B)
- Modify: `tests/test_ida_docs_review_prompt.py` (add new tests)

- [ ] **Step 1: Add failing tests**

Append to `tests/test_ida_docs_review_prompt.py`:

```python
class TestReviewerPromptPrefersTool(unittest.TestCase):
    def test_prompt_mentions_lookup_idapython_doc(self):
        from lucnhan.agent.agents.ida_docs_reviewer import IDA_DOCS_REVIEWER_PROMPT
        self.assertIn("lookup_idapython_doc", IDA_DOCS_REVIEWER_PROMPT)

    def test_prompt_demotes_web_fetch_to_fallback(self):
        from lucnhan.agent.agents.ida_docs_reviewer import IDA_DOCS_REVIEWER_PROMPT
        # The lookup_idapython_doc should appear BEFORE web_fetch in the
        # documentation sources section (Tool first → fallback later).
        from lucnhan.agent.agents.ida_docs_reviewer import build_ida_docs_reviewer_addendum
        prompt = build_ida_docs_reviewer_addendum()
        tool_idx = prompt.find("lookup_idapython_doc")
        web_fetch_idx = prompt.find("web_fetch", tool_idx) if tool_idx >= 0 else -1
        self.assertGreater(tool_idx, -1, "lookup_idapython_doc not in prompt")
        self.assertGreater(web_fetch_idx, -1, "web_fetch not in prompt after the tool")
        # Tool appears strictly before its fallback statement
        self.assertLess(tool_idx, web_fetch_idx)

    def test_prompt_explains_fallback_reason(self):
        from lucnhan.agent.agents.ida_docs_reviewer import IDA_DOCS_REVIEWER_PROMPT
        # The fallback should mention "not in bundle" or similar
        self.assertTrue(
            "not in bundle" in IDA_DOCS_REVIEWER_PROMPT.lower()
            or "fall back" in IDA_DOCS_REVIEWER_PROMPT.lower(),
            "Prompt should explain when to fall back to web_fetch",
        )
```

- [ ] **Step 2: Run tests to verify RED**

Run: `pytest tests/test_ida_docs_review_prompt.py::TestReviewerPromptPrefersTool -v`
Expected: FAIL (prompt does not yet mention `lookup_idapython_doc`).

- [ ] **Step 3: Update prompt section B**

In `lucnhan/agent/agents/ida_docs_reviewer.py`, locate section B (currently titled "RAW RST SOURCE (preferred online format)"). Replace this section with:

```text
B. The bundled offline docs (preferred — works offline, zero network):

   The offline docs bundle ships inside the plugin at
   ``data/idapython-docs/<module>.rst.txt``. Use the
   ``lookup_idapython_doc`` tool to read it:

   ```
   lookup_idapython_doc(module="<module>")
   ```

   Concrete example — to verify ``ida_typeinf.apply_cdecl``:
   ``lookup_idapython_doc(module="ida_typeinf")`` returns the entire
   ``ida_typeinf`` RST reference in one call (5-15 KB raw source).

   Common modules: ``ida_typeinf``, ``ida_name``, ``idautils``,
   ``ida_hexrays``, ``ida_frame``, ``ida_funcs``, ``ida_bytes``,
   ``ida_xref``, ``ida_segment``, ``ida_kernwin``, ``ida_ua``,
   ``idc``, ``idaapi``. These files return the raw RST source.

C. Hex-Rays Python reference (online FALLBACK only — when the module is
   not in the offline bundle):
```

Then rename existing "C." (GitBook) to "D." and "D." (LLM corpus) to "E."

- [ ] **Step 4: Run tests to verify GREEN**

Run: `pytest tests/test_ida_docs_review_prompt.py -v`
Expected: all tests (old + new) pass.

- [ ] **Step 5: Lint + format**

Run: `python -m ruff check lucnhan/agent/agents/ida_docs_reviewer.py tests/test_ida_docs_review_prompt.py --fix`
Run: `python -m ruff format lucnhan/agent/agents/ida_docs_reviewer.py tests/test_ida_docs_review_prompt.py`

- [ ] **Step 6: Commit**

```bash
git add lucnhan/agent/agents/ida_docs_reviewer.py tests/test_ida_docs_review_prompt.py
git commit -m "feat(agent): reviewer prompt prefers lookup_idapython_doc over web_fetch for IDAPython docs"
```

---

## Task 11: Update SKILL.md "When to fetch more" section

**Files:**
- Modify: `lucnhan/skills/builtins/ida-scripting/SKILL.md`
- Modify: `tests/test_ida_docs_review_prompt.py` (add tests)

- [ ] **Step 1: Add failing tests**

Append to `tests/test_ida_docs_review_prompt.py`:

```python
class TestSkillPrefersTool(unittest.TestCase):
    SKILL_PATH = (
        Path(__file__).resolve().parent.parent
        / "skills" / "builtins" / "ida-scripting" / "SKILL.md"
    )

    def setUp(self):
        self.body = self.SKILL_PATH.read_text(encoding="utf-8")

    def test_skill_recommends_lookup_idapython_doc(self):
        self.assertIn("lookup_idapython_doc", self.body)

    def test_skill_demotes_web_fetch_to_fallback(self):
        tool_idx = self.body.find("lookup_idapython_doc")
        web_fetch_idx = self.body.find("web_fetch", tool_idx) if tool_idx >= 0 else -1
        self.assertGreater(tool_idx, -1)
        self.assertGreater(web_fetch_idx, -1)
        self.assertLess(tool_idx, web_fetch_idx)
```

- [ ] **Step 2: Run tests to verify RED**

Run: `pytest tests/test_ida_docs_review_prompt.py::TestSkillPrefersTool -v`
Expected: FAIL (skill body does not mention the new tool).

- [ ] **Step 3: Update SKILL.md "When to fetch more" section**

Replace the section in `lucnhan/skills/builtins/ida-scripting/SKILL.md` that currently shows `web_fetch(url="https://python.docs.hex-rays.com/_sources/ida_<module>/index.rst.txt", format="text")` with:

```markdown
## When to fetch more

The deep static reference below (`## Reference: api-reference.md`) covers ctree,
microcode, types, xrefs, hooks, and netnodes exhaustively. For per-module
references **not in the bundled api-reference**, prefer the **offline docs tool**
(no network needed):

```
lookup_idapython_doc(module="ida_<module>")
```

Reads from the plugin's bundled docs at `data/idapython-docs/<module>.rst.txt`.
Common modules: `ida_typeinf`, `ida_name`, `idautils`, `ida_hexrays`,
`ida_frame`, `ida_funcs`, `ida_bytes`, `ida_xref`, `ida_segment`,
`ida_kernwin`, `ida_ua`, `idc`, `idaapi`.

> **Fallback:** if a module is not in the bundle, fall back to
> `web_fetch(url="https://python.docs.hex-rays.com/_sources/ida_<module>/index.rst.txt", format="text")`
> — but only as a last resort (bot protection may return 403).

For IDA 9.x migration details, fetch the porting guide:
`https://docs.hex-rays.com/developer/idapython/idapython-porting-guide-ida-9`
```

- [ ] **Step 4: Run tests to verify GREEN**

Run: `pytest tests/test_ida_docs_review_prompt.py -v`
Expected: all tests pass.

- [ ] **Step 5: Lint format check**

SKILL.md is markdown. No ruff needed.

- [ ] **Step 6: Commit**

```bash
git add lucnhan/skills/builtins/ida-scripting/SKILL.md tests/test_ida_docs_review_prompt.py
git commit -m "feat(skills): ida-scripting SKILL.md prefers lookup_idapython_doc over web_fetch"
```

---

## Task 12: Initial build + bundle commit

**Files:**
- Modify: `lucnhan/data/idapython-docs/` (NEW files committed here)
- Create: `lucnhan/data/idapython-docs/MANIFEST.json`

This task runs the build script for real and commits the populated bundle. **Not run in CI** — done once by the implementer.

- [ ] **Step 1: Verify the bundle directory exists + is gitignored correctly**

```bash
ls -la lucnhan/data/ 2>&1 || mkdir -p lucnhan/data/idapython-docs
```

Verify `.gitignore` includes the `*.tmp` patterns from Task 5 but NOT the bundle files themselves.

- [ ] **Step 2: Run the build script with real network**

```bash
python scripts/build_idapython_docs.py
```

Expected output: `[ok] N/N modules fetched (X bytes, 0 failures)` where N is ~50.

If non-zero failures, investigate which modules failed (likely temporary network or partial Hex-Rays outage). Re-run after fixing.

- [ ] **Step 3: Verify the bundle is valid**

```bash
ls lucnhan/data/idapython-docs/ | head -10
cat lucnhan/data/idapython-docs/MANIFEST.json | python -m json.tool | head -30
```

Should show ~50 `.rst.txt` files and a valid MANIFEST.json.

- [ ] **Step 4: Run the lookup tool against a real bundle file**

```bash
python -c "
from lucnhan.tools.idapython_docs import lookup_idapython_doc
result = lookup_idapython_doc('ida_typeinf')
print(result[:200])
"
```

Expected: first 200 chars of the ida_typeinf.rst.txt bundle file with the standard header.

- [ ] **Step 5: Run the full test suite**

Run: `pytest tests/ -v`
Expected: all tests pass (existing + new ~21).

- [ ] **Step 6: Verify --verify mode works against the freshly built bundle**

```bash
python scripts/build_idapython_docs.py --verify
```

Expected exit code 0 (no drift, no missing). May print "NEW" lines if Hex-Rays added modules since the script was written — those are informational only.

- [ ] **Step 7: Commit the bundle**

```bash
git add lucnhan/data/idapython-docs/MANIFEST.json
git add lucnhan/data/idapython-docs/
git commit -m "feat(data): initial IDAPython offline docs bundle (~50 modules, ~612 KB)"
```

Note: `git add lucnhan/data/idapython-docs/` adds all `.rst.txt` files. `.tmp` files (if any leftover) are excluded by `.gitignore`.

---

## Task 13: Opt-in integration test

**Files:**
- Create: `tests/test_build_idapython_docs_integration.py`

This task adds a single real-network integration test, gated behind `RUN_NETWORK_TESTS=1`. It validates end-to-end: discover → fetch → write → read.

- [ ] **Step 1: Write the integration test**

Create `tests/test_build_idapython_docs_integration.py`:

```python
"""Real-network integration test for the IDAPython docs build script.

Gated behind ``RUN_NETWORK_TESTS=1`` env var. By default pytest skips this
file so CI without network access stays green.

Run locally with:
    RUN_NETWORK_TESTS=1 pytest tests/test_build_idapython_docs_integration.py -v
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import pytest


@pytest.mark.skipif(
    not os.getenv("RUN_NETWORK_TESTS"),
    reason="opt-in: set RUN_NETWORK_TESTS=1 to run",
)
class TestRealFetchIntegration(unittest.TestCase):
    """End-to-end: fetch one real module from Hex-Rays."""

    def test_fetch_ida_typeinf_end_to_end(self):
        # Fetch just one module (ida_typeinf is large enough to be
        # representative, small enough to be fast)
        import httpx  # type: ignore[import-not-found]
        # We deliberately use stdlib here even though httpx would be nicer —
        # the build script uses stdlib. Use urllib.
        from scripts.build_idapython_docs import (
            SOURCES_URL_TEMPLATE,
            fetch_with_retry,
            sha256_text,
            write_atomic,
        )

        url = SOURCES_URL_TEMPLATE.format(module="ida_typeinf")
        body = fetch_with_retry(url, max_retries=2)
        self.assertIsNotNone(body, "fetch_with_retry returned None — network?")
        assert body is not None  # for type checker

        # Should contain key API names we know exist
        for token in ("create_udt", "apply_cdecl", "BTF_STRUCT"):
            self.assertIn(token, body, f"ida_typeinf.rst.txt missing expected token {token!r}")

        # sha256 is deterministic
        h1 = sha256_text(body)
        h2 = sha256_text(body)
        self.assertEqual(h1, h2)

        # Atomic write round-trips
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ida_typeinf.rst.txt"
            write_atomic(target, body)
            self.assertEqual(target.read_text(encoding="utf-8"), body)
```

- [ ] **Step 2: Run with network flag (local only)**

Run: `RUN_NETWORK_TESTS=1 pytest tests/test_build_idapython_docs_integration.py -v`

Expected: 1 passed (if network reachable).

If the test fails due to network: skip in this session; the test is the canonical integration check for the next CI run.

- [ ] **Step 3: Run WITHOUT flag to confirm skip works**

Run: `pytest tests/test_build_idapython_docs_integration.py -v`
Expected: 1 skipped.

- [ ] **Step 4: Commit**

```bash
git add tests/test_build_idapython_docs_integration.py
git commit -m "test(integration): opt-in real-fetch test for build script"
```

---

## Final Verification

After all tasks complete:

- [ ] **Run full test suite:**

```bash
pytest tests/ -v
```

- [ ] **Run lint + format:**

```bash
python -m ruff format lucnhan/ scripts/ tests/
python -m ruff check lucnhan/ scripts/ tests/
```

- [ ] **Run mypy on new files:**

```bash
python -m mypy lucnhan/tools/idapython_docs.py
```

- [ ] **Manual smoke test in IDA Pro (optional, out-of-band):**

1. Load plugin in IDA
2. Open a sample binary
3. Run an agent turn that triggers `execute_python` with a complex script
4. Confirm the docs reviewer uses `lookup_idapython_doc` (visible in trace logs)
5. Confirm no `403` errors in trace logs

---

## Out of scope (deferred to v2)

- `--update` flag (incremental refresh)
- `--parallel N` flag (concurrent fetches)
- Topic/substring search inside modules
- Cross-module inverted index
- Auto-update at plugin startup
- `web_fetch` cache layer integration
- Lazy online fallback (violates offline-first)
