from __future__ import annotations

import sqlite3
from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).with_name("migrations")
_COLUMN_MIGRATIONS = (
    ("client_id", "002_add_client_id.sql"),
    ("scope_id", "003_add_scope_id.sql"),
    ("idempotency_key", "004_add_idempotency_key.sql"),
    ("request_fingerprint", "005_add_request_fingerprint.sql"),
)


def _sql(name: str) -> str:
    path = (_MIGRATIONS_DIR / name).resolve()
    if path.parent != _MIGRATIONS_DIR.resolve():
        raise RuntimeError("invalid_visual_provider_migration_path")
    return path.read_text(encoding="utf-8")


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(visual_jobs)").fetchall()
    }


def ensure_schema(path: Path) -> None:
    """Apply the provider gateway's isolated SQLite migrations.

    DDL lives only in explicit migration assets under ``migrations/``. The
    runtime JobStore remains CRUD-only, matching ClientPlatform's database
    ownership guard while preserving forward migration from the recovered
    standalone provider-gateway database.
    """

    with sqlite3.connect(str(path), timeout=30) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(_sql("001_create_visual_jobs.sql"))
        columns = _columns(conn)
        for column, migration in _COLUMN_MIGRATIONS:
            if column not in columns:
                conn.executescript(_sql(migration))
                columns.add(column)
        conn.executescript(_sql("010_create_indexes.sql"))
