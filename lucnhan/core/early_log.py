"""Luc Nhan early-startup crash log.

This module is loaded by ``lucnhan_plugin.py`` at IDA plugin import time,
*before* anything that might crash (Shiboken's ``__import__`` hook,
``qt_compat._detect_binding()``, ``FormToPySideWidget``).

**Stdlib only.** No ``import lucnhan.*`` — avoids circular imports and
prevents us from pulling in code that might itself be the crash site. If
Qt is broken, IDA's Python is broken, or our own logging subsystem is
unreachable, this module still writes a self-contained forensic log to
``~/.idapro/lucnhan/early_startup.log``.

Public surface:

- :func:`_early_log` — append a single record (``msg``, ``level``).
- :func:`_early_log_crash` — flush the in-memory ring buffer + a formatted
  traceback into a sibling ``early_startup_crash.log``.
- :func:`_early_log_path` — full path to the running log (tests + inspection).
- :func:`_early_log_crash_path` — full path to the crash log.
- :func:`_early_log_buffer_snapshot` — in-memory ring buffer (tests).
- :func:`_reset_for_tests` — drop state and close the file handle (tests).

Behavior:

- One file per process; opened in append+text mode at import time.
- Every record is followed by ``flush()`` + ``os.fsync()`` so the previous
  records survive a hard crash that hits the next call.
- All operations swallow their own exceptions; we are a diagnostic sink
  and must never crash the host.
"""

from __future__ import annotations

import collections
import ctypes
import datetime
import io
import os
import sys
import threading
import time
import traceback
import typing

# Buffer size: last N formatted records. 50 lines is enough to cover the
# plugin-entry -> first-paint window while keeping the crash file small
# even after a crash loop.
_BUFFER_MAXLEN = 50

# Level strings accepted by :func:`_early_log`. "DEBUG" is intentionally
# omitted from user-facing messages but kept here so internal callers
# (e.g. before/after _ensure_import_guard()) can log guard state without
# spamming INFO.
_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")

# Lock guarding module-level mutable state. IDA may invoke log functions
# from non-main threads during shutdown; CPython's GIL makes single-file
# writes atomic but we still serialize them so that flush()/fsync() order
# is deterministic across threads.
_lock = threading.Lock()

# (timestamp, formatted-line) tuples, capped at _BUFFER_MAXLEN. Holds the
# most recent records so that _early_log_crash() can include them as a
# snapshot even if the live log file is corrupted/empty.
_buffer: collections.deque[tuple[float, str]] = collections.deque(maxlen=_BUFFER_MAXLEN)

# Resolved once at import time. Importing os.path.expanduser each call is
# cheap but the value never changes for the process lifetime, so we cache.
_file_path: str = ""
_crash_path: str = ""

# Long-lived file handle. Held open so appends are single-writes and do
# not pay open() cost; every write is followed by flush+fsync.
_file: io.TextIOBase | None = None

# Banner record emission guarded so repeated imports in the same process
# (e.g. module reload) do not produce duplicate banners.
_banner_written: bool = False

# Pre-rename config directory. Everything the user owns — providers, API
# keys, skills, memory workspaces — lives under ``~/.idapro/<name>``, so the
# rename has to move the directory rather than just stop looking at it.
# Equally: this module is the first thing in a process to create that
# directory, hence the migration lives here and runs before ``makedirs``.
_LEGACY_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".idapro", "rikugan")


def migrate_legacy_config_dir(config_dir: str) -> None:
    """One-time move of the pre-rename config directory onto *config_dir*.

    No-op when *config_dir* already exists or nothing is left to move.
    Best effort by design: this runs from a diagnostic sink, so it must
    never raise. Safe to call repeatedly.
    """
    try:
        if os.path.isdir(_LEGACY_CONFIG_DIR) and not os.path.exists(config_dir):
            os.rename(_LEGACY_CONFIG_DIR, config_dir)
    except OSError:
        pass


