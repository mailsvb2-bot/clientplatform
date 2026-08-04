from __future__ import annotations

import sqlite3


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
            CHECK(channel IN ('telegram', 'vk', 'whatsapp', 'website', 'offline')),
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
        CREATE TABLE IF NOT EXISTS promotion_events(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            campaign_id TEXT NOT NULL,
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
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_promotion_events_campaign_type
        ON promotion_events(business_id, campaign_id, event_type, occurred_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_promotion_events_customer
        ON promotion_events(business_id, customer_id, occurred_at)
        """
    )
