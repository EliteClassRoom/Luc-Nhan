"""Safe SQLite backend: WAL, query_only, migration transactions, bounded retry.

This module is host-agnostic and provides the only creation-capable primitive.
``open_sqlite()`` with ``allow_create=False`` (default) never creates a missing
file — it raises ``FileNotFoundError``. Only explicit
``MemoryRegistry.initialize()`` / ``WorkspaceStore.create()`` pass
``allow_create=True``.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import quote


class UnsupportedSchemaError(RuntimeError):
    """The database schema version is newer than what this code supports."""


class SchemaMigrationRequired(RuntimeError):
    """A read-only open was requested on a database that needs migration."""


class UnsupportedStorageError(RuntimeError):
    """The storage does not support WAL mode or atomic locking."""


def begin_immediate_with_retry(
    conn: sqlite3.Connection,
    *,
    attempts: int = 4,
    initial_backoff_seconds: float = 0.025,
) -> None:
    """Acquire ``BEGIN IMMEDIATE`` with bounded exponential backoff.

    Retries only lock acquisition — never replays a partially executed
    transaction. After ``attempts`` lock failures, re-raises the last
    ``OperationalError``.
    """
    delay = initial_backoff_seconds
    for attempt in range(attempts):
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt + 1 == attempts:
                raise
            time.sleep(delay)
            delay *= 2


def open_sqlite(
    path: Path,
    *,
    read_only: bool,
    expected_version: int,
    migrations: Mapping[int, Callable[[sqlite3.Connection], None]],
    allow_create: bool = False,
) -> sqlite3.Connection:
    """Open or create a SQLite database with safe defaults.

    Parameters
    ----------
    path:
        Database file path.
    read_only:
        If True, open in immutable query_only mode (mode=ro + PRAGMA
        query_only). If False, open for writes.
    expected_version:
        Required ``PRAGMA user_version``. A newer version is rejected;
        an older version triggers migrations (unless read_only).
    migrations:
        Mapping from target version → migration callable. Each callable
        runs inside the transaction created by this function.
    allow_create:
        If True, may create a missing database file. Only registry and
        workspace initialization paths pass True.
    """
    path = path.resolve()

    if not path.is_file() and not allow_create:
        raise FileNotFoundError(path)

    # check_same_thread=False on every connect below: the workspace's single
    # connection is shared across the agent background thread (writes) and the
    # Qt main thread (knowledge-panel refresh reads); WorkspaceStore serializes
    # all access through its RLock, so cross-thread use is safe by design.
    if read_only:
        uri_path = quote(path.as_posix(), safe="/:")
        conn = sqlite3.connect(
            f"file:{uri_path}?mode=ro",
            uri=True,
            timeout=5.0,
            check_same_thread=False,
        )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        if allow_create and not path.exists():
            conn = sqlite3.connect(
                path,
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
        else:
            # Use mode=rw URI to guard against silent recreation of a
            # missing file.
            if not path.is_file():
                raise FileNotFoundError(path)
            uri_path = quote(path.as_posix(), safe="/:")
            conn = sqlite3.connect(
                f"file:{uri_path}?mode=rw",
                uri=True,
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if read_only:
        conn.execute("PRAGMA query_only = ON")
    else:
        mode = str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
        if mode != "wal":
            conn.close()
            raise UnsupportedStorageError(f"SQLite WAL unavailable for {path}")

    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > expected_version:
        conn.close()
        raise UnsupportedSchemaError(f"schema {version} is newer than supported {expected_version}")
    if read_only and version < expected_version:
        conn.close()
        raise SchemaMigrationRequired(f"schema {version} requires migration to {expected_version}")

    for target in range(version + 1, expected_version + 1):
        begin_immediate_with_retry(conn)
        try:
            migrations[target](conn)
            conn.execute(f"PRAGMA user_version = {target}")
            conn.commit()
        except BaseException:
            conn.rollback()
            conn.close()
            raise

    return conn
