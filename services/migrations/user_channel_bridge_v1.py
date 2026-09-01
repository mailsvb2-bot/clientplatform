from __future__ import annotations

import logging
import sqlite3

from services.migrations._helpers import migration_applied, mark_migration

NAME = "user_channel_bridge_v1"
log = logging.getLogger(__name__)


def apply(conn: sqlite3.Connection) -> None:
    if migration_applied(conn, NAME):
        return
    log.info("Migration start: %s", NAME)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_channel_bridge_tokens(
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            purpose TEXT NOT NULL DEFAULT 'switch_messenger',
            created_at TEXT NOT NULL,
            used_at TEXT,
            used_platform TEXT,
            used_external_user_id TEXT
        )
        """.strip()
    )
    mark_migration(conn, NAME)
    log.info("Migration applied: %s", NAME)
