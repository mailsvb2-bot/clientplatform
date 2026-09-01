from __future__ import annotations

import logging
import sqlite3
from services.db import get_db, tx
from services.db.runtime import is_postgres_enabled


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _add_col(conn: sqlite3.Connection, table: str, col_ddl: str):
    """Idempotent ALTER TABLE ... ADD COLUMN.

    SQLite does not support `ADD COLUMN IF NOT EXISTS`, so we check manually.
    """
    col_ddl = (col_ddl or "").strip()
    if not col_ddl:
        return
    col_name = col_ddl.split()[0].strip('`"[]')
    try:
        if col_name in _cols(conn, table):
            return
    except sqlite3.Error:
        logging.getLogger(__name__).exception("Failed to inspect columns for %s.%s", table, col_name)
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_ddl}")


def init_db() -> None:
    """Create tables, apply one-time migrations, and ensure prod tables.

    SQLite remains the default engine, but Postgres now goes through the same
    schema/migration entrypoint via a compatibility adapter from services.db.core.
    """
    from services import schema_tables
    from services.migrations import apply_all_migrations

    with get_db() as conn:
        with tx(conn) as c:
            schema_tables.create_or_update_tables(c)
            # One-time migrations run inside the same transaction as schema adjustments.
            apply_all_migrations(c)

        # Prod tables are safe to ensure outside the transaction.
        ensure_prod_tables(conn)


def ensure_prod_tables(conn: sqlite3.Connection) -> None:
    """Shared runtime tables used for idempotency and bounded probe evidence."""
    conn.execute(
        """
    CREATE TABLE IF NOT EXISTS idempotency (
        user_id INTEGER,
        key TEXT,
        created_at INTEGER,
        UNIQUE(user_id, key)
    )
    """.strip()
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS probe_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            probe_type TEXT NOT NULL,
            run_id TEXT NOT NULL UNIQUE,
            user_id INTEGER,
            started_at_utc TEXT NOT NULL,
            finished_at_utc TEXT,
            status TEXT NOT NULL,
            cleanup_status TEXT NOT NULL DEFAULT 'pending',
            rows_touched INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            evidence_json TEXT NOT NULL DEFAULT '{}'
        )
        """.strip()
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_probe_runs_type_started
        ON probe_runs(probe_type, started_at_utc)
        """.strip()
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_probe_runs_status_cleanup
        ON probe_runs(status, cleanup_status)
        """.strip()
    )
