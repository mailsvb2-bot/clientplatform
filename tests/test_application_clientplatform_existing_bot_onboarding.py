from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from clientplatform.application import bot_provisioning as bot_application
from clientplatform.application import existing_bot_onboarding as application
from clientplatform.domain.bot_provisioning import (
    BotProvisioningStatus,
    BotProvisioningVerificationFailed,
    VerifiedTelegramBot,
)
from clientplatform.infrastructure.managed_bot_credentials import (
    InMemoryManagedBotCredentialVault,
)
from clientplatform.infrastructure.safe_tenancy_repository import TenancyRepository
from services.db.schema import (
    clientplatform_bot_provisioning,
    clientplatform_connections,
    clientplatform_tenancy,
)


class _Provisioner:
    def __init__(self, *, external_bot_id: str = "900001") -> None:
        self.external_bot_id = external_bot_id
        self.calls = 0
        self.rollback_calls = 0

    async def provision(self, request):
        self.calls += 1
        return VerifiedTelegramBot(
            external_bot_id=self.external_bot_id,
            username="existing_practice_bot",
            display_name="Existing Practice Bot",
        )

    async def rollback(self, request) -> None:
        self.rollback_calls += 1


class ClientPlatformExistingBotOnboardingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_connections.ensure(self.conn)
        clientplatform_bot_provisioning.ensure(self.conn)
        tenancy = TenancyRepository(self.conn)
        access = tenancy.create_business(owner_user_id=101, name="Практика")
        self.actor = tenancy.resolve_context(
            user_id=101,
            business_id=access.business.id,
        )
        self.conn.commit()
        self.vault = InMemoryManagedBotCredentialVault()

    def tearDown(self) -> None:
        self.conn.close()

    @contextmanager
    def _db(self):
        with self.conn:
            yield self.conn

    def _patch_db(self):
        return (
            patch.object(application, "get_db", self._db),
            patch.object(bot_application, "get_db", self._db),
            patch.object(bot_application, "get_db_ro", self._db),
        )

    async def test_existing_bot_connects_from_one_transient_token(self) -> None:
        raw_token = "900001:" + ("A" * 40)
        provisioner = _Provisioner()
        app_db, bot_db, bot_ro = self._patch_db()
        with app_db, bot_db, bot_ro:
            completed = await application.connect_existing_telegram_bot(
                actor=self.actor,
                token=raw_token,
                idempotency_key="existing-bot-test-001",
                vault=self.vault,
                provisioner=provisioner,
            )

        self.assertEqual(completed.status, BotProvisioningStatus.COMPLETED)
        self.assertEqual(completed.external_bot_id, "900001")
        self.assertEqual(completed.verified_username, "existing_practice_bot")
        self.assertEqual(provisioner.calls, 1)
        connection = self.conn.execute(
            "SELECT credential_reference,status FROM connections"
        ).fetchone()
        self.assertEqual(connection["status"], "active")
        self.assertTrue(
            connection["credential_reference"].startswith("vault://managed-bot/")
        )
        self.assertNotIn(raw_token, connection["credential_reference"])
        credential = self.conn.execute(
            "SELECT ciphertext,status FROM managed_bot_credentials"
        ).fetchone()
        self.assertEqual(credential["status"], "active")
        self.assertNotIn(raw_token, credential["ciphertext"])

    async def test_identity_mismatch_creates_no_active_route_and_revokes_secret(self) -> None:
        raw_token = "900001:" + ("B" * 40)
        provisioner = _Provisioner(external_bot_id="900002")
        app_db, bot_db, bot_ro = self._patch_db()
        with app_db, bot_db, bot_ro:
            with self.assertRaises(BotProvisioningVerificationFailed):
                await application.connect_existing_telegram_bot(
                    actor=self.actor,
                    token=raw_token,
                    idempotency_key="existing-bot-test-002",
                    vault=self.vault,
                    provisioner=provisioner,
                )

        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM connections").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM managed_bots").fetchone()[0],
            0,
        )
        credential = self.conn.execute(
            "SELECT ciphertext,status FROM managed_bot_credentials"
        ).fetchone()
        self.assertEqual(credential["status"], "revoked")
        self.assertNotIn(raw_token, credential["ciphertext"])

    async def test_invalid_token_is_rejected_before_database_write(self) -> None:
        app_db, bot_db, bot_ro = self._patch_db()
        with app_db, bot_db, bot_ro:
            with self.assertRaises(ValueError):
                await application.connect_existing_telegram_bot(
                    actor=self.actor,
                    token="not-a-token",
                    idempotency_key="existing-bot-test-003",
                    vault=self.vault,
                    provisioner=_Provisioner(),
                )

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM managed_bot_provisioning_requests"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM managed_bot_credentials"
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
