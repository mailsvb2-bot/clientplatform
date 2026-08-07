from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from clientplatform.application import bot_provisioning as bot_application
from clientplatform.application import managed_bot_onboarding as application
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


class _WrongChildProvisioner:
    async def provision(self, request):
        return VerifiedTelegramBot(
            external_bot_id="900002",
            username="other_helper_bot",
            display_name="Другой бот",
        )

    async def rollback(self, request) -> None:
        return None


class ClientPlatformManagedBotIdentityBindingTests(
    unittest.IsolatedAsyncioTestCase
):
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

    async def test_token_for_another_child_cannot_create_active_route(self) -> None:
        with (
            patch.object(application, "get_db", self._db),
            patch.object(application, "get_db_ro", self._db),
            patch.object(bot_application, "get_db", self._db),
            patch.object(bot_application, "get_db_ro", self._db),
        ):
            request = application.begin_telegram_managed_bot_onboarding(
                actor=self.actor,
                idempotency_key="managed-identity-binding-001",
            )
            with self.assertRaisesRegex(
                BotProvisioningVerificationFailed,
                "token identity does not match",
            ):
                await application.complete_telegram_managed_bot_onboarding(
                    user_id=self.actor.user_id,
                    external_bot_id="900001",
                    username="practice_helper_bot",
                    display_name="Помощник",
                    token="900001:" + ("A" * 40),
                    vault=self.vault,
                    provisioner=_WrongChildProvisioner(),
                )

        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM connections").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM managed_bots").fetchone()[0],
            0,
        )
        status = self.conn.execute(
            "SELECT status,last_error_code FROM managed_bot_provisioning_requests WHERE id=?",
            (request.id,),
        ).fetchone()
        self.assertEqual(
            tuple(status),
            (BotProvisioningStatus.FAILED.value, "telegram_verification_failed"),
        )


if __name__ == "__main__":
    unittest.main()
