from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Create the durable tenant-scoped ingress boundary for managed bots."""
    c.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_managed_bots_platform_external
        ON managed_bots(platform, external_bot_id)
        """
    )
    c.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_managed_bots_active_business_platform
        ON managed_bots(business_id, platform)
        WHERE status='active'
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_gateway_ingress_events(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            managed_bot_id TEXT NOT NULL,
            provider_update_id TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            payload_json TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at TEXT NOT NULL,
            locked_at TEXT,
            lock_token TEXT,
            last_error_code TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            processed_at TEXT,
            dead_at TEXT,
            UNIQUE(id, business_id),
            UNIQUE(managed_bot_id, provider_update_id),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(managed_bot_id, business_id)
                REFERENCES managed_bots(id, business_id) ON DELETE CASCADE,
            CHECK(length(payload_sha256)=64),
            CHECK(status IN ('pending', 'processing', 'retry', 'processed', 'dead')),
            CHECK(attempts >= 0)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bot_gateway_ingress_due
        ON bot_gateway_ingress_events(status, available_at, locked_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bot_gateway_ingress_bot_status
        ON bot_gateway_ingress_events(managed_bot_id, status, created_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bot_gateway_ingress_business_status
        ON bot_gateway_ingress_events(business_id, status, created_at)
        """
    )
