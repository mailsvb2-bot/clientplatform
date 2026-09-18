from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Persist owner-selected presentation mode per canonical event stage."""

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_event_content_preferences(
            business_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            mode TEXT NOT NULL,
            updated_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(business_id, event_id, stage),
            FOREIGN KEY(event_id, business_id)
                REFERENCES clientplatform_events(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(updated_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(stage IN ('warmup','event_day_announcement','post_event_followup')),
            CHECK(mode IN ('text','text_with_image','text_in_image'))
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_clientplatform_event_content_event
        ON clientplatform_event_content_preferences(business_id, event_id, stage)
        """
    )
