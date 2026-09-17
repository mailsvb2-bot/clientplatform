from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Create durable multi-session storage for canonical events."""

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_event_sessions(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            starts_at TEXT NOT NULL,
            ends_at TEXT,
            provider_key TEXT NOT NULL,
            provider_label TEXT,
            join_url TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            UNIQUE(event_id, business_id, position),
            FOREIGN KEY(event_id, business_id)
                REFERENCES clientplatform_events(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            CHECK(position >= 1),
            CHECK(length(provider_key) BETWEEN 1 AND 64)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_clientplatform_event_sessions_event
        ON clientplatform_event_sessions(business_id, event_id, position)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_clientplatform_event_sessions_start
        ON clientplatform_event_sessions(business_id, starts_at, event_id, position)
        """
    )
