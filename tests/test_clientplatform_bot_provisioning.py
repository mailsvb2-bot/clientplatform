from __future__ import annotations

import sqlite3
import unittest

from clientplatform.domain.bot_provisioning import (
    BotProvisioningInvariantViolation,
    BotProvisioningNotFound,
    BotProvisioningStatus,
    VerifiedTelegramBot,
)
from clientplatform.domain.connections import ConnectionInvariantViolation
from clientplatform.infrastructure.bot_provisioning_repository import (
    BotProvisioningRepository,
)
from clientplatform.infrastructure.safe_tenancy_repository import TenancyRepository
from services.db.schema import (
    clientplatform_bot_gateway,
    clientplatform_bot_provisioning,
    clientplatform_connections,
    clientplatform_customers,
    clientplatform_tenancy,
)


class ClientPlatformBotProvisioningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_connections.ensure(self.conn)
        clientplatform_bot_gateway.ensure(self.conn)
        clientplatform_bot_provisioning.ensure(self.conn)
        tenancy = TenancyRepository(self.conn)
        first = tenancy.create_business(owner_user_id=101, name="Практика один")
        second = tenancy.create_business(owner_user_id=202, name="Практика два")
        self.owner = tenancy.resolve_context(
            user_id=101,
            business_id=first.business.id,
        )
        self.other_owner = tenancy.resolve_context(
            user_id=202,
            business_id=second.business.id,
        )
        self.repo = BotProvisioningRepository(self.conn)
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def _create(self, *, key: str = "connect-primary-bot"):
        return self.repo.create_request(
            actor=self.owner,
            idempotency_key=key,
            requested_username="practice_helper_bot",
            display_name="Помощник практики",
            now="2026-07-29T09:00:00+00:00",
        )

    def _ready(self, *, key: str = "connect-primary-bot"):
        request = self._create(key=key)
        return self.repo.submit_secret_references(
            actor=self.owner,
            request_id=request.id,
            credential_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_PRIMARY"
            ),
            webhook_secret_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_WEBHOOK_PRIMARY"
            ),
            now="2026-07-29T09:01:00+00:00",
        )

    def test_create_is_idempotent_and_tenant_scoped(self) -> None:
        first = self._create()
        repeated = self._create()
        self.assertEqual(first.id, repeated.id)
        self.assertEqual(first.status, BotProvisioningStatus.AWAITING_SECRET)
        with self.assertRaises(BotProvisioningNotFound):
            self.repo.get(actor=self.other_owner, request_id=first.id)

    def test_raw_token_is_rejected_before_database_write(self) -> None:
        request = self._create()
        with self.assertRaises(ConnectionInvariantViolation):
            self.repo.submit_secret_references(
                actor=self.owner,
                request_id=request.id,
                credential_reference="123456:raw-token-must-not-enter-db",
                webhook_secret_reference=(
                    "secret://env/CLIENTPLATFORM_SECRET_WEBHOOK_PRIMARY"
                ),
            )
        row = self.conn.execute(
            """
            SELECT credential_reference, webhook_secret_reference, status
            FROM managed_bot_provisioning_requests
            WHERE id=?
            """,
            (request.id,),
        ).fetchone()
        self.assertIsNone(row["credential_reference"])
        self.assertIsNone(row["webhook_secret_reference"])
        self.assertEqual(row["status"], "awaiting_secret")

    def test_verification_lease_and_atomic_completion(self) -> None:
        ready = self._ready()
        lease = self.repo.begin_verification(
            actor=self.owner,
            request_id=ready.id,
            now="2026-07-29T09:02:00+00:00",
        )
        with self.assertRaises(BotProvisioningInvariantViolation):
            self.repo.begin_verification(
                actor=self.owner,
                request_id=ready.id,
            )
        completed = self.repo.complete_verified(
            actor=self.owner,
            lease=lease,
            verified_bot=VerifiedTelegramBot(
                external_bot_id="900001",
                username="practice_helper_bot",
                display_name="Помощник практики",
            ),
            now="2026-07-29T09:03:00+00:00",
        )
        self.assertEqual(completed.status, BotProvisioningStatus.COMPLETED)
        self.assertIsNotNone(completed.connection_id)
        self.assertIsNotNone(completed.managed_bot_id)
        connection = self.conn.execute(
            "SELECT status, credential_reference FROM connections WHERE id=?",
            (completed.connection_id,),
        ).fetchone()
        managed_bot = self.conn.execute(
            "SELECT status, external_bot_id FROM managed_bots WHERE id=?",
            (completed.managed_bot_id,),
        ).fetchone()
        self.assertEqual(connection["status"], "active")
        self.assertTrue(connection["credential_reference"].startswith("secret://"))
        self.assertEqual(managed_bot["status"], "active")
        self.assertEqual(managed_bot["external_bot_id"], "900001")
        repeated = self.repo.complete_verified(
            actor=self.owner,
            lease=lease,
            verified_bot=VerifiedTelegramBot(
                external_bot_id="900001",
                username="practice_helper_bot",
            ),
        )
        self.assertEqual(repeated.id, completed.id)

    def test_failed_verification_can_be_rearmed_with_new_references(self) -> None:
        ready = self._ready()
        lease = self.repo.begin_verification(
            actor=self.owner,
            request_id=ready.id,
        )
        failed = self.repo.fail_verification(
            actor=self.owner,
            lease=lease,
            error_code="telegram_verification_failed",
        )
        self.assertEqual(failed.status, BotProvisioningStatus.FAILED)
        self.assertEqual(failed.attempts, 1)
        rearmed = self.repo.submit_secret_references(
            actor=self.owner,
            request_id=failed.id,
            credential_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_RETRY"
            ),
            webhook_secret_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_WEBHOOK_RETRY"
            ),
        )
        self.assertEqual(rearmed.status, BotProvisioningStatus.READY)
        self.assertIsNone(rearmed.last_error_code)
        second_lease = self.repo.begin_verification(
            actor=self.owner,
            request_id=rearmed.id,
        )
        self.assertNotEqual(lease.verification_token, second_lease.verification_token)
        self.assertEqual(second_lease.request.attempts, 2)

    def test_cancel_clears_secret_references_and_is_idempotent(self) -> None:
        ready = self._ready()
        cancelled = self.repo.cancel(
            actor=self.owner,
            request_id=ready.id,
        )
        repeated = self.repo.cancel(
            actor=self.owner,
            request_id=ready.id,
        )
        self.assertEqual(cancelled.status, BotProvisioningStatus.CANCELLED)
        self.assertEqual(repeated.status, BotProvisioningStatus.CANCELLED)
        self.assertIsNone(cancelled.credential_reference)
        self.assertIsNone(cancelled.webhook_secret_reference)
        with self.assertRaises(BotProvisioningInvariantViolation):
            self.repo.begin_verification(
                actor=self.owner,
                request_id=ready.id,
            )

    def test_verified_username_must_match_requested_username(self) -> None:
        ready = self._ready()
        lease = self.repo.begin_verification(
            actor=self.owner,
            request_id=ready.id,
        )
        with self.assertRaises(BotProvisioningInvariantViolation):
            self.repo.complete_verified(
                actor=self.owner,
                lease=lease,
                verified_bot=VerifiedTelegramBot(
                    external_bot_id="900002",
                    username="foreign_helper_bot",
                ),
            )
        failed = self.repo.fail_verification(
            actor=self.owner,
            lease=lease,
            error_code="telegram_identity_mismatch",
        )
        self.assertEqual(failed.status, BotProvisioningStatus.FAILED)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM connections").fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
