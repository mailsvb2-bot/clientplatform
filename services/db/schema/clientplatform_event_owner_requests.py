from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Persist replay-safe owner requests for event creation."""

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_event_owner_requests(
            business_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            event_id TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY(business_id, request_id),
            FOREIGN KEY(event_id, business_id)
                REFERENCES clientplatform_events(id, business_id) ON DELETE CASCADE,
            CHECK(length(request_id)=36),
            CHECK(length(request_hash)=64)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_clientplatform_event_owner_requests_event
        ON clientplatform_event_owner_requests(business_id, event_id)
        """
    )
