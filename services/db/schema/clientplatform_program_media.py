from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Create the durable ClientPlatform program-media cleanup contour."""
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS program_media_cleanup_queue(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            media_reference TEXT NOT NULL UNIQUE,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at TEXT NOT NULL,
            locked_at TEXT,
            lock_token TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            dead_at TEXT,
            CHECK(attempts >= 0),
            CHECK(status IN ('pending', 'processing', 'retry', 'dead'))
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_program_media_cleanup_due
        ON program_media_cleanup_queue(status, available_at, id)
        """
    )
