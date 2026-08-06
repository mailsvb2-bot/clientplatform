from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS ad_spend_operations(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            authorization_id TEXT NOT NULL,
            operation_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            idempotency_key TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at TEXT NOT NULL,
            locked_at TEXT,
            lock_token TEXT,
            last_error_code TEXT,
            provider_evidence_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            dead_at TEXT,
            UNIQUE(id, business_id),
            UNIQUE(business_id, authorization_id, operation_type),
            UNIQUE(business_id, idempotency_key),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(authorization_id, business_id)
                REFERENCES ad_spend_authorizations(id, business_id) ON DELETE CASCADE,
            CHECK(operation_type IN ('launch', 'stop')),
            CHECK(status IN ('queued', 'processing', 'retry', 'succeeded', 'failed')),
            CHECK(attempts >= 0)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ad_spend_operations_due
        ON ad_spend_operations(status, available_at, locked_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ad_spend_operations_business
        ON ad_spend_operations(business_id, authorization_id, status, created_at)
        """
    )