def _resolve_paths() -> tuple[str, str]:
    """Return ``(log_path, crash_path)`` under the user's ``.idapro/lucnhan``.

    Falls back to a tmp-dir path if the standard location cannot be created.
    Never raises.
    """
    try:
        log_dir = os.path.join(os.path.expanduser("~"), ".idapro", "lucnhan")
        migrate_legacy_config_dir(log_dir)
        os.makedirs(log_dir, exist_ok=True)
        return (
            os.path.join(log_dir, "early_startup.log"),
            os.path.join(log_dir, "early_startup_crash.log"),
        )
    except Exception:
        # Path resolution itself is broken (sandboxed env, deleted $HOME).
        # Use /tmp on POSIX or TEMP on Windows; never raise.
        try:
            base = os.environ.get("TEMP") or os.environ.get("TMP") or os.getcwd()
        except Exception:
            base = "."
        return (
            os.path.join(base, "lucnhan_early_startup.log"),
            os.path.join(base, "lucnhan_early_startup_crash.log"),
        )


def _format_line(level: str, msg: str) -> str:
    """Format one record as a single line (no embedded newlines).

    Newlines in ``msg`` are stripped and replaced with a literal ``\\n``
    marker so that one record maps to one line and the file is grep-friendly.
    """
    level = level.upper()
    if level not in _LEVELS:
        level = "INFO"
    ts = datetime.datetime.now().astimezone().isoformat(timespec="milliseconds")
    safe = str(msg).replace("\r", "\\r").replace("\n", "\\n")
    return f"{ts} [{level:<5}] {safe}"


def _open_file(path: str) -> io.TextIOBase | None:
    """Open ``path`` in append+text mode with UTF-8 encoding; never raises.

    Returns ``None`` if the file cannot be opened.
    """
    try:
        # buffering=1 (line-buffered) is the stdlib default for text mode,
        # but we restate it explicitly to make the intent grep-able.
        return open(path, "a", encoding="utf-8", buffering=1, errors="replace")
    except Exception:
        return None


def _write_banner(fh: io.TextIOBase | None) -> None:
    """Write a single banner line describing the process environment.

    Best-effort: each individual piece of state is independently guarded so
    a failure in one probe does not abort the rest.
    """
    global _banner_written
    if _banner_written:
        return
    _banner_written = True
    pieces: list[str] = []
    pieces.append(f"pid={os.getpid()}")
    try:
        pieces.append(f"python={sys.version.split()[0]}")
    except Exception:
        pieces.append("python=?")
    try:
        pieces.append(f"platform={sys.platform}")
    except Exception:
        pieces.append("platform=?")
    try:
        pieces.append(f"argv={sys.argv}")
    except Exception:
        pieces.append("argv=?")
    try:
        # ``builtins_import`` is the ``builtins`` *module* resolved at module
        # load (see bottom of file). Its ``__import__`` attribute is the real
        # builtin import function whose ``id`` lets us verify the Shiboken
        # guard is wrapping the same object across startup phases.
        # ``builtins_import`` is ``Module | None``; guard explicitly so mypy
        # sees a non-Optional operand (the ``except`` covers the runtime miss
        # when the module was somehow evicted, but the type check needs the
        # narrowing to be explicit).
        if builtins_import is not None:
            pieces.append(f"builtins_import_id={id(builtins_import.__import__)}")
        else:
            pieces.append("builtins_import_id=?")
    except Exception:
        pieces.append("builtins_import_id=?")
    # Best-effort Qt5Core probe (Windows only). ctypes is stdlib; we do
    # NOT import PySide6 here so detection failure cannot be a crash site.
    try:
        if sys.platform == "win32":
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.GetModuleHandleW("Qt5Core.dll")
            pieces.append(f"qt5core_handle={int(handle) if handle else 0}")
        else:
            pieces.append("qt5core_handle=n/a")
    except Exception:
        pieces.append("qt5core_handle=?")
    line = "=== Lục nhãn early-startup log started === " + " ".join(pieces)
    _early_log(line, level="INFO")


