"""Atomic file-replace primitive resilient to transient Windows file locks.

Real-time AV/indexing scanners (Windows Defender, Search Indexer) briefly
open freshly-written files, which makes ``os.replace`` fail with
ERROR_SHARING_VIOLATION (32) or ERROR_LOCK_VIOLATION (33).  A short bounded
retry resolves the transient lock; after the retries we re-raise the
original error, so the worst case is identical to a plain replace.

Mirrors the SQLite ``begin_immediate_with_retry`` idiom in
``rikugan.memory.sqlite_backend`` for the same class of Windows file-lock
race.  Shared by the persistent string cache, the MEMORY.md projector, the
session-history writer, and the knowledge raw store.

Host-agnostic: imports only the stdlib.
"""

from __future__ import annotations

import os
import time

# Windows transient file-lock errors worth retrying:
#   32 = ERROR_SHARING_VIOLATION, 33 = ERROR_LOCK_VIOLATION.
_DEFAULT_RETRY_WINERRORS = frozenset({32, 33})
_DEFAULT_ATTEMPTS = 6
_DEFAULT_INITIAL_BACKOFF_SEC = 0.05

StrPath = str | os.PathLike[str]


def atomic_replace(src: StrPath, dst: StrPath) -> None:
    """``os.replace`` with a bounded retry for transient Windows file locks.

    On non-Windows the first failure (if any) propagates immediately since
    ``OSError.winerror`` is ``None``; behavior is identical to a plain
    ``os.replace`` there.  The retry budget is read from module-level
    defaults at call time so tests can tune them via monkeypatch.
    """
    delay = _DEFAULT_INITIAL_BACKOFF_SEC
    for attempt in range(_DEFAULT_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            # Retry only on the two transient Windows file-lock errors;
            # everything else (genuine permission denied, missing source)
            # propagates immediately.
            if getattr(exc, "winerror", None) not in _DEFAULT_RETRY_WINERRORS:
                raise
            if attempt + 1 == _DEFAULT_ATTEMPTS:
                raise
            time.sleep(delay)
            delay *= 2


__all__ = ["StrPath", "atomic_replace"]
