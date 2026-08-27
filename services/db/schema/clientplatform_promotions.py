from __future__ import annotations

import sqlite3

from services.schema_core import _add_col, _cols


def ensure(c: sqlite3.Connection) -> None:
    """Create tenant-scoped promotion campaigns and attributable outcomes."""

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS promotion_campaigns(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            offering_id TEXT NOT NULL,
            booking_slot_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            source_token TEXT NOT NULL UNIQUE,
            creative_id TEXT NOT NULL,
            headline TEXT NOT NULL,
            primary_text TEXT NOT NULL,
            description TEXT NOT NULL,
            cta TEXT NOT NULL,
            creative_style TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            UNIQUE(business_id, booking_slot_id, channel),
            FOREIGN KEY(offering_id, business_id)
                REFERENCES business_offerings(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(booking_slot_id, business_id)
                REFERENCES booking_slots(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(channel IN ('telegram', 'vk', 'max', 'whatsapp', 'website', 'offline')),
            CHECK(status IN ('active', 'paused', 'closed'))
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_promotion_campaigns_business_status
        ON promotion_campaigns(business_id, status, created_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_promotion_campaigns_slot
        ON promotion_campaigns(business_id, booking_slot_id, channel)
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS promotion_source_aliases(
            source_token TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            campaign_id TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            source_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(business_id, campaign_id, source_kind, source_key),
            FOREIGN KEY(campaign_id, business_id)
                REFERENCES promotion_campaigns(id, business_id) ON DELETE CASCADE,
            CHECK(length(source_kind) BETWEEN 1 AND 40),
            CHECK(length(source_key) BETWEEN 1 AND 300),
            CHECK(status IN ('active', 'revoked'))
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_promotion_source_aliases_campaign
        ON promotion_source_aliases(business_id, campaign_id, status, source_kind)
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS promotion_events(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            campaign_id TEXT NOT NULL,
            source_token TEXT,
            event_type TEXT NOT NULL,
            customer_id TEXT NOT NULL,
            booking_slot_id TEXT NOT NULL,
            dedupe_key TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            UNIQUE(business_id, dedupe_key),
            FOREIGN KEY(campaign_id, business_id)
                REFERENCES promotion_campaigns(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(customer_id, business_id)
                REFERENCES customers(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(booking_slot_id, business_id)
                REFERENCES booking_slots(id, business_id) ON DELETE CASCADE,
            CHECK(event_type IN ('opened', 'booked'))
        )
        """
    )
    # Additive migration for promotion events created before source-level
    # attribution. Legacy rows are backfilled to the campaign's canonical token.
    if "source_token" not in _cols(c, "promotion_events"):
        _add_col(c, "promotion_events", "source_token TEXT")
    c.execute(
        """
        UPDATE promotion_events
        SET source_token=(
            SELECT pc.source_token
            FROM promotion_campaigns pc
            WHERE pc.id=promotion_events.campaign_id
              AND pc.business_id=promotion_events.business_id
        )
        WHERE source_token IS NULL OR source_token=''
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_promotion_events_campaign_type
        ON promotion_events(business_id, campaign_id, event_type, occurred_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_promotion_events_source_type
        ON promotion_events(business_id, source_token, event_type, occurred_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_promotion_events_customer
        ON promotion_events(business_id, customer_id, occurred_at)
        """
    )
