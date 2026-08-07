from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from clientplatform.application import bot_provisioning as bot_application
from clientplatform.application import managed_bot_onboarding as application
from clientplatform.domain.bot_provisioning import (
    BotProvisioningInvariantViolation,
    BotProvisioningNotFound,
    BotProvisioningProvider,
    BotProvisioningStatus,
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
    def __init__(self) -> None:
        self.calls = 0
        self.rollback_calls = 0

    async def provision(self, request):
        self.calls += 1
        return VerifiedTelegramBot(
            external_bot_id="900001",
            username="practice_helper_bot",
            display_name="Помощник практики",
        )

    async def rollback(self, request) -> None:
        self.rollback_calls += 1


class ClientPlatformManagedBotOnboardingApplicationTests(
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
        first = tenancy.create_business(owner_user_id=101, name="Практика")
        second = tenancy.create_business(owner_user_id=101, name="Второй проект")
        other = tenancy.create_business(owner_user_id=202, name="Чужой проект")
        self.first = tenancy.resolve_context(
            user_id=101,
            business_id=first.business.id,
        )
        self.second = tenancy.resolve_context(
            user_id=101,
            business_id=second.business.id,
        )
        self.other = tenancy.resolve_context(
            user_id=202,
            business_id=other.business.id,
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
            patch.object(application, "get_db_ro", self._db),
            patch.object(bot_application, "get_db", self._db),
            patch.object(bot_application, "get_db_ro", self._db),
        )

    async def test_creation_survives_without_fsm_and_completes_active_route(self) -> None:
        provisioner = _Provisioner()
        raw_token = "900001:" + ("A" * 40)
        managed_db, managed_ro, bot_db, bot_ro = self._patch_db()
        with managed_db, managed_ro, bot_db, bot_ro:
            request = application.begin_telegram_managed_bot_onboarding(
                actor=self.first,
                idempotency_key="managed-owner-ui-001",
                display_name="Практика",
            )
            self.assertFalse(
                application.has_active_telegram_managed_bot(actor=self.first)
            )
            # Completion intentionally has no FSM/request ID input. The durable
            # membership correlation must recover the request after a restart.
            completed = await application.complete_telegram_managed_bot_onboarding(
                user_id=101,
                external_bot_id="900001",
                username="practice_helper_bot",
                display_name="Помощник практики",
                token=raw_token,
                event_at=datetime.now(timezone.utc) + timedelta(seconds=1),
                vault=self.vault,
                provisioner=provisioner,
            )
            self.assertTrue(
                application.has_active_telegram_managed_bot(actor=self.first)
            )

        self.assertEqual(request.provider, BotProvisioningProvider.TELEGRAM_MANAGED)
        self.assertEqual(request.status, BotProvisioningStatus.AWAITING_SECRET)
        self.assertEqual(completed.status, BotProvisioningStatus.COMPLETED)
        self.assertEqual(completed.external_bot_id, "900001")
        self.assertEqual(provisioner.calls, 1)
        connection = self.conn.execute(
            "SELECT credential_reference,status FROM connections"
        ).fetchone()
        self.assertEqual(connection["status"], "active")
        self.assertTrue(connection["credential_reference"].startswith("vault://managed-bot/"))
        self.assertNotIn(raw_token, connection["credential_reference"])
        credential = self.conn.execute(
            "SELECT ciphertext FROM managed_bot_credentials"
        ).fetchone()
        self.assertNotIn(raw_token, credential["ciphertext"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM managed_bots").fetchone()[0],
            1,
        )

    async def test_one_user_cannot_start_two_managed_bot_creations(self) -> None:
        managed_db, managed_ro, bot_db, bot_ro = self._patch_db()
        with managed_db, managed_ro, bot_db, bot_ro:
            first = application.begin_telegram_managed_bot_onboarding(
                actor=self.first,
                idempotency_key="managed-owner-ui-002",
            )
            repeated = application.begin_telegram_managed_bot_onboarding(
                actor=self.first,
                idempotency_key="managed-owner-ui-003",
            )
            self.assertEqual(repeated.id, first.id)
            with self.assertRaisesRegex(
                BotProvisioningInvariantViolation,
                "finish the current managed bot setup",
            ):
                application.begin_telegram_managed_bot_onboarding(
                    actor=self.second,
                    idempotency_key="managed-owner-ui-004",
                )

    async def test_foreign_user_cannot_claim_another_users_creation_event(self) -> None:
        raw_token = "900001:" + ("A" * 40)
        managed_db, managed_ro, bot_db, bot_ro = self._patch_db()
        with managed_db, managed_ro, bot_db, bot_ro:
            application.begin_telegram_managed_bot_onboarding(
                actor=self.first,
                idempotency_key="managed-owner-ui-005",
            )
            with self.assertRaises(BotProvisioningNotFound) as caught:
                await application.complete_telegram_managed_bot_onboarding(
                    user_id=self.other.user_id,
                    external_bot_id="900001",
                    username="practice_helper_bot",
                    display_name=None,
                    token=raw_token,
                    vault=self.vault,
                    provisioner=_Provisioner(),
                )
        self.assertNotIn(raw_token, str(caught.exception))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM managed_bot_credentials").fetchone()[0],
            0,
        )

    async def test_stale_creation_message_cannot_bind_to_new_request(self) -> None:
        raw_token = "900001:" + ("A" * 40)
        managed_db, managed_ro, bot_db, bot_ro = self._patch_db()
        with managed_db, managed_ro, bot_db, bot_ro:
            request = application.begin_telegram_managed_bot_onboarding(
                actor=self.first,
                idempotency_key="managed-owner-ui-006",
            )
            stale = datetime.fromisoformat(request.created_at) - timedelta(seconds=1)
            with self.assertRaisesRegex(
                BotProvisioningInvariantViolation,
                "predates the active request",
            ):
                await application.complete_telegram_managed_bot_onboarding(
                    user_id=self.first.user_id,
                    external_bot_id="900001",
                    username="practice_helper_bot",
                    display_name=None,
                    token=raw_token,
                    event_at=stale,
                    vault=self.vault,
                    provisioner=_Provisioner(),
                )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM managed_bot_credentials").fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
