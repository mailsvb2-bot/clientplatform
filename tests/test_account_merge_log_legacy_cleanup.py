import sqlite3

import pytest

from services.migrations._helpers import migration_applied
from services.migrations.account_merge_log_legacy_cleanup_v1 import NAME, apply


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _legacy_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE account_merge_log(
            id INTEGER PRIMARY KEY,
            target_account_id INTEGER NOT NULL,
            source_account_id INTEGER NOT NULL,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )


def test_cleanup_marks_absent_legacy_table() -> None:
    conn = _conn()
    try:
        apply(conn)
        assert migration_applied(conn, NAME)
    finally:
        conn.close()


def test_cleanup_drops_empty_legacy_merge_store() -> None:
    conn = _conn()
    try:
        _legacy_table(conn)
        apply(conn)
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_merge_log'"
        ).fetchone() is None
        assert migration_applied(conn, NAME)
    finally:
        conn.close()


def test_cleanup_fails_closed_when_legacy_store_has_evidence() -> None:
    conn = _conn()
    try:
        _legacy_table(conn)
        conn.execute(
            "INSERT INTO account_merge_log VALUES(1,10,20,'legacy','applied','{}','2026-09-03T00:00:00Z')"
        )
        with pytest.raises(RuntimeError, match="legacy_account_merge_log_not_empty:1"):
            apply(conn)
        assert conn.execute("SELECT COUNT(*) FROM account_merge_log").fetchone()[0] == 1
        assert not migration_applied(conn, NAME)
    finally:
        conn.close()
