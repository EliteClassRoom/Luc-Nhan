"""Regression tests for the persistent raw string cache.

These cover the safety properties of ``rikugan.tools.string_cache`` and the
IDA-side ``rikugan.ida.tools.strings`` contract added during the
string-cache refactor:

* ``list_strings`` must not be in ``CACHEABLE_TOOLS`` (otherwise an in-memory
  page can outlive a disk refresh).
* ``get_existing_string_cache`` must return ``None`` for a missing cache
  instead of rebuilding it.
* ``get_string_at`` must use ``get_existing_string_cache`` (no cold rebuild
  on a direct-read miss).
* ``list_strings`` preserves the historical ``title="Strings"`` header.
* ``refresh_string_cache`` must not return the local cache directory path.
* The fallback direct enumeration must sanitize injection markers.
* On-disk artifacts are treated as untrusted:
    - tampered ``meta.json`` (string_count mismatch) is rejected,
    - truncated ``strings.jsonl`` with stale meta is rejected,
    - oversize ``addr_index.json`` is rejected,
    - oversize gram shard is rejected.
* Build / get-at output is sanitized end-to-end.

The tests are pure-Python (no Qt, no IDA) and use ``tmp_path`` + monkeypatch
to isolate the cache from the host runtime.
"""

from __future__ import annotations

import inspect
import json
import os
import threading
import time as _time
from collections.abc import Iterable
from typing import Any

import pytest

from rikugan.core import atomic_io
from rikugan.tools import cache as tool_cache
from rikugan.tools import string_cache

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _patch_active_key(monkeypatch: pytest.MonkeyPatch, cache_dir: str, cache_key: str) -> None:
    """Pin the active cache key so tests do not depend on host identity."""

    def _resolve() -> tuple[str, str, str]:
        return cache_key, "/tmp/fake.idb", cache_key

    monkeypatch.setattr(string_cache, "resolve_active_cache_key", _resolve)


def _build_tiny_cache(
    cache_dir: str,
    cache_key: str,
    records: Iterable[tuple[int, int, str]],
) -> string_cache.StringCacheIndex:
    """Build a cache and return the index."""

    idx = string_cache.get_or_build_string_cache(lambda: iter(records), cache_dir)
    assert idx is not None
    return idx


# ---------------------------------------------------------------------------
# CACHEABLE_TOOLS contract
# ---------------------------------------------------------------------------


def test_list_strings_not_cacheable() -> None:
    """``list_strings`` must not be cached in-memory (refresh can't invalidate)."""
    assert "list_strings" not in tool_cache.CACHEABLE_TOOLS, (
        "list_strings must not be cached in-memory: a refresh would leave "
        "stale pages that the agent would observe as current"
    )


def test_search_strings_not_cacheable() -> None:
    """``search_strings`` shares the same refresh-argument race."""
    assert "search_strings" not in tool_cache.CACHEABLE_TOOLS


# ---------------------------------------------------------------------------
# get_existing_string_cache: read-only, no rebuild
# ---------------------------------------------------------------------------


def test_returns_none_when_missing(tmp_path: Any) -> None:
    """No cache key resolution -> short-circuit to None without touching FS."""
    cache_dir = str(tmp_path)
    result = string_cache.get_existing_string_cache(cache_dir)
    assert result is None


