"""Optional UI profiling probes for diagnosing main-thread latency.

Probes use ``log_debug`` (the project's standard debug channel). The file
sink always records DEBUG (see ``core.logging``), so probe output lands in
``lucnhan_debug.log`` regardless of the IDA Output verbosity setting. The
host sink (IDA Output window) only shows DEBUG when the user raises
``ida_output_log_level`` to ``debug`` — so probes never spam the Output
window by default.

Use this to localise "whole IDA lags when the chat grows" to a specific
layer (poll loop, per-event dispatch, markdown render, or widget layout)
before attempting fixes — see systematic-debugging Phase 1.

Typical usage::

    1. Settings → IDA Output verbosity → Debug  (optional: also see it live)
    2. Reproduce the lag in a chat session.
    3. Inspect lucnhan_debug.log for ``PROFILE[*].slow`` lines::

        PROFILE[ui.poll_events].slow: tick_ms=180.4 events=30
        PROFILE[ui.render].slow: tick_ms=42.1 text_len=8234
        PROFILE[ui.md_to_html].slow: tick_ms=15.7 text_len=8234 cache=miss

The ``probe`` context manager is a no-op (returns immediately) unless the
Luc Nhan logger is enabled for DEBUG, so production builds with default
verbosity pay no measurable overhead.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager

from ..core.logging import get_logger

#: Report a tick only when it exceeds this many milliseconds. Keeps the
#: log readable — we care about the slow ticks, not the 0.2ms ones.
_SLOW_THRESHOLD_MS = 50.0

#: Rolling buffer of the last N slow ticks per probe, for summary dumps.
_HISTORY: deque[dict[str, float | str]] = deque(maxlen=200)


@contextmanager
def probe(name: str, **fields: float | str | int | bool) -> Iterator[None]:
    """Time a block and log it if it exceeds the slow threshold.

    Extra keyword arguments are included in the log line so callers can
    attach context (e.g. ``text_len=...``, ``events=...``).

    No-op when the logger is not enabled for DEBUG, so default-verbosity
    production runs pay only one ``isEnabledFor`` check per call.
    """
    logger = get_logger()
    if not logger.isEnabledFor(10):  # logging.DEBUG == 10
        yield
        return
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        if elapsed_ms >= _SLOW_THRESHOLD_MS:
            record: dict[str, float | str] = {"name": name, "tick_ms": round(elapsed_ms, 1)}
            for k, v in fields.items():
                record[k] = v
            _HISTORY.append(record)
            extras = " ".join(f"{k}={v}" for k, v in fields.items())
            logger.debug("PROFILE[%s].slow: tick_ms=%.1f %s", name, elapsed_ms, extras)


def dump_summary() -> None:
    """Log a histogram-style summary of accumulated slow ticks.

    Call this from a debug hotkey or end-of-turn hook to see which layer
    dominated wall-clock time during a laggy session.
    """
    if not _HISTORY:
        return
    logger = get_logger()
    if not logger.isEnabledFor(10):  # logging.DEBUG == 10
        return
    by_name: dict[str, list[float]] = {}
    for record in _HISTORY:
        by_name.setdefault(str(record["name"]), []).append(float(record["tick_ms"]))
    lines = [f"PROFILE summary (slow ticks > {_SLOW_THRESHOLD_MS:.0f}ms):"]
    for name, times in sorted(by_name.items()):
        times.sort()
        count = len(times)
        median = times[count // 2]
        p95 = times[min(count - 1, int(count * 0.95))]
        total = sum(times)
        lines.append(f"  {name}: count={count} median={median:.1f}ms p95={p95:.1f}ms total={total:.0f}ms")
    logger.debug("\n".join(lines))
