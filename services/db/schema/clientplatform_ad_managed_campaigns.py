from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Create durable tenant-owned provider campaign bindings."""
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS ad_managed_campaigns(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            promotion_campaign_id TEXT NOT NULL,
            connection_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            provisioning_key TEXT NOT NULL,
            external_campaign_id TEXT,
            external_campaign_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'provisioning',
            last_error_code TEXT,
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            UNIQUE(business_id, promotion_campaign_id, connection_id),
            UNIQUE(business_id, provider, provisioning_key),
            UNIQUE(connection_id, external_campaign_id),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(promotion_campaign_id, business_id)
                REFERENCES promotion_campaigns(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(connection_id, business_id)
                REFERENCES ad_connections(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(provider IN ('yandex_direct')),
            CHECK(status IN ('provisioning', 'ready', 'failed')),
            CHECK((status='ready' AND external_campaign_id IS NOT NULL) OR status!='ready')
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ad_managed_campaigns_business_status
        ON ad_managed_campaigns(business_id, status, updated_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ad_managed_campaigns_connection
        ON ad_managed_campaigns(connection_id, status, external_campaign_id)
        """
    )


__all__ = ["ensure"]
