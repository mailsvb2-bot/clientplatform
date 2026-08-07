from __future__ import annotations

import sqlite3
import unittest

from clientplatform.domain.bot_provisioning import BotProvisioningProvider
from clientplatform.infrastructure.bot_provisioning_repository import (
    BotProvisioningRepository,
)
from clientplatform.infrastructure.safe_tenancy_repository import TenancyRepository
from services.db.schema import clientplatform_connections, clientplatform_tenancy
from services.migrations import clientplatform_managed_bot_provider_v1 as migration


_OLD_PROVISIONING_DDL = """
CREATE TABLE managed_bot_provisioning_requests(
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
    CHECK(provider='botfather'),
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


class ClientPlatformManagedBotProviderMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_connections.ensure(self.conn)
        self.conn.execute(_OLD_PROVISIONING_DDL)
        tenancy = TenancyRepository(self.conn)
        access = tenancy.create_business(owner_user_id=101, name="Практика")
        self.actor = tenancy.resolve_context(
            user_id=101,
            business_id=access.business.id,
        )
        self.repository = BotProvisioningRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_existing_botfather_row_survives_and_managed_provider_becomes_valid(self) -> None:
        legacy = self.repository.create_request(
            actor=self.actor,
            provider=BotProvisioningProvider.BOTFATHER,
            idempotency_key="legacy-botfather-001",
        )
        self.conn.commit()

        migration.apply(self.conn)
        managed = self.repository.create_request(
            actor=self.actor,
            provider=BotProvisioningProvider.TELEGRAM_MANAGED,
            idempotency_key="managed-native-001",
        )
        self.conn.commit()

        rows = self.conn.execute(
            """
            SELECT id,provider FROM managed_bot_provisioning_requests
            ORDER BY created_at,id
            """
        ).fetchall()
        self.assertEqual({row["id"] for row in rows}, {legacy.id, managed.id})
        self.assertEqual(
            {row["provider"] for row in rows},
            {"botfather", "telegram_managed"},
        )
        ddl = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='managed_bot_provisioning_requests'"
        ).fetchone()[0]
        self.assertIn("telegram_managed", ddl)


if __name__ == "__main__":
    unittest.main()
