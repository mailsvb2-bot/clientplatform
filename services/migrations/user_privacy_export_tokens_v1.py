from __future__ import annotations

import logging
import sqlite3

from services.migrations._helpers import migration_applied, mark_migration

NAME = "user_privacy_export_tokens_v1"
log = logging.getLogger(__name__)


def apply(conn: sqlite3.Connection) -> None:
    if migration_applied(conn, NAME):
        return
    log.info("Migration start: %s", NAME)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_privacy_export_tokens(
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            created_at TEXT NOT NULL,
            consumed_at TEXT
        )
        """.strip()
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_privacy_export_tokens_user "
        "ON user_privacy_export_tokens(user_id, created_at)"
    )
    mark_migration(conn, NAME)
    log.info("Migration applied: %s", NAME)
