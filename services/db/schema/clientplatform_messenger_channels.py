from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Create tenant-scoped VK/MAX ingress routes and one-time customer link grants."""

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS messenger_ingress_routes(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            connection_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            external_route_id TEXT NOT NULL,
            webhook_secret_reference TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            revoked_at TEXT,
            UNIQUE(id, business_id),
            UNIQUE(id, business_id, platform),
            UNIQUE(business_id, connection_id),
            UNIQUE(business_id, platform, external_route_id),
            FOREIGN KEY(connection_id, business_id, platform)
                REFERENCES connections(id, business_id, platform) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(platform IN ('vk','max')),
            CHECK(status IN ('active','disabled','revoked')),
            CHECK(
                substr(webhook_secret_reference, 1, 9)='secret://'
                OR substr(webhook_secret_reference, 1, 6)='kms://'
                OR substr(webhook_secret_reference, 1, 8)='vault://'
            )
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS customer_channel_link_tokens(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            customer_id TEXT NOT NULL,
            token_digest TEXT NOT NULL,
            target_platform TEXT,
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            consumed_platform TEXT,
            consumed_external_subject TEXT,
            UNIQUE(id, business_id),
            UNIQUE(token_digest),
            FOREIGN KEY(customer_id, business_id)
                REFERENCES customers(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(length(token_digest)=64),
            CHECK(target_platform IS NULL OR target_platform IN ('telegram','vk','max')),
            CHECK(consumed_platform IS NULL OR consumed_platform IN ('telegram','vk','max')),
            CHECK(
                (consumed_at IS NULL AND consumed_platform IS NULL AND consumed_external_subject IS NULL)
                OR (consumed_at IS NOT NULL AND consumed_platform IS NOT NULL AND consumed_external_subject IS NOT NULL)
            )
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_messenger_ingress_routes_active
        ON messenger_ingress_routes(platform, status, external_route_id)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_customer_channel_links_business_customer
        ON customer_channel_link_tokens(business_id, customer_id, expires_at)
        """
    )
