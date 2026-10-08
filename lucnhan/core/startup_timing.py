"""Startup performance instrumentation for Luc Nhan.

Low-overhead timing helpers used to measure and report Luc Nhan's cold-start
performance.  Supports two modes:

* **Summary mode** (default, always on): records top-level phases matching
  an explicit allowlist (e.g. ``toggle.*``, ``panel_core.*``, ``controller.*``).
* **Detailed mode** (``LUCNHAN_STARTUP_PROFILE=1``): records every individual
  timer with extra metadata.

All timing records are buffered in memory during early bootstrap (before the
logging subsystem is available) and flushed once ``flush()`` is called.
Flush requires ``complete()`` to have been called first; otherwise it is a
no-op.  After a successful flush records are cleared and the session is
marked flushed.  On log-write failure records are preserved for the next
retry.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_PROFILE_DETAILED = os.environ.get("LUCNHAN_STARTUP_PROFILE", "") in ("1", "yes", "true")

# Summary-mode allowlist — only phases whose label starts with one of these
# prefixes are recorded when _PROFILE_DETAILED is False.
_SUMMARY_PREFIXES = (
    "toggle.",
    "panel_core.",
    "controller.",
    "session_restore.",
    "first_prompt.",
    "runtime_init.",
    "ui.",
    "tools.",
    "ida_form.",
)

# Cached log callable — only set after a successful import, never
# permanently suppressed.  Retried on each flush() until it succeeds.
_log_debug: Any = None
_log_debug_lock = threading.Lock()
_log_debug_lookup_warned = False


def _get_log_debug() -> Any:
    """Return the log_debug callable, importing it on first success."""
    global _log_debug
    if _log_debug is not None:
        return _log_debug
    with _log_debug_lock:
        if _log_debug is not None:
            return _log_debug
        try:
            import importlib

            log_mod = importlib.import_module("lucnhan.core.logging")
            _log_debug = log_mod.log_debug
        except Exception as exc:
            # Suppress noise: only emit once per process via a module flag.
            global _log_debug_lookup_warned
            if not _log_debug_lookup_warned:
                _log_debug_lookup_warned = True
                import sys

                sys.stderr.write(f"[lucnhan:startup_timing] log_debug unavailable: {exc}\n")
        return _log_debug


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class _PhaseRecord:
    """A single timing record for a named phase."""

    __slots__ = ("end_ns", "label", "meta", "start_ns")

    def __init__(self, label: str, start_ns: int, end_ns: int, meta: dict[str, Any] | None = None) -> None:
        self.label = label
        self.start_ns = start_ns
        self.end_ns = end_ns
        self.meta = meta

    @property
    def elapsed_ms(self) -> float:
        return (self.end_ns - self.start_ns) / 1_000_000.0


class _StartupSession:
    """Collects all timing records for one cold plugin-panel open.

    Thread-safe — the runtime init thread and the UI thread can both
    call start()/end() concurrently.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.startup_id: str = ""
        self.records: list[_PhaseRecord] = []
        self._base_ns: int | None = None
        self._flushed: bool = False
        self._completed: bool = False

    def reset_for_new_session(self) -> None:
        """Prepare for a new cold-open session (clears all prior records)."""
        with self._lock:
            self.startup_id = uuid.uuid4().hex[:8]
            self.records.clear()
            self._base_ns = None
            self._flushed = False
            self._completed = False

    def complete(self) -> None:
        """Mark the session as complete.  The next ``flush_to_log()`` will print and clear records."""
        with self._lock:
            self._completed = True

    def start(self, label: str) -> int:
        """Begin timing a phase. Returns the start timestamp (ns).

        After the session has been flushed this returns 0 — warm re-opens
        should not create spurious timing records or partial base timestamps.
        """
        ts = time.perf_counter_ns()
        with self._lock:
            if self._flushed:
                return 0
            if self._base_ns is None:
                self._base_ns = ts
            if not self.startup_id:
                self.startup_id = uuid.uuid4().hex[:8]
        return ts

    @staticmethod
    def _is_summary(label: str) -> bool:
        """Return True if *label* should be recorded in summary (non-detailed) mode."""
        return any(label.startswith(prefix) for prefix in _SUMMARY_PREFIXES)

    def end(self, label: str, start_ns: int, meta: dict[str, Any] | None = None) -> float:
        """End timing a phase. Returns elapsed ms.

        After the session has been flushed this returns 0.0 — no records
        are created for warm re-opens.

        In summary mode records only allowlisted top-level phases.
        In detailed mode all phases are recorded.
        """
        if start_ns == 0:
            return 0.0
        end_ns = time.perf_counter_ns()
        with self._lock:
            if self._flushed:
                return 0.0
            if _PROFILE_DETAILED or self._is_summary(label):
                self.records.append(_PhaseRecord(label, start_ns, end_ns, meta))
        return (end_ns - start_ns) / 1_000_000.0

    def count(self, label: str) -> None:
        """Record a simple counter (no timing).  No-op after flush."""
        now = time.perf_counter_ns()
        with self._lock:
            if self._flushed:
                return
            self.records.append(_PhaseRecord(label, now, now, {"count": True}))

    def set_metadata(self, key: str, value: Any) -> None:
        """Attach metadata to the session (displayed in the flush report).  No-op after flush."""
        with self._lock:
            if self._flushed:
                return
            self.records.append(
                _PhaseRecord(f"meta.{key}", time.perf_counter_ns(), time.perf_counter_ns(), {key: value})
            )

    def flush_to_log(self) -> None:
        """Write all collected timing records to the debug log.

        Records are only actually flushed once per session, and only after
        ``complete()`` has been called, so that premature flushes (e.g.
        from ``update_settings()``) are harmless no-ops.

        If the logging subsystem is not yet available, records are
        preserved and retried on the next flush() call.

        If log write fails, records are preserved and the error is written
        to stderr so timing data is not silently lost.
        """
        log_debug_fn = _get_log_debug()
        if log_debug_fn is None:
            return  # logging not available yet; records preserved for retry

        # Snapshot records under lock, then write outside lock
        with self._lock:
            if not self.records:
                return
            if not self._completed:
                return  # session not yet complete — don't flush prematurely
            sid = self.startup_id or "unknown"
            snapshot = list(self.records)
            base_ns = self._base_ns

        # Write log lines outside the lock
        try:
            log_debug_fn(f"=== Startup timing report (startup_id={sid}) ===")

            sorted_records = sorted(snapshot, key=lambda r: r.start_ns)

            for rec in sorted_records:
                meta_str = ""
                if rec.meta:
                    meta_str = " | " + " ".join(f"{k}={v}" for k, v in sorted(rec.meta.items()))
                log_debug_fn(f"  STARTUP[{rec.label}]: {rec.elapsed_ms:.1f}ms{meta_str}")

            # Total elapsed since session start
            if base_ns is not None:
                total_ms = (time.perf_counter_ns() - base_ns) / 1_000_000.0
                log_debug_fn(f"=== Startup timing: {len(sorted_records)} phases, {total_ms:.0f}ms total ===")
        except Exception as e:
            import sys

            sys.stderr.write(f"[Lục nhãn] Failed to write startup timing log: {e}\n")
            return  # preserve records for next retry

        # Only clear records after successful log write
        with self._lock:
            self.records.clear()
            self._base_ns = None
            self._flushed = True


