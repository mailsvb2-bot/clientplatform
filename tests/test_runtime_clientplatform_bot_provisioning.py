from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from clientplatform.domain.bot_provisioning import (
    BotProvisioningStatus,
    BotProvisioningVerificationFailed,
    ManagedBotProvisioningRequest,
)
from clientplatform.runtime.bot_provisioning import BotFatherTelegramProvisioner
from clientplatform.runtime.secrets import EnvironmentCredentialProvider


class _FakeSession:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


class _FakeBot:
    instances: list["_FakeBot"] = []
    identity = SimpleNamespace(
        id=900001,
        username="practice_helper_bot",
        first_name="Помощник",
        last_name="Практики",
    )
    set_webhook_result = True

    def __init__(self, *, token: str) -> None:
        self.token = token
        self.session = _FakeSession()
        self.webhooks: list[dict[str, object]] = []
        self.delete_calls: list[bool] = []
        self.instances.append(self)

    async def get_me(self):
        return self.identity

    async def set_webhook(self, **kwargs):
        self.webhooks.append(dict(kwargs))
        return self.set_webhook_result

    async def delete_webhook(self, *, drop_pending_updates: bool):
        self.delete_calls.append(drop_pending_updates)
        return True


class ClientPlatformBotProvisioningRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _FakeBot.instances.clear()
        _FakeBot.set_webhook_result = True
        _FakeBot.identity = SimpleNamespace(
            id=900001,
            username="practice_helper_bot",
            first_name="Помощник",
            last_name="Практики",
        )
        self.environment = {
            "CLIENTPLATFORM_SECRET_TELEGRAM_PRIMARY": "telegram-token-value",
            "CLIENTPLATFORM_SECRET_WEBHOOK_PRIMARY": "webhook-secret-value",
        }
        self.request = ManagedBotProvisioningRequest(
            id="00000000-0000-0000-0000-000000000101",
            business_id="00000000-0000-0000-0000-000000000102",
            created_by_member_id="00000000-0000-0000-0000-000000000103",
            provider="botfather",
            status=BotProvisioningStatus.READY,
            idempotency_key="connect-primary-bot",
            requested_username="practice_helper_bot",
            display_name="Помощник практики",
            credential_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_PRIMARY"
            ),
            webhook_secret_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_WEBHOOK_PRIMARY"
            ),
            external_bot_id=None,
            verified_username=None,
            connection_id=None,
            managed_bot_id=None,
            attempts=0,
            created_at="2026-07-29T09:00:00+00:00",
            updated_at="2026-07-29T09:00:00+00:00",
        )

    def _provisioner(self) -> BotFatherTelegramProvisioner:
        return BotFatherTelegramProvisioner(
            credential_provider=EnvironmentCredentialProvider(self.environment),
            public_base_url="https://cp.example.test/base/",
            gateway_path_prefix="/clientplatform/managed-bots",
        )

    async def test_verifies_identity_and_configures_tokenless_route(self) -> None:
        with patch("clientplatform.runtime.bot_provisioning.Bot", _FakeBot):
            verified = await self._provisioner().provision(self.request)
        self.assertEqual(verified.external_bot_id, "900001")
        self.assertEqual(verified.username, "practice_helper_bot")
        bot = _FakeBot.instances[-1]
        self.assertEqual(bot.token, "telegram-token-value")
        self.assertEqual(bot.session.closed, 1)
        self.assertEqual(len(bot.webhooks), 1)
        webhook = bot.webhooks[0]
        self.assertEqual(
            webhook["url"],
            "https://cp.example.test/clientplatform/managed-bots/telegram/900001",
        )
        self.assertEqual(webhook["secret_token"], "webhook-secret-value")
        self.assertNotIn("telegram-token-value", str(webhook["url"]))
        self.assertNotIn("webhook-secret-value", str(webhook["url"]))

    async def test_rollback_deletes_webhook_without_dropping_updates(self) -> None:
        with patch("clientplatform.runtime.bot_provisioning.Bot", _FakeBot):
            await self._provisioner().rollback(self.request)
        bot = _FakeBot.instances[-1]
        self.assertEqual(bot.delete_calls, [False])
        self.assertEqual(bot.session.closed, 1)

    async def test_missing_secret_reference_fails_without_creating_bot(self) -> None:
        provisioner = BotFatherTelegramProvisioner(
            credential_provider=EnvironmentCredentialProvider({}),
            public_base_url="https://cp.example.test",
        )
        with patch("clientplatform.runtime.bot_provisioning.Bot", _FakeBot):
            with self.assertRaises(BotProvisioningVerificationFailed):
                await provisioner.provision(self.request)
        self.assertEqual(_FakeBot.instances, [])

    async def test_webhook_rejection_is_fail_closed_and_session_is_closed(self) -> None:
        _FakeBot.set_webhook_result = False
        with patch("clientplatform.runtime.bot_provisioning.Bot", _FakeBot):
            with self.assertRaises(BotProvisioningVerificationFailed):
                await self._provisioner().provision(self.request)
        self.assertEqual(_FakeBot.instances[-1].session.closed, 1)

    def test_requires_https_public_base_url(self) -> None:
        with self.assertRaises(BotProvisioningVerificationFailed):
            BotFatherTelegramProvisioner(
                credential_provider=EnvironmentCredentialProvider(self.environment),
                public_base_url="http://cp.example.test",
            )


if __name__ == "__main__":
    unittest.main()
