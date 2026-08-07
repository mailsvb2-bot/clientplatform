from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from clientplatform.domain.bot_provisioning import (
    BotProvisioningStatus,
    BotProvisioningVerificationFailed,
    BotProvisioningWebhookConflict,
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
    delete_result = True
    webhook_url = ""

    def __init__(self, *, token: str) -> None:
        self.token = token
        self.session = _FakeSession()
        self.delete_calls: list[bool] = []
        self.instances.append(self)

    async def get_me(self):
        return self.identity

    async def get_webhook_info(self):
        return SimpleNamespace(url=self.webhook_url)

    async def delete_webhook(self, *, drop_pending_updates: bool):
        self.delete_calls.append(drop_pending_updates)
        return self.delete_result


class ClientPlatformBotProvisioningRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _FakeBot.instances.clear()
        _FakeBot.delete_result = True
        _FakeBot.webhook_url = ""
        _FakeBot.identity = SimpleNamespace(
            id=900001,
            username="practice_helper_bot",
            first_name="Помощник",
            last_name="Практики",
        )
        self.environment = {
            "CLIENTPLATFORM_SECRET_TELEGRAM_PRIMARY": "telegram-token-value",
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
                "secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_PRIMARY"
            ),
            external_bot_id=None,
            verified_username=None,
            connection_id=None,
            managed_bot_id=None,
            attempts=0,
            created_at="2026-07-29T09:00:00+00:00",
            updated_at="2026-07-29T09:00:00+00:00",
        )

    def _provisioner(
        self,
        *,
        reject_active_webhook: bool = False,
    ) -> BotFatherTelegramProvisioner:
        return BotFatherTelegramProvisioner(
            credential_provider=EnvironmentCredentialProvider(self.environment),
            public_base_url="http://ignored.invalid",
            gateway_path_prefix="/ignored",
            reject_active_webhook=reject_active_webhook,
        )

    async def test_verifies_identity_and_removes_webhook_for_polling(self) -> None:
        with patch("clientplatform.runtime.bot_provisioning.Bot", _FakeBot):
            verified = await self._provisioner().provision(self.request)
        self.assertEqual(verified.external_bot_id, "900001")
        self.assertEqual(verified.username, "practice_helper_bot")
        bot = _FakeBot.instances[-1]
        self.assertEqual(bot.token, "telegram-token-value")
        self.assertEqual(bot.delete_calls, [False])
        self.assertEqual(bot.session.closed, 1)
        self.assertFalse(hasattr(bot, "set_webhook"))

    async def test_existing_webhook_can_be_rejected_without_removal(self) -> None:
        _FakeBot.webhook_url = "https://other-service.example/webhook"
        with patch("clientplatform.runtime.bot_provisioning.Bot", _FakeBot):
            with self.assertRaises(BotProvisioningWebhookConflict):
                await self._provisioner(reject_active_webhook=True).provision(
                    self.request
                )
        bot = _FakeBot.instances[-1]
        self.assertEqual(bot.delete_calls, [])
        self.assertEqual(bot.session.closed, 1)

    async def test_empty_webhook_allows_safe_existing_bot_import(self) -> None:
        with patch("clientplatform.runtime.bot_provisioning.Bot", _FakeBot):
            verified = await self._provisioner(
                reject_active_webhook=True
            ).provision(self.request)
        self.assertEqual(verified.external_bot_id, "900001")
        self.assertEqual(_FakeBot.instances[-1].delete_calls, [False])

    async def test_rollback_keeps_webhook_disabled_without_dropping_updates(self) -> None:
        with patch("clientplatform.runtime.bot_provisioning.Bot", _FakeBot):
            await self._provisioner().rollback(self.request)
        bot = _FakeBot.instances[-1]
        self.assertEqual(bot.delete_calls, [False])
        self.assertEqual(bot.session.closed, 1)

    async def test_missing_secret_reference_fails_without_creating_bot(self) -> None:
        provisioner = BotFatherTelegramProvisioner(
            credential_provider=EnvironmentCredentialProvider({}),
        )
        with patch("clientplatform.runtime.bot_provisioning.Bot", _FakeBot):
            with self.assertRaises(BotProvisioningVerificationFailed):
                await provisioner.provision(self.request)
        self.assertEqual(_FakeBot.instances, [])

    async def test_delete_webhook_rejection_is_fail_closed(self) -> None:
        _FakeBot.delete_result = False
        with patch("clientplatform.runtime.bot_provisioning.Bot", _FakeBot):
            with self.assertRaises(BotProvisioningVerificationFailed):
                await self._provisioner().provision(self.request)
        self.assertEqual(_FakeBot.instances[-1].session.closed, 1)

    def test_public_url_is_not_required_for_polling(self) -> None:
        BotFatherTelegramProvisioner(
            credential_provider=EnvironmentCredentialProvider(self.environment),
            public_base_url="",
        )


if __name__ == "__main__":
    unittest.main()
