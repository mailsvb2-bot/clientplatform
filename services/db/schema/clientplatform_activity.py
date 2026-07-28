from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Create the extensible business activity, offerings and customer invite boundary."""
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS business_profiles(
            business_id TEXT PRIMARY KEY,
            activity_description TEXT NOT NULL,
            timezone TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(status IN ('draft', 'ready'))
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS business_capabilities(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            connector_key TEXT NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            UNIQUE(business_id, connector_key),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(kind IN ('programs', 'consultations', 'services', 'custom')),
            CHECK(status IN ('active', 'disabled'))
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS business_offerings(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            capability_id TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT,
            UNIQUE(id, business_id),
            FOREIGN KEY(capability_id, business_id)
                REFERENCES business_capabilities(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(status IN ('active', 'archived'))
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS customer_invites(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'active',
            expires_at TEXT NOT NULL,
            created_by_member_id TEXT NOT NULL,
            claimed_customer_id TEXT,
            created_at TEXT NOT NULL,
            claimed_at TEXT,
            revoked_at TEXT,
            UNIQUE(id, business_id),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            FOREIGN KEY(claimed_customer_id, business_id)
                REFERENCES customers(id, business_id),
            CHECK(status IN ('active', 'claimed', 'revoked', 'expired'))
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_business_capabilities_active
        ON business_capabilities(business_id, status, connector_key)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_business_offerings_active
        ON business_offerings(business_id, capability_id, status, created_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_customer_invites_business_status
        ON customer_invites(business_id, status, expires_at)
        """
    )