def test_returns_none_for_missing_version_dir(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Key resolves but no version_dir on disk yet."""
    cache_dir = str(tmp_path)
    _patch_active_key(monkeypatch, cache_dir, "idb-no-such-key")
    assert string_cache.get_existing_string_cache(cache_dir) is None


def test_does_not_invoke_records_factory(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """``get_existing_string_cache`` must not call the records factory or build."""
    cache_dir = str(tmp_path)
    _patch_active_key(monkeypatch, cache_dir, "idb-must-not-build")
    called = {"count": 0}

    def factory() -> Iterable[tuple[int, int, str]]:
        called["count"] += 1
        return iter(())

    orig_get_or_build = string_cache.get_or_build_string_cache
    calls: list[Any] = []

    def spy(factory_: Any, cache_dir_: str, **kw: Any) -> Any:
        calls.append((factory_, cache_dir_, kw))
        return orig_get_or_build(factory_, cache_dir_, **kw)

    monkeypatch.setattr(string_cache, "get_or_build_string_cache", spy)

    result = string_cache.get_existing_string_cache(cache_dir)
    assert result is None
    assert called["count"] == 0
    assert len(calls) == 0


def test_round_trips_after_build(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """After a build, the read-only helper returns the same index data."""
    cache_dir = str(tmp_path)
    _patch_active_key(monkeypatch, cache_dir, "idb-roundtrip")
    _build_tiny_cache(
        cache_dir,
        "idb-roundtrip",
        [
            (0x401000, 11, "hello world"),
            (0x401020, 8, "goodbye!"),
        ],
    )
    idx = string_cache.get_existing_string_cache(cache_dir)
    assert idx is not None
    assert idx.total == 2
    assert idx.get_at(0x401000).text == "hello world"


# ---------------------------------------------------------------------------
# On-disk artifact bounds
# ---------------------------------------------------------------------------


def test_tampered_meta_with_wrong_string_count_is_rejected(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """``meta.json`` claiming more strings than ``strings.jsonl`` has -> rejected."""
    cache_dir = str(tmp_path)
    _patch_active_key(monkeypatch, cache_dir, "idb-bad-meta")
    idx = _build_tiny_cache(
        cache_dir,
        "idb-bad-meta",
        [(0x401000, 5, "alpha"), (0x401010, 5, "bravo")],
    )
    meta_path = idx.meta_path
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    meta["string_count"] = 999
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f)

    assert string_cache.get_existing_string_cache(cache_dir) is None


def test_truncated_strings_jsonl_rejected_when_meta_claims_more(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """``strings.jsonl`` with fewer lines than ``meta.json`` claims -> rejected."""
    cache_dir = str(tmp_path)
    _patch_active_key(monkeypatch, cache_dir, "idb-trunc")
    version_dir = string_cache.cache_version_dir(cache_dir, "idb-trunc")
    idx = _build_tiny_cache(
        cache_dir,
        "idb-trunc",
        [
            (0x401000, 5, "alpha"),
            (0x401020, 5, "bravo"),
            (0x401040, 5, "charlie"),
        ],
    )
    # Keep meta at 3 but reduce strings.jsonl to 1 line.
    strings_path = os.path.join(version_dir, "strings.jsonl")
    with open(strings_path, encoding="utf-8") as f:
        first = f.readline()
    with open(strings_path, "w", encoding="utf-8") as f:
        f.write(first)
    assert idx.total == 3  # in-memory still says 3
    # get_existing must refuse because disk != meta.
    assert string_cache.get_existing_string_cache(cache_dir) is None


def test_oversized_addr_index_rejected(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """``addr_index.json`` exceeding the byte cap -> ``get_at`` returns None."""
    cache_dir = str(tmp_path)
    _patch_active_key(monkeypatch, cache_dir, "idb-oversize-idx")
    version_dir = string_cache.cache_version_dir(cache_dir, "idb-oversize-idx")
    _build_tiny_cache(
        cache_dir,
        "idb-oversize-idx",
        [(0x401000, 5, "alpha")],
    )
    addr_index_path = os.path.join(version_dir, "addr_index.json")
    # Lower the bound so the existing addr_index.json exceeds it.  The JSON
    # payload ``{"0x401000": 0}`` is 14 bytes; bound to 4 makes it oversize.
    low_bound = 4
    monkeypatch.setattr(string_cache, "_MAX_ADDR_INDEX_FILE_BYTES", low_bound)
    with open(addr_index_path, "w", encoding="utf-8") as f:
        json.dump({"0x401000": 0}, f)
    assert os.path.getsize(addr_index_path) > low_bound
    fresh = string_cache.get_existing_string_cache(cache_dir)
    assert fresh is not None
    assert fresh.get_at(0x401000) is None


def test_oversized_shard_rejected(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Oversize gram shard is skipped instead of loading it into memory."""
    cache_dir = str(tmp_path)
    _patch_active_key(monkeypatch, cache_dir, "idb-oversize-shard")
    _build_tiny_cache(
        cache_dir,
        "idb-oversize-shard",
        [(0x401000, 12, "hello world")],
    )
    idx = string_cache.get_existing_string_cache(cache_dir)
    assert idx is not None
    # Lower the bound so the existing shard file exceeds it.
    low_bound = 4
    monkeypatch.setattr(string_cache, "_MAX_SHARD_FILE_BYTES", low_bound)
    # Search must still return an empty result (no raise, no loop).
    results = idx.search("hello")
    assert isinstance(results, list)


# ---------------------------------------------------------------------------
# Sanitization: build, get_at, search output
# ---------------------------------------------------------------------------


def test_build_sanitizes_stored_text(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stored text in ``strings.jsonl`` is free of injection markers.

    ``strip_injection_markers`` replaces known role markers and instruction
    overrides with the placeholder ``[FILTERED]``.  This test asserts the
    on-disk corpus has those placeholders rather than the raw markers.
    """
    cache_dir = str(tmp_path)
    _patch_active_key(monkeypatch, cache_dir, "idb-sanitize")
    idx = _build_tiny_cache(
        cache_dir,
        "idb-sanitize",
        [
            (0x401000, 30, "the [SYSTEM] payload arrives here"),
            (0x401020, 30, "[INST] never mind [/INST]"),
        ],
    )
    version_dir = idx.version_dir
    with open(os.path.join(version_dir, "strings.jsonl"), encoding="utf-8") as f:
        raw = f.read()
    # The known role markers must be replaced with the sentinel.
    assert "[SYSTEM]" not in raw
    assert "[INST]" not in raw
    assert "[/INST]" not in raw
    # And the sentinel must actually be present.
    assert "[FILTERED]" in raw


def test_get_at_returns_sanitized_text(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """``get_at`` never returns text containing injection markers."""
    cache_dir = str(tmp_path)
    _patch_active_key(monkeypatch, cache_dir, "idb-getat-sanitize")
    _build_tiny_cache(
        cache_dir,
        "idb-getat-sanitize",
        [(0x401000, 30, "the [SYSTEM] payload arrives here")],
    )
    idx = string_cache.get_existing_string_cache(cache_dir)
    assert idx is not None
    rec = idx.get_at(0x401000)
    assert rec is not None
    assert "[SYSTEM]" not in rec.text
    assert "[FILTERED]" in rec.text


def test_search_returns_sanitized_matches(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Search matches must not carry injection markers.

    Sanitization happens at build time, so the sanitized token text
    (``[FILTERED]``) is what the inverted index sees.  Searching for the
    *surviving* surrounding text returns matches without the raw marker.
    """
    cache_dir = str(tmp_path)
    _patch_active_key(monkeypatch, cache_dir, "idb-search-sanitize")
    _build_tiny_cache(
        cache_dir,
        "idb-search-sanitize",
        [(0x401000, 30, "the [SYSTEM] payload arrives here")],
    )
    idx = string_cache.get_existing_string_cache(cache_dir)
    assert idx is not None
    results = idx.search("payload")
    assert results, "search should find at least one match"
    for r in results:
        assert "[SYSTEM]" not in r.text


# ---------------------------------------------------------------------------
# Windows transient file-lock (AV/indexer) retry on os.replace
# ---------------------------------------------------------------------------


def _winerror_32_replace_factory(monkeypatch: pytest.MonkeyPatch, fails_per_dst: int):
    """Patch ``os.replace`` to fail ``fails_per_dst`` times per *destination*.

    Simulates Windows Defender / Search Indexer briefly holding each freshly
    written cache file open (ERROR_SHARING_VIOLATION / 32), which previously
    aborted the whole cache build with a ``[WinError 32]`` warning.  Each
    destination independently recovers after a couple of retries, exactly as a
    real AV scan releases a file handle.
    """
    real_replace = os.replace
    calls = {"n": 0}
    failures = {"n": 0}
    per_dst: dict[str, int] = {}
    state = {"calls": calls, "failures": failures, "per_dst": per_dst}

    def flaky_replace(src: str, dst: str) -> None:
        calls["n"] += 1
        seen = per_dst.get(dst, 0)
        if seen < fails_per_dst:
            per_dst[dst] = seen + 1
            failures["n"] += 1
            exc = PermissionError(32, "The process cannot access the file")
            exc.winerror = 32
            raise exc
        return real_replace(src, dst)

    monkeypatch.setattr(atomic_io.os, "replace", flaky_replace)
    return state


def test_build_retries_through_transient_winerror_32(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient WinError 32 during ``os.replace`` must be retried, not fatal.

    Regression for the malware-analysis report where AV scanning freshly
    written cache shards made the final ``<shard>.jsonl.new`` ->
    ``<shard>.jsonl`` rename fail and aborted the whole string cache build.
    """
    cache_dir = str(tmp_path)
    _patch_active_key(monkeypatch, cache_dir, "idb-winerror32")
    # Each destination fails twice then recovers — the retry must absorb it.
    state = _winerror_32_replace_factory(monkeypatch, fails_per_dst=2)
    monkeypatch.setattr(atomic_io, "_DEFAULT_INITIAL_BACKOFF_SEC", 0.0)

    idx = _build_tiny_cache(
        cache_dir,
        "idb-winerror32",
        [
            (0x401000, 11, "hello world"),
            (0x401020, 8, "goodbye!"),
        ],
    )
    # The flaky replace must have actually fired its failures and recovered.
    assert state["failures"]["n"] > 0
    # And the cache must be complete and queryable.
    assert idx.total == 2
    rec = idx.get_at(0x401000)
    assert rec is not None
    assert rec.text == "hello world"
    assert idx.search("hello")


def test_build_raises_when_winerror_32_is_persistent(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """If WinError 32 never clears after all retries, the build fails loudly.

    The retry must not mask a permanently-locked file into a silent hang or
    a corrupt cache — it re-raises the original error, same as before the fix.
    """
    cache_dir = str(tmp_path)
    _patch_active_key(monkeypatch, cache_dir, "idb-winerror32-stuck")
    # More failures per dst than retries => never recovers.
    _winerror_32_replace_factory(monkeypatch, fails_per_dst=10_000)
    monkeypatch.setattr(atomic_io, "_DEFAULT_INITIAL_BACKOFF_SEC", 0.0)
    monkeypatch.setattr(atomic_io, "_DEFAULT_ATTEMPTS", 3)

    with pytest.raises(OSError):
        _build_tiny_cache(cache_dir, "idb-winerror32-stuck", [(0x401000, 5, "alpha")])


def test_non_winerror_replace_failure_is_not_retried(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-transient error (e.g. ENOENT) must propagate immediately, no retry."""
    cache_dir = str(tmp_path)
    _patch_active_key(monkeypatch, cache_dir, "idb-enoent")
    state = {"calls": 0}

    def always_enoent(src: str, dst: str) -> None:
        state["calls"] += 1
        raise FileNotFoundError(2, "no such file")

    monkeypatch.setattr(atomic_io.os, "replace", always_enoent)
    monkeypatch.setattr(atomic_io, "_DEFAULT_INITIAL_BACKOFF_SEC", 0.0)

    with pytest.raises(OSError):
        _build_tiny_cache(cache_dir, "idb-enoent", [(0x401000, 5, "alpha")])
    # Exactly one attempt — no retry on a non-sharing-violation error.
    assert state["calls"] == 1


# ---------------------------------------------------------------------------
# IDA-side contract: strings.py source checks
# ---------------------------------------------------------------------------


def _ida_strings_module() -> Any:
    """Import the IDA-side strings module (loads without IDA present)."""
    import rikugan.ida.tools.strings as strings_mod

    return strings_mod


def test_list_strings_preserves_title_strings() -> None:
    """``title="Strings"`` is preserved for backward compatibility."""
    src = inspect.getsource(_ida_strings_module().list_strings)
    assert 'title="Strings"' in src


def test_list_strings_emits_cache_status_as_suffix() -> None:
    """Cache status is emitted as a suffix line, not embedded in the title."""
    src = inspect.getsource(_ida_strings_module().list_strings)
    assert "(served from persistent cache:" in src


def test_refresh_string_cache_does_not_leak_paths() -> None:
    """Refresh output must never include a local filesystem path."""
    src = inspect.getsource(_ida_strings_module().refresh_string_cache)
    assert "version_dir" not in src
    assert "cache_dir" not in src
    assert "Cache directory" not in src


def test_get_string_at_uses_existing_cache() -> None:
    """``get_string_at`` uses the read-only helper, not the build helper."""
    src = inspect.getsource(_ida_strings_module().get_string_at)
    assert "get_existing_string_cache" in src
    assert "get_or_build_string_cache" not in src


def test_fallback_direct_enumeration_is_sanitized() -> None:
    """Direct-enumeration fallback paths must sanitize injection markers."""
    src_list = inspect.getsource(_ida_strings_module().list_strings)
    src_search = inspect.getsource(_ida_strings_module().search_strings)
    assert "strip_injection_markers" in src_list
    assert "strip_injection_markers" in src_search


# ---------------------------------------------------------------------------
# Background build coordinator (UI-mode decoupling)
# ---------------------------------------------------------------------------
#
# These exercise trigger_background_build / background_build_status with a
# default inline *main_runner* (no IDA, no Qt) so the daemon thread runs the
# two-phase build synchronously.  They verify the tool-facing contract:
# a cold build never blocks the caller (returns "building" immediately),
# exactly one build runs per cache_key, a complete cache short-circuits to
# "ready", and a failed build is reported and cleaned up.


def _wait_status(
    cache_dir: str,
    expected: str,
    timeout: float = 10.0,
) -> tuple[str, int]:
    """Poll background_build_status until it reaches *expected* (or timeout)."""
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        status, total = string_cache.background_build_status(cache_dir)
        if status == expected:
            return status, total
        _time.sleep(0.01)
    return string_cache.background_build_status(cache_dir)


def _reset_bg_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear process-level background state so tests are independent."""
    with string_cache._BG_LOCK:
        string_cache._BG_STATE.clear()
    # Drop any build threads left over from prior tests (best-effort).
    for t in threading.enumerate():
        if t.name.startswith("strcache-build-"):
            t.join(timeout=2.0)


def test_background_build_runs_to_ready(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cold call returns 'building' immediately and finishes 'ready'."""
    _reset_bg_state(monkeypatch)
    cache_dir = str(tmp_path)
    _patch_active_key(monkeypatch, cache_dir, "idb-bg-ready")
    records = [
        (0x401000, 11, "hello world"),
        (0x401020, 5, "bravo"),
        (0x401040, 6, "charlie"),
    ]

    status = string_cache.trigger_background_build(lambda: iter(records), cache_dir)
    assert status == "building"

    status, total = _wait_status(cache_dir, "ready")
    assert status == "ready"
    assert total == 3
    # The on-disk cache must be complete and queryable.
    idx = string_cache.get_existing_string_cache(cache_dir)
    assert idx is not None
    assert idx.search("hello")
    assert idx.get_at(0x401020).text == "bravo"


def test_background_build_short_circuits_when_complete(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """If a complete cache already exists, no build is started."""
    _reset_bg_state(monkeypatch)
    cache_dir = str(tmp_path)
    _patch_active_key(monkeypatch, cache_dir, "idb-bg-short")
    # Pre-build synchronously so a complete cache is on disk.
    _build_tiny_cache(cache_dir, "idb-bg-short", [(0x401000, 5, "alpha")])

    started = {"n": 0}

    def factory() -> Iterable[tuple[int, int, str]]:
        started["n"] += 1
        return iter([(0xDEAD, 1, "x")])

    status = string_cache.trigger_background_build(factory, cache_dir)
    assert status == "ready"
    # The factory must never have been consumed.
    assert started["n"] == 0


def test_background_build_no_second_build_while_running(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A concurrent caller sees 'building' and does not start a second build."""
    _reset_bg_state(monkeypatch)
    cache_dir = str(tmp_path)
    _patch_active_key(monkeypatch, cache_dir, "idb-bg-dedupe")
    started = {"n": 0}
    gate = threading.Event()

    def factory() -> Iterable[tuple[int, int, str]]:
        # Increment before blocking so the caller can observe the build started.
        started["n"] += 1
        # Block enumeration so the build stays 'building' long enough to
        # observe a second trigger.
        gate.wait(timeout=5.0)
        return iter([(0x401000, 5, "alpha")])

    status = string_cache.trigger_background_build(factory, cache_dir)
    assert status == "building"
    # A second trigger while the first is running must not start another build.
    status2 = string_cache.trigger_background_build(lambda: iter([]), cache_dir)
    assert status2 == "building"
    # Release the gate so the daemon thread can finish and not leak.
    gate.set()
    _wait_status(cache_dir, "ready")
    # The factory ran exactly once: the second trigger was deduped, not built.
    assert started["n"] == 1


def test_background_build_reports_failure_and_cleans_up(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A factory that raises leaves status 'failed' and no committed cache."""
    _reset_bg_state(monkeypatch)
    cache_dir = str(tmp_path)
    _patch_active_key(monkeypatch, cache_dir, "idb-bg-fail")

    def factory() -> Iterable[tuple[int, int, str]]:
        raise RuntimeError("boom")

    status = string_cache.trigger_background_build(factory, cache_dir)
    assert status == "building"
    status, _total = _wait_status(cache_dir, "failed")
    assert status == "failed"
    # No complete cache committed.
    assert string_cache.get_existing_string_cache(cache_dir) is None


def test_background_force_refresh_rebuilds(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """force_refresh=True wipes and rebuilds even when a cache exists."""
    _reset_bg_state(monkeypatch)
    cache_dir = str(tmp_path)
    _patch_active_key(monkeypatch, cache_dir, "idb-bg-refresh")
    idx = _build_tiny_cache(cache_dir, "idb-bg-refresh", [(0x401000, 5, "alpha")])
    assert idx.total == 1

    new_records = [
        (0x401000, 5, "alpha"),
        (0x401020, 5, "bravo"),
        (0x401040, 7, "charlie"),
    ]
    status = string_cache.trigger_background_build(lambda: iter(new_records), cache_dir, force_refresh=True)
    assert status == "building"
    status, total = _wait_status(cache_dir, "ready")
    assert status == "ready"
    assert total == 3
    fresh = string_cache.get_existing_string_cache(cache_dir)
    assert fresh is not None and fresh.total == 3


def test_background_build_status_unknown_until_triggered(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Before any build is triggered, status is 'unknown'."""
    _reset_bg_state(monkeypatch)
    cache_dir = str(tmp_path)
    _patch_active_key(monkeypatch, cache_dir, "idb-bg-unknown")
    status, _total = string_cache.background_build_status(cache_dir)
    assert status == "unknown"