# ---------------------------------------------------------------------------
# Singleton — one session per process lifetime (cold-open)
# ---------------------------------------------------------------------------

_session: _StartupSession = _StartupSession()


def start(label: str) -> int:
    """Begin timing a named phase.  Returns the start timestamp to pass to ``end()``."""
    return _session.start(label)


def end(label: str, start_ns: int, meta: dict[str, Any] | None = None) -> float:
    """End timing *label*.  Returns elapsed wall-clock in milliseconds."""
    return _session.end(label, start_ns, meta)


def count(label: str) -> None:
    """Record a simple counter (no timing)."""
    _session.count(label)


def set_metadata(key: str, value: Any) -> None:
    """Attach metadata to the current startup session."""
    _session.set_metadata(key, value)


def reset_for_new_session() -> None:
    """Clear all records for a fresh cold-open."""
    _session.reset_for_new_session()


def complete() -> None:
    """Mark the startup session as fully initialized."""
    _session.complete()


def flush() -> None:
    """Write accumulated timing data to the debug log.

    Safe to call multiple times — records are preserved until the session
    is first ``complete()``-ed and the log write succeeds.  Premature flushes
    (e.g. from update_settings) are no-ops.  On write failure records are
    retained for the next attempt.
    After complete, records are cleared on the first successful flush.
    """
    _session.flush_to_log()
