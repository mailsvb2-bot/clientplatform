from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Create the durable Telegram bot provisioning state machine."""
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS managed_bot_provisioning_requests(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            created_by_member_id TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT 'botfather',
            status TEXT NOT NULL DEFAULT 'awaiting_secret',
            idempotency_key TEXT NOT NULL,
            requested_username TEXT,
            display_name TEXT,
            credential_reference TEXT,
            webhook_secret_reference TEXT,
            external_bot_id TEXT,
            verified_username TEXT,
            connection_id TEXT,
            managed_bot_id TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            verification_token TEXT,
            verification_started_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            failed_at TEXT,
            cancelled_at TEXT,
            last_error_code TEXT,
            UNIQUE(id, business_id),
            UNIQUE(business_id, provider, idempotency_key),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            FOREIGN KEY(connection_id, business_id)
                REFERENCES connections(id, business_id),
            FOREIGN KEY(managed_bot_id, business_id)
                REFERENCES managed_bots(id, business_id),
            CHECK(provider IN ('telegram_managed', 'botfather')),
            CHECK(status IN (
                'awaiting_secret', 'ready', 'verifying',
                'completed', 'failed', 'cancelled'
            )),
            CHECK(attempts >= 0),
            CHECK(
                credential_reference IS NULL
                OR substr(credential_reference, 1, 9)='secret://'
                OR substr(credential_reference, 1, 6)='kms://'
                OR substr(credential_reference, 1, 8)='vault://'
            ),
            CHECK(
                webhook_secret_reference IS NULL
                OR substr(webhook_secret_reference, 1, 9)='secret://'
                OR substr(webhook_secret_reference, 1, 6)='kms://'
                OR substr(webhook_secret_reference, 1, 8)='vault://'
            ),
            CHECK(
                status NOT IN ('ready','verifying','completed')
                OR (
                    credential_reference IS NOT NULL
                    AND webhook_secret_reference IS NOT NULL
                )
            ),
            CHECK(
                status!='verifying'
                OR (
                    verification_token IS NOT NULL
                    AND verification_started_at IS NOT NULL
                )
            ),
            CHECK(
                status!='completed'
                OR (
                    external_bot_id IS NOT NULL
                    AND verified_username IS NOT NULL
                    AND connection_id IS NOT NULL
                    AND managed_bot_id IS NOT NULL
                    AND completed_at IS NOT NULL
                    AND verification_token IS NULL
                )
            )
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_managed_bot_provisioning_business_status
        ON managed_bot_provisioning_requests(business_id, status, created_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_managed_bot_provisioning_verifying
        ON managed_bot_provisioning_requests(status, verification_started_at)
        """
    )
