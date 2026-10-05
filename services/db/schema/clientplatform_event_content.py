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

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_event_landing_profiles(
            business_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            draft_json TEXT NOT NULL,
            published_json TEXT,
            draft_source TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            published_revision INTEGER,
            preview_token_digest TEXT,
            preview_revision INTEGER,
            preview_expires_at TEXT,
            ai_status TEXT,
            ai_base_revision INTEGER,
            ai_claim_digest TEXT,
            ai_updated_at TEXT,
            updated_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            published_at TEXT,
            PRIMARY KEY(business_id, event_id),
            FOREIGN KEY(event_id, business_id)
                REFERENCES clientplatform_events(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(updated_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(draft_source IN ('template','ai','owner')),
            CHECK(revision >= 1),
            CHECK(published_revision IS NULL OR published_revision >= 1),
            CHECK(preview_revision IS NULL OR preview_revision >= 1),
            CHECK(ai_status IS NULL OR ai_status IN ('confirming','planning','ready','ambiguous')),
            CHECK(ai_base_revision IS NULL OR ai_base_revision >= 1),
            CHECK(
                (ai_status IS NULL
                    AND ai_base_revision IS NULL
                    AND ai_claim_digest IS NULL
                    AND ai_updated_at IS NULL)
                OR
                (ai_status IS NOT NULL
                    AND ai_base_revision IS NOT NULL
                    AND length(ai_claim_digest)=64
                    AND ai_updated_at IS NOT NULL)
            ),
            CHECK(
                (preview_token_digest IS NULL
                    AND preview_revision IS NULL
                    AND preview_expires_at IS NULL)
                OR
                (preview_token_digest IS NOT NULL
                    AND length(preview_token_digest)=64
                    AND preview_revision IS NOT NULL
                    AND preview_expires_at IS NOT NULL)
            ),
            CHECK(
                (published_json IS NULL
                    AND published_revision IS NULL
                    AND published_at IS NULL)
                OR
                (published_json IS NOT NULL
                    AND published_revision IS NOT NULL
                    AND published_at IS NOT NULL)
            )
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_clientplatform_event_landing_event
        ON clientplatform_event_landing_profiles(business_id, event_id, revision)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_clientplatform_event_landing_preview
        ON clientplatform_event_landing_profiles(
            preview_token_digest, preview_expires_at
        )
        """
    )

    landing_columns = {
        str(row[1])
        for row in c.execute(
            "PRAGMA table_info(clientplatform_event_landing_profiles)"
        ).fetchall()
    }
    landing_additions = {
        "ai_status": "TEXT",
        "ai_base_revision": "INTEGER",
        "ai_claim_digest": "TEXT",
        "ai_updated_at": "TEXT",
    }
    for column, sql_type in landing_additions.items():
        if column not in landing_columns:
            c.execute(
                f"ALTER TABLE clientplatform_event_landing_profiles "
                f"ADD COLUMN {column} {sql_type}"
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
