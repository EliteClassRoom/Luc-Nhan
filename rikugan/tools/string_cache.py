"""Persistent raw NoSQL-style cache for binary strings.

This module is host-agnostic — it must NOT import any ``ida_*`` modules.
The IDA tool layer (``rikugan/ida/tools/strings.py``) supplies the raw
records; this module persists them as plain JSONL / JSON files under
the user Rikugan config cache directory and serves list / search / get_at
operations without re-enumerating ``idautils.Strings()``.

Layout under ``<cache_dir>/strings/<cache_key>/v1/``:

- ``meta.json``       — schema_version + identity + counts, written last
- ``strings.jsonl``   — one document per string (``i``, ``ea``, ``length``, ``text``, ``text_lc``)
- ``addr_index.json`` — ``{hex_ea: document_id}`` for ``get_string_at`` fast path
- ``grams/<shard>.jsonl`` — lowercase trigram postings sharded by the first
  two safe characters of the gram.

The cache key is ``db_instance_id`` when available, otherwise a deterministic
hash of the normalized IDB path.  When neither is available, helpers return
``None`` and the caller falls back to non-persistent behavior.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from ..constants import STRING_CACHE_SCHEMA_VERSION
from ..core.atomic_io import atomic_replace
from ..core.host import get_database_instance_id, get_database_path
from ..core.logging import log_debug, log_warning
from ..core.sanitize import strip_injection_markers

# Trigram size used for the inverted index.
_TRIGRAM_SIZE = 3

# Maximum number of candidates we'll verify before giving up on a query.
# Prevents pathological common-gram blow-up on huge binaries.
_MAX_VERIFY_CANDIDATES = 200_000

# Hard bounds applied to on-disk artifacts to prevent tampered/oversized
# caches from blowing up memory or stalling the agent.  These are read-side
# limits — they do not affect legitimate caches of any realistic binary.
_MAX_ADDR_INDEX_ENTRIES = 10_000_000  # 10M addresses
_MAX_ADDR_INDEX_FILE_BYTES = 512 * 1024 * 1024  # 512 MB
_MAX_SHARD_FILE_BYTES = 256 * 1024 * 1024  # 256 MB per gram shard
_MAX_SHARD_LINE_BYTES = 4 * 1024 * 1024  # 4 MB per shard line
_MAX_POSTING_IDS_PER_GRAM = 10_000_000  # 10M ids per trigram posting list
_MAX_TRIGRAMS_PER_QUERY = 256  # safety cap on the grams set


@dataclass(frozen=True)
class StringRecord:
    """In-memory representation of one cached string."""

    i: int  # document id (line index in strings.jsonl)
    ea: int  # effective address
    length: int  # original string length as reported by IDA
    text: str  # the raw string text (already sanitized for cache writes)


# ---------------------------------------------------------------------------
# Cache key derivation
# ---------------------------------------------------------------------------


def _normalize_db_path(path: str) -> str:
    """Stable canonical form of an IDB path for cache key derivation."""
    if not path:
        return ""
    try:
        return os.path.normcase(os.path.realpath(os.path.abspath(path)))
    except OSError:
        return path


def derive_cache_key(idb_path: str = "", db_instance_id: str = "") -> str:
    """Return a filesystem-safe cache key for the active database.

    Prefers ``db_instance_id`` (stable across path moves); falls back to a
    SHA-256 digest of the normalized IDB path.  Returns ``""`` when neither
    is available — callers must handle this case (no persistent cache).
    """
    if db_instance_id:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", db_instance_id)[:96]
        if safe:
            return f"idb-{safe}"
    norm = _normalize_db_path(idb_path)
    if norm:
        digest = hashlib.sha256(norm.encode("utf-8", errors="replace")).hexdigest()[:32]
        return f"path-{digest}"
    return ""


def resolve_active_cache_key() -> tuple[str, str, str]:
    """Return ``(cache_key, idb_path, db_instance_id)`` from host helpers.

    Returns ``("", "", "")`` when no usable identity is available.
    """
    idb_path = get_database_path()
    db_instance_id = get_database_instance_id()
    return derive_cache_key(idb_path, db_instance_id), idb_path, db_instance_id


# ---------------------------------------------------------------------------
# Filesystem paths
# ---------------------------------------------------------------------------


def cache_root_dir(cache_dir: str) -> str:
    """Return ``<cache_dir>/strings`` — root for all per-IDB string caches."""
    return os.path.join(cache_dir, "strings")


def cache_version_dir(cache_dir: str, cache_key: str) -> str:
    """Return ``<cache_dir>/strings/<cache_key>/v1`` — current cache layout."""
    return os.path.join(cache_root_dir(cache_dir), cache_key, "v1")


def grams_dir(version_dir: str) -> str:
    return os.path.join(version_dir, "grams")


# ---------------------------------------------------------------------------
# Gram helpers
# ---------------------------------------------------------------------------


def _safe_gram_shard(gram: str) -> str:
    """Return a filesystem-safe shard name for *gram*.

    The first two characters of a lowercase trigram are usually letters or
    digits; we still hash the full gram and combine with a sanitized prefix
    so filenames are short, deterministic, and portable.
    """
    if not gram:
        return "_.jsonl"
    prefix_src = gram[:2]
    prefix = "".join(c if c.isalnum() or c in "-_" else "_" for c in prefix_src)
    digest = hashlib.sha1(gram.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"{prefix}-{digest}.jsonl"


def _extract_trigrams(text_lc: str) -> set[str]:
    """Return the unique lowercase trigrams contained in *text_lc*.

    Yields an empty set for strings shorter than 3 characters — short strings
    are handled by the cached-document scan fallback, not the inverted index.
    """
    if len(text_lc) < _TRIGRAM_SIZE:
        return set()
    return {text_lc[i : i + _TRIGRAM_SIZE] for i in range(len(text_lc) - _TRIGRAM_SIZE + 1)}


# ---------------------------------------------------------------------------
# Atomic file I/O
# ---------------------------------------------------------------------------


def _atomic_write_text(path: str, text: str) -> None:
    """Write *text* to *path* atomically using temp-file + ``os.replace``."""
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".sc_tmp_", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        atomic_replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_jsonl(path: str, records: Iterable[dict[str, Any]]) -> None:
    """Write one JSON object per line to *path* atomically."""
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".sc_tmp_", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
                f.write("\n")
        atomic_replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_json(path: str, obj: Any) -> None:
    """Write *obj* as JSON to *path* atomically."""
    _atomic_write_text(path, json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


# ---------------------------------------------------------------------------
# Cache index API
# ---------------------------------------------------------------------------


class StringCacheIndex:
    """Read-only view of a fully-built string cache directory.

    Constructed by :func:`get_or_build_string_cache` once the on-disk cache
    is verified (or freshly built).  Provides list / search / get_at APIs
    served entirely from disk.
    """

    def __init__(
        self,
        version_dir: str,
        cache_key: str,
        idb_path: str,
        db_instance_id: str,
        total: int,
    ) -> None:
        self.version_dir = version_dir
        self.cache_key = cache_key
        self.idb_path = idb_path
        self.db_instance_id = db_instance_id
        self.total = total
        self._strings_path = os.path.join(version_dir, "strings.jsonl")
        self._addr_index_path = os.path.join(version_dir, "addr_index.json")
        self._meta_path = os.path.join(version_dir, "meta.json")
        self._grams_dir = grams_dir(version_dir)

    @property
    def meta_path(self) -> str:
        return self._meta_path

    # ------------------------------------------------------------------
    # list / pagination
    # ------------------------------------------------------------------

    def list(
        self,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[StringRecord], int]:
        """Return ``(page, total)`` from the cached string documents."""
        offset = max(0, int(offset))
        limit = max(1, int(limit))
        if offset >= self.total:
            return [], self.total
        page: list[StringRecord] = []
        try:
            with open(self._strings_path, encoding="utf-8", errors="replace") as f:
                # Skip to offset
                for _ in range(offset):
                    line = f.readline()
                    if not line:
                        return page, self.total
                while len(page) < limit:
                    line = f.readline()
                    if not line:
                        break
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    page.append(
                        StringRecord(
                            i=int(rec.get("i", 0)),
                            ea=int(rec.get("ea", 0)),
                            length=int(rec.get("length", 0)),
                            text=strip_injection_markers(str(rec.get("text", ""))),
                        )
                    )
        except OSError as e:
            log_warning(f"StringCacheIndex.list failed: {e}")
            return page, self.total
        return page, self.total

    # ------------------------------------------------------------------
    # search via n-gram postings
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 20) -> list[StringRecord]:
        """Case-insensitive substring search served from the n-gram index.

        Short queries (``len(query) < 3``) skip the inverted index and scan
        ``strings.jsonl`` directly because trigrams do not apply.
        """
        limit = max(1, int(limit))
        q_lc = (query or "").lower()
        if not q_lc:
            return []
        if len(q_lc) < _TRIGRAM_SIZE:
            return self._scan_for_substring(q_lc, limit)
        grams = _extract_trigrams(q_lc)
        # Bound the gram set so a pathological query cannot amplify work.
        if len(grams) > _MAX_TRIGRAMS_PER_QUERY:
            grams = set(sorted(grams)[:_MAX_TRIGRAMS_PER_QUERY])
        if not grams:
            return self._scan_for_substring(q_lc, limit)
        candidates = self._candidate_ids_for_grams(grams)
        if not candidates:
            return []
        if len(candidates) > _MAX_VERIFY_CANDIDATES:
            # Best-effort: take the lowest ids first (more likely to be
            # deterministic / well-formed); verification still bounds work.
            candidates = set(sorted(candidates)[:_MAX_VERIFY_CANDIDATES])
        results = self._verify_candidates(candidates, q_lc, limit)
        return results

    def _scan_for_substring(self, q_lc: str, limit: int) -> list[StringRecord]:
        """Linear scan fallback for short queries."""
        results: list[StringRecord] = []
        try:
            with open(self._strings_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    text_lc = str(rec.get("text_lc", ""))
                    if q_lc in text_lc:
                        results.append(
                            StringRecord(
                                i=int(rec.get("i", 0)),
                                ea=int(rec.get("ea", 0)),
                                length=int(rec.get("length", 0)),
                                text=strip_injection_markers(str(rec.get("text", ""))),
                            )
                        )
                        if len(results) >= limit:
                            break
        except OSError as e:
            log_warning(f"StringCacheIndex._scan_for_substring failed: {e}")
        results.sort(key=lambda r: r.ea)
        return results

    def _candidate_ids_for_grams(self, grams: set[str]) -> set[int]:
        """Intersect posting lists for the requested grams.

        We always load the smallest posting list first, then intersect the
        remaining postings against it to keep memory bounded for common
        grams like ``"   "`` or letter triples.
        """
        postings: list[set[int]] = []
        for gram in grams:
            shard_name = _safe_gram_shard(gram)
            shard_path = os.path.join(self._grams_dir, shard_name)
            if not os.path.exists(shard_path):
                continue
            # Cap shard size: a tampered or runaway shard cannot blow up
            # memory; treat oversize as if the gram has no postings.
            try:
                if os.path.getsize(shard_path) > _MAX_SHARD_FILE_BYTES:
                    log_warning(f"StringCacheIndex: skipping oversize shard {shard_path}")
                    continue
            except OSError:
                continue
            ids = self._read_posting_shard(shard_path, gram)
            if len(ids) > _MAX_POSTING_IDS_PER_GRAM:
                log_warning(f"StringCacheIndex: truncating oversize posting list for '{gram}'")
                ids = set(sorted(ids)[:_MAX_POSTING_IDS_PER_GRAM])
            postings.append(ids)
        if not postings:
            return set()
        # Smallest-first intersection to keep memory bounded.
        postings.sort(key=len)
        result = set(postings[0])
        for other in postings[1:]:
            result.intersection_update(other)
            if not result:
                return result
        return result

    def _read_posting_shard(self, shard_path: str, gram: str) -> set[int]:
        """Read posting ids for *gram* from *shard_path*.

        A shard file may contain multiple grams (because we hash the full
        gram into the filename and use a prefix for grouping); we filter
        to the requested gram here.  Individual lines are bounded in size
        to prevent tampered files from exhausting memory.
        """
        ids: set[int] = set()
        try:
            with open(shard_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if len(line) > _MAX_SHARD_LINE_BYTES:
                        log_warning(f"StringCacheIndex: skipping oversize line in {shard_path}")
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("g") != gram:
                        continue
                    for raw in rec.get("ids", ()):
                        try:
                            ids.add(int(raw))
                        except (TypeError, ValueError):
                            continue
                    break  # grams appear once per shard
        except OSError as e:
            log_warning(f"StringCacheIndex: failed to read shard {shard_path}: {e}")
        return ids

    def _verify_candidates(
        self,
        candidates: Iterable[int],
        q_lc: str,
        limit: int,
    ) -> list[StringRecord]:
        """Verify candidate document ids contain *q_lc* and return matches."""
        # Index lookup via line offset is more memory-friendly than reading
        # everything when the candidate set is large.  We use a small index
        # built lazily here from the JSONL (one pass is cheap relative to
        # the build cost).
        sorted_ids = sorted(candidates)
        results: list[StringRecord] = []
        try:
            with open(self._strings_path, encoding="utf-8", errors="replace") as f:
                # Single pass, advance a counter and emit when we hit a candidate id.
                target = set(sorted_ids)
                current = 0
                for line in f:
                    if current in target:
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            current += 1
                            continue
                        text_lc = str(rec.get("text_lc", ""))
                        if q_lc in text_lc:
                            results.append(
                                StringRecord(
                                    i=current,
                                    ea=int(rec.get("ea", 0)),
                                    length=int(rec.get("length", 0)),
                                    text=strip_injection_markers(str(rec.get("text", ""))),
                                )
                            )
                            if len(results) >= limit:
                                break
                    current += 1
                    if current > max(target, default=0):
                        break
        except OSError as e:
            log_warning(f"StringCacheIndex._verify_candidates failed: {e}")
        results.sort(key=lambda r: r.ea)
        return results

    # ------------------------------------------------------------------
    # get_at
    # ------------------------------------------------------------------

    def get_at(self, ea: int) -> StringRecord | None:
        """Return the cached string at *ea* if present, else None.

        The on-disk ``addr_index.json`` is treated as untrusted: a tampered
        or runaway file is rejected before allocation.
        """
        # Cap file size before loading to prevent a tampered artifact from
        # exhausting memory on the agent host.
        try:
            if os.path.getsize(self._addr_index_path) > _MAX_ADDR_INDEX_FILE_BYTES:
                log_warning("StringCacheIndex.get_at: addr_index oversize, skipped")
                return None
        except OSError:
            return None
        try:
            with open(self._addr_index_path, encoding="utf-8", errors="replace") as f:
                idx = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(idx, dict):
            return None
        if len(idx) > _MAX_ADDR_INDEX_ENTRIES:
            log_warning("StringCacheIndex.get_at: addr_index oversize, skipped")
            return None
        target = f"0x{ea:x}"
        doc_id = idx.get(target)
        if not isinstance(doc_id, int):
            return None
        try:
            with open(self._strings_path, encoding="utf-8", errors="replace") as f:
                for _ in range(doc_id):
                    if not f.readline():
                        return None
                line = f.readline()
        except OSError:
            return None
        if not line:
            return None
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            return None
        return StringRecord(
            i=doc_id,
            ea=int(rec.get("ea", ea)),
            length=int(rec.get("length", 0)),
            text=strip_injection_markers(str(rec.get("text", ""))),
        )


# ---------------------------------------------------------------------------
# Build & cache lifecycle
# ---------------------------------------------------------------------------

# Process-local lock registry keyed by cache_key so concurrent builds of the
# same IDB never trample each other within one process.
_BUILD_LOCKS: dict[str, threading.RLock] = {}
_BUILD_LOCKS_GUARD = threading.Lock()


def _get_build_lock(cache_key: str) -> threading.RLock:
    with _BUILD_LOCKS_GUARD:
        lock = _BUILD_LOCKS.get(cache_key)
        if lock is None:
            lock = threading.RLock()
            _BUILD_LOCKS[cache_key] = lock
        return lock


def _safe_rmtree(path: str) -> None:
    """Best-effort recursive removal of *path*."""
    try:
        shutil.rmtree(path)
    except OSError as e:
        log_warning(f"Failed to remove partial cache {path}: {e}")


def _is_cache_complete(version_dir: str) -> tuple[bool, int]:
    """Return ``(complete, total)`` for the on-disk cache.

    Complete means ``meta.json``, ``strings.jsonl``, and ``addr_index.json``
    all exist and parse cleanly.  Total comes from ``meta.json`` when valid.
    Also enforces that ``meta.json`` and the on-disk corpus agree, so a
    tampered/leftover ``meta.json`` cannot validate mismatched files.
    """
    meta_path = os.path.join(version_dir, "meta.json")
    strings_path = os.path.join(version_dir, "strings.jsonl")
    addr_path = os.path.join(version_dir, "addr_index.json")
    if not (os.path.exists(meta_path) and os.path.exists(strings_path) and os.path.exists(addr_path)):
        return False, 0
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False, 0
    if not isinstance(meta, dict):
        return False, 0
    if int(meta.get("schema_version", -1)) != STRING_CACHE_SCHEMA_VERSION:
        return False, 0
    total = int(meta.get("string_count", 0))
    if total < 0:
        return False, 0
    # Cross-check that strings.jsonl actually contains ``total`` lines.
    # This prevents a stale meta.json from validating a corrupted or
    # partially-rebuilt cache.
    try:
        with open(strings_path, encoding="utf-8", errors="replace") as f:
            line_count = 0
            for _ in f:
                line_count += 1
                if line_count > total:
                    break
    except OSError:
        return False, 0
    if line_count != total:
        return False, 0
    return True, total


def _build_cache(
    version_dir: str,
    records_factory: Callable[[], Iterable[tuple[int, int, str]]],
    cache_key: str,
    idb_path: str,
    db_instance_id: str,
) -> StringCacheIndex:
    """Build (or rebuild) the cache for *version_dir* from a records factory.

    *records_factory* yields ``(ea, length, text)`` tuples from the host
    string enumerator.  Each output file is staged as ``<name>.new`` inside
    ``version_dir`` and then atomically renamed via ``os.replace``.  This
    avoids the Windows directory-replace pitfall (see plan notes) and means
    readers can rely on file presence as a completeness signal — ``meta.json``
    is written last so a complete cache always has it.

    On failure, partially-written ``.new`` files are removed.  Commit order
    is: grams → strings.jsonl → addr_index.json → meta.json.  ``meta.json``
    is removed *before* commit so a stale meta can never validate files
    from a different build attempt.
    """
    start = time.monotonic()
    cache_root = os.path.dirname(version_dir)
    os.makedirs(cache_root, exist_ok=True)
    os.makedirs(version_dir, exist_ok=True)
    grams_subdir = grams_dir(version_dir)
    os.makedirs(grams_subdir, exist_ok=True)

    # Track .new files so we can clean them up on failure.
    staged: list[str] = []

    def _stage_jsonl(text_path: str, record_iter: Iterable[dict[str, Any]]) -> None:
        """Stream-write JSONL records to ``<path>.new`` then atomic-rename."""
        parent = os.path.dirname(text_path) or "."
        os.makedirs(parent, exist_ok=True)
        new_path = f"{text_path}.new"
        fd, tmp = tempfile.mkstemp(prefix=".sc_tmp_", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for rec in record_iter:
                    f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
                    f.write("\n")
            atomic_replace(tmp, new_path)
            staged.append(new_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _stage(text_path: str, text: str) -> None:
        """Write *text* to ``<path>.new`` then atomically rename to *path*."""
        parent = os.path.dirname(text_path) or "."
        os.makedirs(parent, exist_ok=True)
        new_path = f"{text_path}.new"
        fd, tmp = tempfile.mkstemp(prefix=".sc_tmp_", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            atomic_replace(tmp, new_path)
            staged.append(new_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _commit(path: str) -> None:
        """Atomically rename a staged ``<path>.new`` to its final *path*."""
        new_path = f"{path}.new"
        if os.path.exists(new_path):
            atomic_replace(new_path, path)

    def _cleanup_staged() -> None:
        for p in staged:
            try:
                if os.path.exists(p):
                    os.unlink(p)
            except OSError:
                pass

    string_count = 0
    addr_index: dict[str, int] = {}
    postings: dict[str, list[int]] = {}

    def _emit_iter():
        """Yield string documents one at a time so we can stream-write them."""
        nonlocal string_count
        for i, (ea, length, text) in enumerate(records_factory()):
            sanitized = strip_injection_markers(str(text))
            addr_index[f"0x{ea:x}"] = i
            for gram in _extract_trigrams(sanitized.lower()):
                postings.setdefault(gram, []).append(i)
            string_count = i + 1
            yield {
                "i": i,
                "ea": int(ea),
                "length": int(length),
                "text": sanitized,
                "text_lc": sanitized.lower(),
            }

    try:
        # 1) strings.jsonl — streamed, no full corpus list kept in memory.
        strings_path = os.path.join(version_dir, "strings.jsonl")
        _stage_jsonl(strings_path, _emit_iter())

        # 2) addr_index.json
        addr_path = os.path.join(version_dir, "addr_index.json")
        _stage(addr_path, json.dumps(addr_index, ensure_ascii=False, separators=(",", ":")))

        # 3) grams/*.jsonl — shard by safe prefix.
        gram_count = 0
        shards: dict[str, list[tuple[str, list[int]]]] = {}
        for gram, ids in postings.items():
            ids.sort()
            shard_name = _safe_gram_shard(gram)
            shards.setdefault(shard_name, []).append((gram, ids))
        for shard_name, gram_entries in shards.items():
            shard_path = os.path.join(grams_subdir, shard_name)
            shard_lines = [
                json.dumps(
                    {"g": gram, "ids": ids},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                for gram, ids in gram_entries
            ]
            _stage(shard_path, "\n".join(shard_lines) + "\n")
            gram_count += len(gram_entries)

        # 4) meta.json LAST so readers never see partial caches.
        meta = {
            "schema_version": STRING_CACHE_SCHEMA_VERSION,
            "created_at": time.time(),
            "refreshed_at": time.time(),
            "db_instance_id": db_instance_id,
            "idb_path": _normalize_db_path(idb_path),
            "cache_key": cache_key,
            "string_count": string_count,
            "gram_count": gram_count,
            "source": "idautils.Strings",
        }
        meta_path = os.path.join(version_dir, "meta.json")
        _stage(
            meta_path,
            json.dumps(meta, ensure_ascii=False, indent=2),
        )

        # 5) Commit: invalidate any stale meta.json first, then promote
        # files in dependency order.  Removing meta.json here guarantees a
        # reader that observes meta.json present after this point can trust
        # it — it cannot be a leftover from a previous (different) build.
        try:
            os.unlink(meta_path)
        except OSError:
            pass
        for p in (os.path.join(grams_subdir, shard_name) for shard_name in shards):
            _commit(p)
        _commit(strings_path)
        _commit(addr_path)
        _commit(meta_path)

        duration = time.monotonic() - start
        log_debug(
            f"String cache built: {string_count} strings, {gram_count} trigram shards, "
            f"key={cache_key} in {duration:.2f}s"
        )
        return StringCacheIndex(
            version_dir=version_dir,
            cache_key=cache_key,
            idb_path=idb_path,
            db_instance_id=db_instance_id,
            total=string_count,
        )
    except Exception as e:
        log_warning(f"String cache build failed: {e}")
        _cleanup_staged()
        raise


def get_or_build_string_cache(
    records_factory: Callable[[], Iterable[tuple[int, int, str]]],
    cache_dir: str,
    *,
    refresh: bool = False,
) -> StringCacheIndex | None:
    """Return a :class:`StringCacheIndex` for the active database.

    *records_factory* must yield ``(ea, length, text)`` tuples.  It is only
    invoked when the on-disk cache is missing, corrupt, schema-mismatched, or
    *refresh* is True.

    Returns ``None`` when no usable cache identity can be derived (caller
    should fall back to non-persistent behavior).
    """
    cache_key, idb_path, db_instance_id = resolve_active_cache_key()
    if not cache_key:
        return None
    version_dir = cache_version_dir(cache_dir, cache_key)
    lock = _get_build_lock(cache_key)

    with lock:
        if refresh:
            # Wipe any existing version_dir before rebuilding.
            if os.path.exists(version_dir):
                _safe_rmtree(version_dir)
            return _build_cache(version_dir, records_factory, cache_key, idb_path, db_instance_id)

        complete, total = _is_cache_complete(version_dir)
        if complete:
            return StringCacheIndex(
                version_dir=version_dir,
                cache_key=cache_key,
                idb_path=idb_path,
                db_instance_id=db_instance_id,
                total=total,
            )
        # Stale or missing — rebuild from the factory.
        return _build_cache(version_dir, records_factory, cache_key, idb_path, db_instance_id)


def get_existing_string_cache(cache_dir: str) -> StringCacheIndex | None:
    """Return a :class:`StringCacheIndex` for the active database, but only
    if a complete on-disk cache already exists.

    Unlike :func:`get_or_build_string_cache`, this **never** invokes
    ``records_factory`` and **never** rebuilds the cache.  Use this for
    direct, read-only lookups (e.g. ``get_string_at`` fallback) so a cache
    miss does not pay the cold-build cost.

    Returns ``None`` when no usable identity is available or the cache is
    missing / incomplete.
    """
    cache_key, idb_path, db_instance_id = resolve_active_cache_key()
    if not cache_key:
        return None
    version_dir = cache_version_dir(cache_dir, cache_key)
    complete, total = _is_cache_complete(version_dir)
    if not complete:
        return None
    return StringCacheIndex(
        version_dir=version_dir,
        cache_key=cache_key,
        idb_path=idb_path,
        db_instance_id=db_instance_id,
        total=total,
    )


def refresh_string_cache(
    records_factory: Callable[[], Iterable[tuple[int, int, str]]],
    cache_dir: str,
) -> StringCacheIndex | None:
    """Force-rebuild the cache.  See :func:`get_or_build_string_cache`."""
    return get_or_build_string_cache(records_factory, cache_dir, refresh=True)


__all__ = [
    "StringCacheIndex",
    "StringRecord",
    "cache_root_dir",
    "cache_version_dir",
    "derive_cache_key",
    "get_existing_string_cache",
    "get_or_build_string_cache",
    "refresh_string_cache",
    "resolve_active_cache_key",
]
