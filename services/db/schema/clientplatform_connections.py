from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Create secret-reference connections and a transport-neutral dispatch outbox."""
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS connections(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            connection_type TEXT NOT NULL,
            external_account_id TEXT NOT NULL,
            credential_reference TEXT NOT NULL,
            permissions_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'pending',
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_success_at TEXT,
            last_error_at TEXT,
            last_error_code TEXT,
            UNIQUE(id, business_id),
            UNIQUE(id, business_id, platform),
            UNIQUE(business_id, platform, connection_type, external_account_id),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(platform IN ('telegram', 'vk', 'max')),
            CHECK(connection_type IN (
                'telegram_shared_bot', 'telegram_managed_bot',
                'telegram_business', 'telegram_channel',
                'vk_community', 'max_shared_bot', 'max_personal_bot'
            )),
            CHECK(
                (platform='telegram' AND connection_type IN (
                    'telegram_shared_bot', 'telegram_managed_bot',
                    'telegram_business', 'telegram_channel'
                ))
                OR (platform='vk' AND connection_type='vk_community')
                OR (platform='max' AND connection_type IN (
                    'max_shared_bot', 'max_personal_bot'
                ))
            ),
            CHECK(
                substr(credential_reference, 1, 9)='secret://'
                OR substr(credential_reference, 1, 6)='kms://'
                OR substr(credential_reference, 1, 8)='vault://'
            ),
            CHECK(status IN ('pending', 'active', 'attention', 'disabled', 'revoked'))
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS managed_bots(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            connection_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            external_bot_id TEXT NOT NULL,
            username TEXT,
            display_name TEXT,
            webhook_secret_reference TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            revoked_at TEXT,
            UNIQUE(id, business_id),
            UNIQUE(business_id, platform, external_bot_id),
            UNIQUE(business_id, connection_id),
            FOREIGN KEY(connection_id, business_id, platform)
                REFERENCES connections(id, business_id, platform) ON DELETE CASCADE,
            CHECK(platform IN ('telegram', 'max')),
            CHECK(
                substr(webhook_secret_reference, 1, 9)='secret://'
                OR substr(webhook_secret_reference, 1, 6)='kms://'
                OR substr(webhook_secret_reference, 1, 8)='vault://'
            ),
            CHECK(status IN ('active', 'disabled', 'revoked'))
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS connection_credentials(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            external_account_id TEXT NOT NULL,
            purpose TEXT NOT NULL,
            ciphertext TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            revoked_at TEXT,
            UNIQUE(id, business_id),
            UNIQUE(business_id, platform, external_account_id, purpose),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            CHECK(platform IN ('vk', 'max')),
            CHECK(purpose IN ('provider_token', 'webhook_secret', 'confirmation_code')),
            CHECK(status IN ('active', 'revoked')),
            CHECK(length(ciphertext) > 0)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_connection_credentials_business_status
        ON connection_credentials(business_id, platform, status, updated_at)
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS managed_bot_credentials(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            external_bot_id TEXT NOT NULL,
            ciphertext TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            revoked_at TEXT,
            UNIQUE(id, business_id),
            UNIQUE(business_id, external_bot_id),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            CHECK(status IN ('active', 'revoked')),
            CHECK(length(ciphertext) > 0)
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS delivery_dispatch_outbox(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            logical_delivery_id TEXT NOT NULL,
            connection_id TEXT NOT NULL,
            customer_identity_id TEXT NOT NULL,
            payload_kind TEXT NOT NULL,
            payload_ref TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at TEXT NOT NULL,
            locked_at TEXT,
            lock_token TEXT,
            provider_message_id TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            sent_at TEXT,
            dead_at TEXT,
            UNIQUE(id, business_id),
            UNIQUE(business_id, logical_delivery_id, connection_id, customer_identity_id),
            UNIQUE(business_id, idempotency_key),
            FOREIGN KEY(logical_delivery_id, business_id)
                REFERENCES lesson_deliveries(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(connection_id, business_id, platform)
                REFERENCES connections(id, business_id, platform),
            FOREIGN KEY(customer_identity_id, business_id, platform)
                REFERENCES customer_identities(id, business_id, platform),
            CHECK(platform IN ('telegram', 'vk', 'max')),
            CHECK(payload_kind IN (
                'audio', 'video', 'text', 'document', 'image',
                'link', 'task', 'mixed'
            )),
            CHECK(status IN (
                'pending', 'sending', 'retry', 'sent', 'dead', 'cancelled'
            )),
            CHECK(attempts >= 0)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_connections_business_status
        ON connections(business_id, platform, status, created_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_managed_bots_business_status
        ON managed_bots(business_id, platform, status)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_managed_bot_credentials_business_status
        ON managed_bot_credentials(business_id, status, updated_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dispatch_outbox_due
        ON delivery_dispatch_outbox(status, available_at, locked_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dispatch_outbox_business_delivery
        ON delivery_dispatch_outbox(business_id, logical_delivery_id, status)
        """
    )
