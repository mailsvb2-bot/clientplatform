from __future__ import annotations

from datetime import datetime, timezone

from services.db.runtime import is_postgres_enabled


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_schema_migrations(conn) -> None:
    """
    Canonical migration ledger.

    Compatibility contract:
    - older migrations import migration_applied()
    - newer migrations may import is_migration_applied() / is_applied()
    - some migrations import table_exists()
    - mark_migration() must work on SQLite and Postgres
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at_utc TEXT
        )
        """
    )

    # Repair old/partial tables. Postgres supports IF NOT EXISTS.
    # SQLite in this project normally already uses the full shape; if the
    # column exists this is harmless on Postgres and guarded by try on SQLite.
    try:
        conn.execute(
            "ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS applied_at_utc TEXT"
        )
    except Exception:
        pass

    try:
        conn.execute(
            """
            UPDATE schema_migrations
            SET applied_at_utc = COALESCE(applied_at_utc, ?)
            WHERE applied_at_utc IS NULL
            """,
            (_utc_now(),),
        )
    except Exception:
        pass


def table_exists(conn, table_name: str) -> bool:
    """
    Backend-safe table existence check.

    Important for Postgres:
    never SELECT from a possibly missing table, because that aborts the
    transaction and poisons the rest of startup migrations.
    """
    if is_postgres_enabled():
        row = conn.execute(
            """
            SELECT 1 AS present
            FROM information_schema.tables
            WHERE table_schema = current_schema()
              AND table_name = ?
            LIMIT 1
            """,
            (table_name,),
        ).fetchone()
        return row is not None

    row = conn.execute(
        """
        SELECT 1 AS present
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        LIMIT 1
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def is_migration_applied(conn, name: str) -> bool:
    ensure_schema_migrations(conn)
    row = conn.execute(
        "SELECT 1 AS present FROM schema_migrations WHERE name = ? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def migration_applied(conn, name: str) -> bool:
    return is_migration_applied(conn, name)


def is_applied(conn, name: str) -> bool:
    return is_migration_applied(conn, name)


def mark_migration(conn, name: str) -> None:
    ensure_schema_migrations(conn)

    if is_migration_applied(conn, name):
        return

    conn.execute(
        "INSERT INTO schema_migrations(name, applied_at_utc) VALUES(?, ?)",
        (name, _utc_now()),
    )
