from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from clientplatform.application import bot_provisioning as bot_application
from clientplatform.application import managed_bot_onboarding as onboarding
from clientplatform.application import managed_bot_owner as owner_application
from clientplatform.domain.bot_provisioning import VerifiedTelegramBot
from clientplatform.domain.managed_bot_owner import ManagedBotWebhookMaterial
from clientplatform.infrastructure.managed_bot_credentials import (
    InMemoryManagedBotCredentialVault,
    ManagedBotCredentialError,
    ManagedBotCredentialStore,
)
from clientplatform.infrastructure.safe_tenancy_repository import TenancyRepository
from services.db.schema import (
    clientplatform_bot_provisioning,
    clientplatform_connections,
    clientplatform_tenancy,
)


class _Provisioner:
    async def provision(self, request):
        return VerifiedTelegramBot(
            external_bot_id="900001",
            username="practice_helper_bot",
            display_name="Помощник",
        )

    async def rollback(self, request) -> None:
        return None


class ManagedBotCredentialRevokeTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_revoke_erases_ciphertext_and_route_in_one_transaction(self) -> None:
        with (
            patch.object(onboarding, "get_db", self._db),
            patch.object(bot_application, "get_db", self._db),
            patch.object(bot_application, "get_db_ro", self._db),
        ):
            onboarding.begin_telegram_managed_bot_onboarding(
                actor=self.actor,
                idempotency_key="managed-revoke-001",
            )
            completed = await onboarding.complete_telegram_managed_bot_onboarding(
                user_id=self.actor.user_id,
                external_bot_id="900001",
                username="practice_helper_bot",
                display_name="Помощник",
                token="900001:" + ("A" * 40),
                vault=self.vault,
                provisioner=_Provisioner(),
            )

        assert completed.managed_bot_id is not None
        row = self.conn.execute(
            """
            SELECT bot.id AS managed_bot_id, bot.business_id, bot.connection_id,
                   bot.external_bot_id, bot.username, bot.webhook_secret_reference,
                   connection.credential_reference
            FROM managed_bots AS bot
            JOIN connections AS connection
              ON connection.id=bot.connection_id AND connection.business_id=bot.business_id
            WHERE bot.id=?
            """,
            (completed.managed_bot_id,),
        ).fetchone()
        material = ManagedBotWebhookMaterial(
            managed_bot_id=str(row["managed_bot_id"]),
            business_id=str(row["business_id"]),
            connection_id=str(row["connection_id"]),
            external_bot_id=str(row["external_bot_id"]),
            username=str(row["username"]),
            credential_reference=str(row["credential_reference"]),
            webhook_secret_reference=str(row["webhook_secret_reference"]),
        )
        reference = material.credential_reference
        self.assertTrue(reference.startswith("vault://managed-bot/"))
        self.assertTrue(ManagedBotCredentialStore(self.conn, vault=self.vault).resolve(reference))

        with (
            patch.object(owner_application, "get_db", self._db),
            patch.object(
                owner_application,
                "AgeManagedBotCredentialVault",
                return_value=self.vault,
            ),
        ):
            owner_application._revoke_managed_bot_and_credential(
                actor=self.actor,
                managed_bot_id=material.managed_bot_id,
                material=material,
            )

        connection = self.conn.execute(
            "SELECT status FROM connections WHERE id=?",
            (material.connection_id,),
        ).fetchone()
        bot = self.conn.execute(
            "SELECT status FROM managed_bots WHERE id=?",
            (material.managed_bot_id,),
        ).fetchone()
        credential = self.conn.execute(
            "SELECT status,ciphertext FROM managed_bot_credentials WHERE external_bot_id=?",
            (material.external_bot_id,),
        ).fetchone()
        self.assertEqual(connection["status"], "revoked")
        self.assertEqual(bot["status"], "revoked")
        self.assertEqual(tuple(credential), ("revoked", "revoked"))
        with self.assertRaises(ManagedBotCredentialError):
            ManagedBotCredentialStore(self.conn, vault=self.vault).resolve(reference)


if __name__ == "__main__":
    unittest.main()
