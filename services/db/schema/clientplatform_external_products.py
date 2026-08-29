from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Create business-scoped external product connectors and verified receipts."""

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS external_product_connectors(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            product_key TEXT NOT NULL,
            display_name TEXT NOT NULL,
            webhook_secret_reference TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            activated_at TEXT,
            disabled_at TEXT,
            revoked_at TEXT,
            last_event_at TEXT,
            last_error_at TEXT,
            last_error_code TEXT,
            UNIQUE(id, business_id),
            UNIQUE(business_id, product_key),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(length(product_key) BETWEEN 2 AND 64),
            CHECK(length(display_name) BETWEEN 1 AND 160),
            CHECK(
                substr(webhook_secret_reference, 1, 9)='secret://'
                OR substr(webhook_secret_reference, 1, 6)='kms://'
                OR substr(webhook_secret_reference, 1, 8)='vault://'
            ),
            CHECK(status IN ('pending','active','attention','disabled','revoked'))
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_external_product_connectors_business_status
        ON external_product_connectors(business_id, status, product_key)
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS external_product_event_receipts(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            connector_id TEXT NOT NULL,
            external_event_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            customer_id TEXT,
            customer_fingerprint TEXT,
            payload_fingerprint TEXT NOT NULL,
            outcome_event_id TEXT,
            occurred_at TEXT NOT NULL,
            received_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'accepted',
            UNIQUE(id, business_id),
            UNIQUE(business_id, connector_id, external_event_id),
            FOREIGN KEY(connector_id, business_id)
                REFERENCES external_product_connectors(id, business_id)
                ON DELETE CASCADE,
            FOREIGN KEY(customer_id, business_id)
                REFERENCES customers(id, business_id),
            FOREIGN KEY(business_id, outcome_event_id)
                REFERENCES business_outcome_events(business_id, id),
            CHECK(event_type IN (
                'evidence','lead_created','lead_qualified',
                'order_paid','refund_recorded'
            )),
            CHECK(
                (event_type='evidence' AND customer_id IS NULL AND customer_fingerprint IS NULL)
                OR
                (customer_id IS NOT NULL AND length(customer_fingerprint)=64)
            ),
            CHECK(length(payload_fingerprint)=64),
            CHECK(status IN ('accepted'))
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_external_product_receipts_customer_time
        ON external_product_event_receipts(
            business_id, customer_id, occurred_at, external_event_id
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_external_product_receipts_connector_time
        ON external_product_event_receipts(
            business_id, connector_id, received_at, external_event_id
        )
        """
    )