def _early_log(msg: str, level: typing.Literal["INFO", "DEBUG", "WARN", "ERROR"] = "INFO") -> None:
    """Append one record to the early-startup log.

    ``msg`` is coerced via ``str()`` and stripped of newlines so each call
    produces exactly one line. The record is also stored in the in-memory
    ring buffer so :func:`_early_log_crash` can include it as a snapshot.

    Safe to call from any thread. Never raises.
    """
    global _file
    try:
        line = _format_line(level, msg)
    except Exception:
        # The only way _format_line can fail is str() blowing up on the
        # caller's object; degrade to a placeholder so the log still
        # receives *something*.
        line = f"{datetime.datetime.now().astimezone().isoformat(timespec='milliseconds')} [ERROR] unformattable log record"

    with _lock:
        try:
            _buffer.append((time.time(), line))
        except Exception:
            pass
        fh = _file
        if fh is not None:
            try:
                fh.write(line + "\n")
                fh.flush()
                # fsync requires a raw file descriptor. Wrap the lookup in
                # try/except because some environments restrict buffer flushes
                # on closed/replaced file objects.
                try:
                    os.fsync(fh.fileno())
                except Exception:
                    pass
            except Exception:
                # File is wedged (disk full, fd closed, etc.). Drop the
                # handle so subsequent calls fall back to buffer-only.
                try:
                    fh.close()  # type: ignore[union-attr]
                except Exception:
                    pass
                _file = None


def _early_log_crash(exc: BaseException) -> None:
    """Flush the buffer + a formatted traceback to ``early_startup_crash.log``.

    The crash file is created on the first crash and appended-to on each
    subsequent crash. A leading ``=== crash @ <ts> ===`` marker separates
    individual crash records.

    Never raises.
    """
    try:
        try:
            tb_lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
        except Exception:
            tb_lines = [f"Unformattable exception: {type(exc).__name__}: {exc}\n"]
        # Snapshot the buffer (under the lock) so the chronology is consistent.
        with _lock:
            snapshot = list(_buffer)
        ts = datetime.datetime.now().astimezone().isoformat(timespec="milliseconds")
        body_lines: list[str] = [f"=== crash @ {ts} {type(exc).__name__}: {exc} ==="]
        body_lines.append("--- buffer snapshot ---")
        for _t, line in snapshot:
            body_lines.append(line)
        body_lines.append("--- traceback ---")
        body_lines.extend(tb_lines)
        body = "".join(line if line.endswith("\n") else line + "\n" for line in body_lines)

        fh = _open_file(_crash_path)
        if fh is None:
            return
        try:
            fh.write(body)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except Exception:
                pass
        finally:
            try:
                fh.close()
            except Exception:
                pass
    except Exception:
        # Even crash-path I/O failed. Last resort: drop to stderr.
        try:
            sys.stderr.write(f"[Lục nhãn] _early_log_crash sink failure: {type(exc).__name__}: {exc}\n")
        except Exception:
            pass


def _early_log_path() -> str:
    """Return the resolved path of the running log file (tests/inspection)."""
    return _file_path


def _early_log_crash_path() -> str:
    """Return the resolved path of the crash log file (tests/inspection)."""
    return _crash_path


def _early_log_buffer_snapshot() -> list[str]:
    """Return a chronological copy of buffered log lines (tests/inspection)."""
    with _lock:
        return [line for _t, line in _buffer]


def _reset_for_tests() -> None:
    """Drop all module-level state and close the file handle.

    Only intended for unit tests. Calling this in production would discard
    the diagnostic sink mid-session.
    """
    global _file, _banner_written, _file_path, _crash_path
    with _lock:
        if _file is not None:
            try:
                _file.close()
            except Exception:
                pass
        _file = None
        _banner_written = False
        _file_path = ""
        _crash_path = ""
        _buffer.clear()


# ---------------------------------------------------------------------------
# Module import side effects
# ---------------------------------------------------------------------------
#
# We resolve paths and open the file handle *at import time* so that any
# failure (e.g. ``$HOME`` unreadable) shows up in the same log we are
# trying to write to — not in a separate sink. The banner is written below
# once paths are known. An import-time exception here cannot actually be
# raised: every helper above swallows internally, so callers importing
# ``early_log`` will never see ImportError from this module.

_file_path, _crash_path = _resolve_paths()
_file = _open_file(_file_path)

# Reference used by the banner writer to print ``id(builtins.__import__)``
# at import time without invoking ``builtins.__import__`` (which would
# round-trip through our optional future guard and confuse the snapshot).
# Must be resolved BEFORE ``_write_banner`` so the banner probe has a real
# module object to introspect (otherwise it falls into the except branch
# and prints ``builtins_import_id=?``).
builtins_import = sys.modules.get("builtins")

_write_banner(_file)
