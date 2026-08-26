from __future__ import annotations

import logging
import re
import sqlite3

from services.db.runtime import is_postgres_enabled
from services.migrations._helpers import mark_migration, migration_applied


NAME = "clientplatform_messenger_setup_telegram_v1"
_CONSTRAINT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _sqlite_supports_telegram(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        ("messenger_connection_setup_sessions",),
    ).fetchone()
    if row is None:
        return True
    sql = str(row["sql"] if hasattr(row, "keys") else row[0])
    return "'telegram'" in sql


def _rebuild_sqlite(conn: sqlite3.Connection) -> None:
    from services.db.schema import clientplatform_messenger_channels

    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        ("messenger_connection_setup_sessions",),
    ).fetchone()
    if row is None:
        clientplatform_messenger_channels.ensure(conn)
        return
    conn.execute("DROP INDEX IF EXISTS idx_messenger_setup_business_platform")
    conn.execute("DROP TABLE IF EXISTS messenger_connection_setup_sessions_platform_v0")
    conn.execute(
        "ALTER TABLE messenger_connection_setup_sessions "
        "RENAME TO messenger_connection_setup_sessions_platform_v0"
    )
    clientplatform_messenger_channels.ensure(conn)
    conn.execute(
        """
        INSERT INTO messenger_connection_setup_sessions(
            id,business_id,platform,token_digest,created_by_member_id,
            created_at,expires_at,consumed_at
        )
        SELECT
            id,business_id,platform,token_digest,created_by_member_id,
            created_at,expires_at,consumed_at
        FROM messenger_connection_setup_sessions_platform_v0
        """
    )
    conn.execute("DROP TABLE messenger_connection_setup_sessions_platform_v0")


def _update_postgres(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT conname, pg_get_constraintdef(oid) AS definition
        FROM pg_constraint
        WHERE conrelid='messenger_connection_setup_sessions'::regclass
          AND contype='c'
        """
    ).fetchall()
    for row in rows:
        name = str(row["conname"] if hasattr(row, "keys") else row[0])
        definition = str(row["definition"] if hasattr(row, "keys") else row[1])
        if "platform" not in definition:
            continue
        if not _CONSTRAINT_NAME.fullmatch(name):
            raise RuntimeError("unsafe messenger setup constraint name")
        conn.execute(
            f'ALTER TABLE messenger_connection_setup_sessions DROP CONSTRAINT "{name}"'
        )
    conn.execute(
        """
        ALTER TABLE messenger_connection_setup_sessions
        ADD CONSTRAINT cp_messenger_setup_platform_telegram_v1
        CHECK(platform IN ('telegram','vk','max'))
        """
    )


def apply(conn: sqlite3.Connection) -> None:
    log = logging.getLogger(__name__)
    if migration_applied(conn, NAME):
        log.info("Migration skipped (already applied): %s", NAME)
        return
    log.info("Migration start: %s", NAME)
    if is_postgres_enabled():
        _update_postgres(conn)
    elif not _sqlite_supports_telegram(conn):
        _rebuild_sqlite(conn)
    mark_migration(conn, NAME)
    log.info("Migration applied: %s", NAME)
