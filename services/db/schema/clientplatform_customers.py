from __future__ import annotations

import sqlite3

from services.schema_core import _add_col, _cols


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
            first_contact_at TEXT,
            last_contact_at TEXT,
            UNIQUE(id, business_id),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(status IN ('active', 'archived'))
        )
        """
    )
    customer_columns = _cols(c, "customers")
    if "first_contact_at" not in customer_columns:
        _add_col(c, "customers", "first_contact_at TEXT")
    if "last_contact_at" not in customer_columns:
        _add_col(c, "customers", "last_contact_at TEXT")
    c.execute(
        """
        UPDATE customers
        SET first_contact_at=COALESCE(first_contact_at, created_at),
            last_contact_at=COALESCE(last_contact_at, updated_at, created_at)
        WHERE first_contact_at IS NULL OR last_contact_at IS NULL
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
            first_contact_at TEXT,
            last_contact_at TEXT,
            UNIQUE(business_id, platform, external_subject),
            UNIQUE(id, business_id),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(customer_id, business_id)
                REFERENCES customers(id, business_id) ON DELETE CASCADE,
            CHECK(status IN ('active', 'revoked'))
        )
        """
    )
    identity_columns = _cols(c, "customer_identities")
    if "first_contact_at" not in identity_columns:
        _add_col(c, "customer_identities", "first_contact_at TEXT")
    if "last_contact_at" not in identity_columns:
        _add_col(c, "customer_identities", "last_contact_at TEXT")
    c.execute(
        """
        UPDATE customer_identities
        SET first_contact_at=COALESCE(first_contact_at, created_at),
            last_contact_at=COALESCE(last_contact_at, updated_at, created_at)
        WHERE first_contact_at IS NULL OR last_contact_at IS NULL
        """
    )

    c.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_identity_tenant_platform
        ON customer_identities(id, business_id, platform)
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
        CREATE INDEX IF NOT EXISTS idx_customers_business_contact
        ON customers(business_id, status, last_contact_at, first_contact_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_customer_identities_customer_status
        ON customer_identities(business_id, customer_id, status)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_customer_identities_contact
        ON customer_identities(business_id, platform, status, last_contact_at)
        """
    )
