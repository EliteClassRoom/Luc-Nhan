"""Build the offline IDAPython docs bundle from Hex-Rays upstream.

Runs at dev/CI time (NOT inside IDA). Produces raw RST files + MANIFEST.json
under lucnhan/data/idapython-docs/. See:
docs/superpowers/specs/2026-07-07-idapython-offline-docs-design.md
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants (no magic numbers; values from spec section Components)
#
# NOTE: This module is built up incrementally across tasks 1-6.  Only the
# constants and imports needed for the current task are present; later
# tasks (fetch/manifest/build/verify) will add urllib, hashlib, json, etc.
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

# Module names are [a-z0-9_]+ per spec; we accept that here. The Sphinx
# index page contains <a href="ida_typeinf/"> entries (plain format) and
# <a class="reference internal" href="ida_typeinf/index.html"> entries
# (Sphinx-emitted format with optional anchor fragment). Both forms must
# be discovered; the optional `(?:/index\.html)?(?:#[^"]*)?/?` suffix
# group covers both the bare trailing slash and the explicit index page.
_MODULE_LINK_RE: re.Pattern[str] = re.compile(r'<a[^>]*href="(?P<module>[a-z0-9_]+)(?:/index\.html)?(?:#[^"]*)?/?"')


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


def fetch_with_retry(
    url: str,
    max_retries: int = MAX_RETRIES,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> str | None:
    """Fetch ``url`` with exponential backoff on transient network errors.

    Retryable errors (caught and retried up to ``max_retries`` times):
        ``socket.timeout``, ``ConnectionError``, ``TimeoutError``,
        ``urllib.error.URLError`` (network-level). HTTP 5xx is also
        treated as transient and retried.

    Non-retryable (return ``None`` immediately, no retry):
        ``urllib.error.HTTPError`` with a 4xx status — the module path is
        genuinely wrong and retrying will not help.

    Args:
        url: URL to fetch.
        max_retries: Maximum number of attempts before giving up.
        timeout: Per-request timeout in seconds.

    Returns:
        Response body as a UTF-8 string on success, or ``None`` if all
        attempts fail. This function does NOT raise — it logs warnings to
        stderr and returns ``None`` so the build loop can continue.
    """
    last_error: BaseException | None = None
    backoff = INITIAL_BACKOFF_SECONDS
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
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


# ---------------------------------------------------------------------------
# Manifest dataclasses + JSON I/O — Task 4 deliverable
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Build loop + CLI — Task 5 deliverable
# ---------------------------------------------------------------------------


def _fetch_index_html(timeout: float = HTTP_TIMEOUT_SECONDS) -> str:
    """Fetch and decode the Hex-Rays module index HTML page.

    Raises urllib.error.URLError on network failure so caller can decide.
    """
    with urllib.request.urlopen(UPSTREAM_INDEX_URL, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def build_bundle(
    output_dir: Path = OUTPUT_DIR,
    *,
    now: str | None = None,
    max_retries: int = MAX_RETRIES,
) -> tuple[int, int]:
    """Fetch all modules from upstream and write bundle to ``output_dir``.

    Returns:
        ``(success_count, failed_count)`` where both ints >= 0 on a
        successful run. Returns the sentinel ``(-1, -1)`` when the
        upstream index page returned HTTP 403 (Hex-Rays CDN blocking
        the build script's IP) — ``main()`` maps that sentinel to
        process exit 2 with the spec-mandated error message.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = now or (datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))

    try:
        index_html = _fetch_index_html()
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            print(
                "Hex-Rays returned 403 on the module index. Try again later or run from a different IP/CDN.",
                file=sys.stderr,
            )
            return (-1, -1)
        raise
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
        return _run_verify()

    success, failed = build_bundle()
    # Sentinel: build_bundle() returned (-1, -1) for upstream index 403.
    # Per spec, this is a distinct fatal condition (Hex-Rays CDN blocks
    # our IP) — separate exit code from "no modules fetched" or "partial
    # failures". The error message has already been printed to stderr by
    # build_bundle(), so we only translate the sentinel to exit code.
    if success < 0 and failed < 0:
        return 2
    if failed > 0:
        return 1
    if success == 0:
        return 2  # Nothing succeeded
    return 0


def verify_bundle(output_dir: Path = OUTPUT_DIR) -> tuple[int, int, int, bool]:
    """Compare local bundle against upstream.

    Returns:
        ``(drift, new, missing, network_ok)`` 4-tuple.

        - ``drift``: local file's sha256 no longer matches upstream.
        - ``new``: module exists upstream but not in local MANIFEST.
        - ``missing``: module in local MANIFEST but no longer upstream.
        - ``network_ok``: ``False`` when upstream was unreachable
          (``URLError`` / ``TimeoutError`` / ``OSError`` while fetching
          the index HTML, or when no local MANIFEST exists). Callers
          MUST treat ``network_ok=False`` as a distinct failure mode
          from "no drift" — exit codes need to differentiate "could not
          reach upstream" from "everything is up to date".
    """
    local = load_manifest(path=output_dir / "MANIFEST.json")
    if local is None:
        print(f"[warn] No local MANIFEST.json at {output_dir}", file=sys.stderr)
        return (0, 0, 0, False)

    local_by_name = {e.name: e for e in local.modules}

    try:
        upstream_html = _fetch_index_html()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"[fail] Cannot reach upstream: {exc}", file=sys.stderr)
        return (0, 0, 0, False)

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

    return (drift, new_count, missing, True)


def _run_verify() -> int:
    """Run ``--verify`` mode and translate counts to process exit code.

    Exit codes:
        - ``0`` — local bundle is in sync with upstream.
        - ``1`` — local bundle has drift or missing modules.
        - ``2`` — upstream was unreachable; we cannot make a claim about
          drift/missing status (this is distinct from exit 1 and must not
          be conflated with "no drift"). False confidence from a silent
          network failure is the bug Fix 1 prevents.
    """
    drift, _new, missing, network_ok = verify_bundle()
    if not network_ok:
        return 2
    if drift > 0 or missing > 0:
        return 1
    # new_count is informational only, does not fail verify
    return 0


__all__ = [
    "BACKOFF_MULTIPLIER",
    "BASE_URL",
    "HTTP_TIMEOUT_SECONDS",
    "INITIAL_BACKOFF_SECONDS",
    "MANIFEST_PATH",
    "MANIFEST_SCHEMA_VERSION",
    "MAX_RETRIES",
    "OUTPUT_DIR",
    "SOURCES_URL_TEMPLATE",
    "UPSTREAM_INDEX_URL",
    "Manifest",
    "ManifestEntry",
    "build_bundle",
    "discover_modules_from_index",
    "fetch_with_retry",
    "load_manifest",
    "main",
    "sha256_text",
    "verify_bundle",
    "write_atomic",
    "write_manifest",
]
