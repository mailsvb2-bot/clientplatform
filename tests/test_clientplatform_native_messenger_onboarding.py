from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from clientplatform.application.native_messenger_onboarding import (
    provision_max_channel,
    provision_vk_channel,
)
from clientplatform.infrastructure.managed_bot_credentials import InMemoryManagedBotCredentialVault
from clientplatform.infrastructure.safe_tenancy_repository import TenancyRepository
from services.db.schema import (
    clientplatform_connections,
    clientplatform_messenger_channels,
    clientplatform_tenancy,
)


class _FakeMaxSender:
    def __init__(self, *, fail_subscription: bool = False) -> None:
        self.fail_subscription = fail_subscription
        self.subscription: tuple[str, str] | None = None

    async def get_me(self):
        return {
            "user_id": 880001,
            "first_name": "MAX Бот",
            "username": "max_business_bot",
            "is_bot": True,
        }

    async def ensure_webhook_subscription(self, *, url: str, secret: str):
        if self.fail_subscription:
            raise RuntimeError("provider unavailable")
        self.subscription = (url, secret)
        return {"success": True}


class _FakeVkSender:
    def __init__(self) -> None:
        self.callback: tuple[str, str, str] | None = None

    async def verify_community(self, group_id: str):
        return {"id": int(group_id), "name": "Практика VK"}

    async def get_callback_confirmation_code(self, group_id: str):
        del group_id
        return "vk-confirmation-code"

    async def ensure_callback_server(self, *, group_id: str, url: str, secret: str):
        self.callback = (str(group_id), url, secret)
        return 77

class NativeMessengerOnboardingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_connections.ensure(self.conn)
        clientplatform_messenger_channels.ensure(self.conn)
        tenancy = TenancyRepository(self.conn)
        business = tenancy.create_business(owner_user_id=101, name="Практика")
        self.actor = tenancy.resolve_context(
            user_id=101,
            business_id=business.business.id,
        )
        self.vault = InMemoryManagedBotCredentialVault()

    def tearDown(self) -> None:
        self.conn.close()

    def _db_patch(self):
        @contextmanager
        def local_db():
            yield self.conn

        return patch(
            "clientplatform.application.native_messenger_onboarding.get_db",
            local_db,
        )

    async def test_max_verifies_then_encrypts_and_registers_canonical_webhook(self) -> None:
        sender = _FakeMaxSender()
        with self._db_patch():
            result = await provision_max_channel(
                actor=self.actor,
                provider_token="raw-max-token",
                public_base_url="https://client.example.test",
                sender=sender,
                credential_vault=self.vault,
            )

        self.assertEqual(result.connection.platform.value, "max")
        self.assertEqual(result.connection.status.value, "active")
        self.assertEqual(result.route.status, "active")
        self.assertEqual(
            result.webhook_url,
            f"https://client.example.test/clientplatform/webhooks/max/{result.route.id}",
        )
        self.assertEqual(sender.subscription[0], result.webhook_url)
        ciphertexts = [
            str(row[0])
            for row in self.conn.execute(
                "SELECT ciphertext FROM connection_credentials ORDER BY purpose"
            ).fetchall()
        ]
        self.assertTrue(ciphertexts)
        self.assertTrue(all("raw-max-token" not in item for item in ciphertexts))

    async def test_public_base_must_be_default_https_origin_before_provider_io(self) -> None:
        sender = _FakeMaxSender()
        for public_base_url in (
            "https://client.example.test:8443",
            "https://client.example.test/prefix",
        ):
            with self.subTest(public_base_url=public_base_url), self.assertRaisesRegex(
                ValueError,
                "HTTPS origin",
            ):
                await provision_max_channel(
                    actor=self.actor,
                    provider_token="raw-max-token",
                    public_base_url=public_base_url,
                    sender=sender,
                    credential_vault=self.vault,
                )

        self.assertIsNone(sender.subscription)
        stored = self.conn.execute(
            "SELECT COUNT(*) AS c FROM connection_credentials"
        ).fetchone()
        self.assertEqual(0, int(stored["c"]))

    async def test_max_provider_failure_disables_route_and_connection(self) -> None:
        with self._db_patch(), self.assertRaisesRegex(RuntimeError, "provider unavailable"):
            await provision_max_channel(
                actor=self.actor,
                provider_token="raw-max-token",
                public_base_url="https://client.example.test",
                sender=_FakeMaxSender(fail_subscription=True),
                credential_vault=self.vault,
            )

        connection = self.conn.execute(
            "SELECT status FROM connections WHERE business_id=? AND platform='max'",
            (self.actor.business_id,),
        ).fetchone()
        route = self.conn.execute(
            "SELECT status FROM messenger_ingress_routes WHERE business_id=? AND platform='max'",
            (self.actor.business_id,),
        ).fetchone()
        credentials = self.conn.execute(
            "SELECT status,ciphertext FROM connection_credentials "
            "WHERE business_id=? AND platform='max'",
            (self.actor.business_id,),
        ).fetchall()
        self.assertEqual(connection["status"], "disabled")
        self.assertEqual(route["status"], "disabled")
        self.assertTrue(credentials)
        self.assertTrue(
            all(
                row["status"] == "revoked" and row["ciphertext"] == "revoked"
                for row in credentials
            )
        )

    async def test_vk_verifies_group_and_configures_tenant_scoped_callback(self) -> None:
        sender = _FakeVkSender()
        with self._db_patch():
            result = await provision_vk_channel(
                actor=self.actor,
                group_id="238191212",
                provider_token="raw-vk-token",
                public_base_url="https://client.example.test",
                sender=sender,
                credential_vault=self.vault,
            )

        self.assertEqual(result.connection.platform.value, "vk")
        self.assertEqual(result.connection.external_account_id, "238191212")
        self.assertEqual(result.display_name, "Практика VK")
        self.assertEqual(sender.callback[0], "238191212")
        self.assertEqual(sender.callback[1], result.webhook_url)
        purposes = {
            row["purpose"]
            for row in self.conn.execute(
                "SELECT purpose FROM connection_credentials WHERE business_id=?",
                (self.actor.business_id,),
            ).fetchall()
        }
        self.assertEqual(
            purposes,
            {"provider_token", "webhook_secret", "confirmation_code"},
        )


if __name__ == "__main__":
    unittest.main()
