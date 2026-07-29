from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from clientplatform.application import bot_provisioning as application
from clientplatform.domain.bot_provisioning import (
    BotProvisioningStatus,
    BotProvisioningVerificationFailed,
    VerifiedTelegramBot,
)
from clientplatform.domain.connections import ConnectionInvariantViolation
from clientplatform.infrastructure.safe_connection_repository import ConnectionRepository
from clientplatform.infrastructure.safe_tenancy_repository import TenancyRepository
from services.db.schema import (
    clientplatform_bot_gateway,
    clientplatform_bot_provisioning,
    clientplatform_connections,
    clientplatform_customers,
    clientplatform_tenancy,
)


class _FakeProvisioner:
    def __init__(
        self,
        *,
        verified: VerifiedTelegramBot | None = None,
        error: Exception | None = None,
    ) -> None:
        self.verified = verified or VerifiedTelegramBot(
            external_bot_id="900001",
            username="practice_helper_bot",
            display_name="Помощник практики",
        )
        self.error = error
        self.provision_calls = 0
        self.rollback_calls = 0

    async def provision(self, request):
        self.provision_calls += 1
        if self.error is not None:
            raise self.error
        return self.verified

    async def rollback(self, request) -> None:
        self.rollback_calls += 1


class ClientPlatformBotProvisioningApplicationTests(
    unittest.IsolatedAsyncioTestCase
):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_connections.ensure(self.conn)
        clientplatform_bot_gateway.ensure(self.conn)
        clientplatform_bot_provisioning.ensure(self.conn)
        tenancy = TenancyRepository(self.conn)
        access = tenancy.create_business(owner_user_id=101, name="Практика")
        self.owner = tenancy.resolve_context(
            user_id=101,
            business_id=access.business.id,
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    @contextmanager
    def _db(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _patch_db(self):
        return (
            patch.object(application, "get_db", self._db),
            patch.object(application, "get_db_ro", self._db),
        )

    def _prepare_request(self):
        request = application.create_botfather_provisioning(
            actor=self.owner,
            idempotency_key="connect-primary-bot",
            requested_username="practice_helper_bot",
            display_name="Помощник практики",
        )
        return application.submit_botfather_secret_references(
            actor=self.owner,
            request_id=request.id,
            credential_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_PRIMARY"
            ),
            webhook_secret_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_WEBHOOK_PRIMARY"
            ),
        )

    async def test_finalize_creates_active_route_and_is_idempotent(self) -> None:
        get_db_patch, get_db_ro_patch = self._patch_db()
        with get_db_patch, get_db_ro_patch:
            request = self._prepare_request()
            provisioner = _FakeProvisioner()
            completed = await application.finalize_botfather_provisioning(
                actor=self.owner,
                request_id=request.id,
                provisioner=provisioner,
            )
            repeated = await application.finalize_botfather_provisioning(
                actor=self.owner,
                request_id=request.id,
                provisioner=provisioner,
            )
        self.assertEqual(completed.status, BotProvisioningStatus.COMPLETED)
        self.assertEqual(repeated.id, completed.id)
        self.assertEqual(provisioner.provision_calls, 1)
        self.assertEqual(provisioner.rollback_calls, 0)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM managed_bots WHERE status='active'"
            ).fetchone()[0],
            1,
        )

    async def test_verification_failure_is_durable_without_connection(self) -> None:
        get_db_patch, get_db_ro_patch = self._patch_db()
        with get_db_patch, get_db_ro_patch:
            request = self._prepare_request()
            provisioner = _FakeProvisioner(
                error=BotProvisioningVerificationFailed("verification failed")
            )
            with self.assertRaises(BotProvisioningVerificationFailed):
                await application.finalize_botfather_provisioning(
                    actor=self.owner,
                    request_id=request.id,
                    provisioner=provisioner,
                )
            failed = application.get_bot_provisioning(
                actor=self.owner,
                request_id=request.id,
            )
        self.assertEqual(failed.status, BotProvisioningStatus.FAILED)
        self.assertEqual(failed.last_error_code, "telegram_verification_failed")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM connections").fetchone()[0],
            0,
        )

    async def test_database_conflict_rolls_back_configured_webhook(self) -> None:
        connections = ConnectionRepository(self.conn)
        existing_connection = connections.create_connection(
            actor=self.owner,
            platform="telegram",
            connection_type="telegram_managed_bot",
            external_account_id="800001",
            credential_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_EXISTING"
            ),
        )
        connections.activate_connection(
            actor=self.owner,
            connection_id=existing_connection.id,
        )
        connections.register_managed_bot(
            actor=self.owner,
            connection_id=existing_connection.id,
            external_bot_id="800001",
            webhook_secret_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_WEBHOOK_EXISTING"
            ),
            username="existing_helper_bot",
        )
        self.conn.commit()

        get_db_patch, get_db_ro_patch = self._patch_db()
        with get_db_patch, get_db_ro_patch:
            request = self._prepare_request()
            provisioner = _FakeProvisioner(
                verified=VerifiedTelegramBot(
                    external_bot_id="900002",
                    username="practice_helper_bot",
                )
            )
            with self.assertRaises(ConnectionInvariantViolation):
                await application.finalize_botfather_provisioning(
                    actor=self.owner,
                    request_id=request.id,
                    provisioner=provisioner,
                )
            failed = application.get_bot_provisioning(
                actor=self.owner,
                request_id=request.id,
            )
        self.assertEqual(provisioner.rollback_calls, 1)
        self.assertEqual(failed.status, BotProvisioningStatus.FAILED)
        self.assertEqual(failed.last_error_code, "provisioning_commit_failed")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM managed_bots").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM connections").fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
