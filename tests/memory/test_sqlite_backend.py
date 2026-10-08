"""Tests for SQLite backend: WAL, migrations, query_only, newer-schema rejection."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lucnhan.memory.sqlite_backend import (
    SchemaMigrationRequired,
    UnsupportedSchemaError,
    begin_immediate_with_retry,
    open_sqlite,
)


def _v1_migration(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE item(id TEXT PRIMARY KEY)")


def test_open_sqlite_enables_required_pragmas(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    conn = open_sqlite(
        db,
        read_only=False,
        expected_version=1,
        migrations={1: _v1_migration},
        allow_create=True,
    )
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        conn.close()


def test_read_only_open_is_query_only_and_windows_safe(tmp_path: Path) -> None:
    db = tmp_path / "space and unicode.db"
    writer = open_sqlite(
        db,
        read_only=False,
        expected_version=1,
        migrations={1: _v1_migration},
        allow_create=True,
    )
    writer.close()

    reader = open_sqlite(db, read_only=True, expected_version=1, migrations={})
    try:
        assert reader.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("INSERT INTO item VALUES ('x')")
    finally:
        reader.close()


def test_missing_database_not_recreated_on_writable_open(tmp_path: Path) -> None:
    db = tmp_path / "nonexistent.db"
    with pytest.raises(FileNotFoundError):
        open_sqlite(db, read_only=False, expected_version=1, migrations={1: _v1_migration})
    assert not db.exists()


def test_newer_schema_is_rejected_without_mutation(tmp_path: Path) -> None:
    db = tmp_path / "newer.db"
    raw = sqlite3.connect(db)
    raw.execute("PRAGMA user_version = 9")
    raw.close()

    with pytest.raises(UnsupportedSchemaError):
        open_sqlite(
            db,
            read_only=False,
            expected_version=1,
            migrations={1: _v1_migration},
        )


def test_read_only_missing_db_raises_file_not_found(tmp_path: Path) -> None:
    db = tmp_path / "ghost.db"
    with pytest.raises(FileNotFoundError):
        open_sqlite(db, read_only=True, expected_version=1, migrations={})


def test_read_only_older_schema_requires_migration(tmp_path: Path) -> None:
    db = tmp_path / "older.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE t(id INTEGER)")
    raw.execute("PRAGMA user_version = 0")
    raw.close()

    with pytest.raises(SchemaMigrationRequired):
        open_sqlite(db, read_only=True, expected_version=1, migrations={})


def test_begin_immediate_with_retry_acquires_lock(tmp_path: Path) -> None:
    db = tmp_path / "retry.db"
    conn = open_sqlite(
        db,
        read_only=False,
        expected_version=1,
        migrations={1: _v1_migration},
        allow_create=True,
    )
    try:
        begin_immediate_with_retry(conn)
        conn.execute("INSERT INTO item VALUES ('ok')")
        conn.commit()
    finally:
        conn.close()


def test_begin_immediate_raises_on_nested_transaction(tmp_path: Path) -> None:
    db = tmp_path / "nested.db"
    conn = open_sqlite(
        db,
        read_only=False,
        expected_version=1,
        migrations={1: _v1_migration},
        allow_create=True,
    )
    try:
        begin_immediate_with_retry(conn)
        with pytest.raises(sqlite3.OperationalError):
            begin_immediate_with_retry(conn)
        conn.rollback()
    finally:
        conn.close()
