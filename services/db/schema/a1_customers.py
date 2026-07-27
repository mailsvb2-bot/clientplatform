from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Create tenant-scoped customer profiles and external identities."""
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS customers(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            display_name TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT,
            UNIQUE(id, business_id),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(status IN ('active', 'archived'))
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS customer_identities(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            customer_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            external_subject TEXT NOT NULL,
            username TEXT,
            display_name TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            revoked_at TEXT,
            UNIQUE(business_id, platform, external_subject),
            UNIQUE(id, business_id),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(customer_id, business_id)
                REFERENCES customers(id, business_id) ON DELETE CASCADE,
            CHECK(status IN ('active', 'revoked'))
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_customers_business_status
        ON customers(business_id, status, created_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_customer_identities_customer_status
        ON customer_identities(business_id, customer_id, status)
        """
    )
