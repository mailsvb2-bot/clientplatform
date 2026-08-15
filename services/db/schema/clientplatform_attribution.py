from __future__ import annotations

import sqlite3


_SOURCE_VALUES = (
    "'organic','referral','telegram','vk','max','website',"
    "'yandex_direct','partner','manual_import','unknown'"
)


def ensure(c: sqlite3.Connection) -> None:
    """Create durable first-party acquisition attribution without a second event store."""

    c.execute(
        f"""
        CREATE TABLE IF NOT EXISTS attribution_identities(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            source TEXT NOT NULL,
            identity_kind TEXT NOT NULL,
            identity_fingerprint TEXT NOT NULL,
            source_ref_type TEXT NOT NULL,
            source_ref_id TEXT NOT NULL,
            promotion_campaign_id TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            UNIQUE(business_id, identity_kind, identity_fingerprint),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(promotion_campaign_id, business_id)
                REFERENCES promotion_campaigns(id, business_id) ON DELETE CASCADE,
            CHECK(source IN ({_SOURCE_VALUES})),
            CHECK(length(identity_kind) BETWEEN 1 AND 40),
            CHECK(length(identity_fingerprint)=64),
            CHECK(length(source_ref_type) BETWEEN 1 AND 40),
            CHECK(length(source_ref_id) BETWEEN 1 AND 300)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_attribution_identities_business_source
        ON attribution_identities(business_id, source, created_at)
        """
    )

    c.execute(
        f"""
        CREATE TABLE IF NOT EXISTS acquisition_touches(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            attribution_identity_id TEXT NOT NULL,
            customer_id TEXT NOT NULL,
            source TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{{}}',
            metadata_version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            UNIQUE(business_id, attribution_identity_id, customer_id),
            FOREIGN KEY(attribution_identity_id, business_id)
                REFERENCES attribution_identities(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(customer_id, business_id)
                REFERENCES customers(id, business_id) ON DELETE CASCADE,
            CHECK(source IN ({_SOURCE_VALUES})),
            CHECK(metadata_version >= 1)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_acquisition_touches_customer_time
        ON acquisition_touches(business_id, customer_id, occurred_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_acquisition_touches_source_time
        ON acquisition_touches(business_id, source, occurred_at)
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS attribution_links(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            touch_id TEXT NOT NULL,
            customer_id TEXT,
            booking_slot_id TEXT,
            model_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            UNIQUE(business_id, customer_id, model_version),
            UNIQUE(business_id, booking_slot_id, model_version),
            FOREIGN KEY(touch_id, business_id)
                REFERENCES acquisition_touches(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(customer_id, business_id)
                REFERENCES customers(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(booking_slot_id, business_id)
                REFERENCES booking_slots(id, business_id) ON DELETE CASCADE,
            CHECK(model_version='first_touch_v1'),
            CHECK(
                (customer_id IS NOT NULL AND booking_slot_id IS NULL)
                OR (customer_id IS NULL AND booking_slot_id IS NOT NULL)
            )
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_attribution_links_touch
        ON attribution_links(business_id, touch_id, model_version)
        """
    )
