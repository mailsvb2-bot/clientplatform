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
            CHECK(mode IN ('text','text_with_image','text_in_image','text_with_video'))
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_clientplatform_event_content_event
        ON clientplatform_event_content_preferences(business_id, event_id, stage)
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_event_content_messages(
            business_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            slot_key TEXT NOT NULL,
            position INTEGER NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            scheduled_at TEXT,
            text TEXT NOT NULL,
            source TEXT NOT NULL,
            updated_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(business_id, event_id, stage, slot_key),
            FOREIGN KEY(event_id, business_id)
                REFERENCES clientplatform_events(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(updated_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(stage IN ('warmup','event_day_announcement','post_event_followup')),
            CHECK(source IN ('template','ai','owner')),
            CHECK(position >= 1),
            CHECK(revision >= 1),
            CHECK(length(slot_key) BETWEEN 1 AND 80),
            CHECK(length(text) BETWEEN 1 AND 4000)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_clientplatform_event_content_messages_due
        ON clientplatform_event_content_messages(
            stage, scheduled_at, business_id, event_id, position
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_event_content_assets(
            business_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            slot_key TEXT NOT NULL,
            kind TEXT NOT NULL,
            media_reference TEXT NOT NULL,
            source TEXT NOT NULL,
            source_ref TEXT NOT NULL DEFAULT '',
            revision INTEGER NOT NULL DEFAULT 1,
            updated_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(business_id, event_id, stage, slot_key),
            FOREIGN KEY(event_id, business_id)
                REFERENCES clientplatform_events(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(updated_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(stage IN ('warmup','event_day_announcement','post_event_followup')),
            CHECK(kind IN ('image','video')),
            CHECK(source IN ('owner','generated')),
            CHECK(revision >= 1),
            CHECK(length(slot_key) BETWEEN 1 AND 80),
            CHECK(length(media_reference) BETWEEN 1 AND 2048),
            CHECK(length(source_ref) <= 200)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_clientplatform_event_content_assets_event
        ON clientplatform_event_content_assets(business_id, event_id, stage, slot_key)
        """
    )

    message_columns = {
        str(row[1])
        for row in c.execute(
            "PRAGMA table_info(clientplatform_event_content_messages)"
        ).fetchall()
    }
    if "revision" not in message_columns:
        c.execute(
            """
            ALTER TABLE clientplatform_event_content_messages
            ADD COLUMN revision INTEGER NOT NULL DEFAULT 1
            """
        )
