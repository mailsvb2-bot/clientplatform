from __future__ import annotations

import logging

from services.db.runtime import is_postgres_enabled
from services.migrations._helpers import is_migration_applied, mark_migration

log = logging.getLogger(__name__)

NAME = "scheduled_jobs_to_jobs_v1"


def _table_exists(conn, table_name: str) -> bool:
    """Backend-safe table existence check for optional legacy tables."""

    # Selecting from a missing table aborts a PostgreSQL transaction, so the
    # catalog check must use the canonical ClientPlatform DB runtime rather than
    # the old METRO_DB_ENGINE environment variable.
    if is_postgres_enabled():
        row = conn.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = current_schema()
              AND table_name = ?
            LIMIT 1
            """,
            (table_name,),
        ).fetchone()
        return bool(row)

    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        LIMIT 1
        """,
        (table_name,),
    ).fetchone()
    return bool(row)


def apply(conn) -> None:
    if is_migration_applied(conn, NAME):
        return

    # Fresh Postgres installs will not have the old SQLite legacy table.
    # That is not an error. Mark migration as applied and continue.
    if not _table_exists(conn, "scheduled_jobs"):
        log.info("Migration skipped: legacy scheduled_jobs table does not exist")
        mark_migration(conn, NAME)
        return

    # If the new table is not present yet, create the minimal canonical table.
    # Existing schema_core may also create/extend it later.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            kind TEXT,
            payload TEXT,
            run_at_utc TEXT,
            status TEXT DEFAULT 'pending',
            created_at_utc TEXT
        )
        """
    )

    rows = conn.execute(
        """
        SELECT id, user_id, kind, payload, run_at_utc, status, created_at_utc
        FROM scheduled_jobs
        """
    ).fetchall()

    for row in rows:
        data = dict(row) if not isinstance(row, dict) else row
        conn.execute(
            """
            INSERT INTO jobs(id, user_id, kind, payload, run_at_utc, status, created_at_utc)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                data.get("id"),
                data.get("user_id"),
                data.get("kind"),
                data.get("payload"),
                data.get("run_at_utc"),
                data.get("status") or "pending",
                data.get("created_at_utc"),
            ),
        )

    mark_migration(conn, NAME)
    log.info("Migration applied: %s, migrated_rows=%s", NAME, len(rows))
