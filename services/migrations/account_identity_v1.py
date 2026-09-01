from __future__ import annotations

import logging
import sqlite3

from services.migrations._helpers import mark_migration, migration_applied

NAME = "account_identity_v1"
log = logging.getLogger(__name__)


def _is_duplicate_column_error(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "duplicate column" in text or "already exists" in text


def _try_add_column(conn: sqlite3.Connection, table: str, ddl: str) -> None:
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    except sqlite3.OperationalError as exc:
        if _is_duplicate_column_error(exc):
            return
        raise


def apply(conn: sqlite3.Connection) -> None:
    if migration_applied(conn, NAME):
        return

    log.info("Migration start: %s", NAME)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS accounts(
            account_id INTEGER PRIMARY KEY,
            primary_user_id INTEGER,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """.strip()
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_primary_user ON accounts(primary_user_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_channel_identities(
            account_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            external_user_id TEXT NOT NULL,
            username TEXT,
            display_name TEXT,
            linked_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            verified_at TEXT,
            link_source TEXT NOT NULL DEFAULT 'runtime',
            PRIMARY KEY(account_id, platform)
        )
        """.strip()
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_account_channel_external
        ON account_channel_identities(platform, external_user_id)
        """.strip()
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_account_channel_last_seen
        ON account_channel_identities(account_id, last_seen_at)
        """.strip()
    )

    _try_add_column(conn, "user_channel_bridge_tokens", "account_id INTEGER")
    _try_add_column(conn, "user_channel_bridge_tokens", "target_platform TEXT")
    _try_add_column(conn, "user_channel_bridge_tokens", "created_from_platform TEXT")
    _try_add_column(conn, "user_channel_bridge_tokens", "created_from_external_user_id TEXT")
    _try_add_column(conn, "user_channel_bridge_tokens", "expires_at TEXT")
    _try_add_column(conn, "user_channel_bridge_tokens", "consumed_account_id INTEGER")

    mark_migration(conn, NAME)
    log.info("Migration applied: %s", NAME)
