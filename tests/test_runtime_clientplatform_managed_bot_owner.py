from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from clientplatform.domain.managed_bot_owner import (
    ManagedBotWebhookMaterial,
    ManagedBotWebhookOperationFailed,
)
from clientplatform.runtime.managed_bot_owner import (
    TelegramManagedBotWebhookController,
)
from clientplatform.runtime.secrets import EnvironmentCredentialProvider


class _FakeSession:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


class _FakeBot:
    instances: list["_FakeBot"] = []
    identity = SimpleNamespace(id=700001, username="practice_helper_bot")
    set_result = True
    delete_result = True

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
        return self.set_result

    async def delete_webhook(self, *, drop_pending_updates: bool):
        self.delete_calls.append(drop_pending_updates)
        return self.delete_result


class ClientPlatformManagedBotOwnerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _FakeBot.instances.clear()
        _FakeBot.identity = SimpleNamespace(
            id=700001,
            username="practice_helper_bot",
        )
        _FakeBot.set_result = True
        _FakeBot.delete_result = True
        self.environment = {
            "CLIENTPLATFORM_SECRET_TELEGRAM_OWNER_LIFECYCLE": "telegram-token-value",
            "CLIENTPLATFORM_SECRET_WEBHOOK_OWNER_LIFECYCLE": "webhook-secret-value",
        }
        self.material = ManagedBotWebhookMaterial(
            managed_bot_id="00000000-0000-0000-0000-000000000203",
            business_id="00000000-0000-0000-0000-000000000201",
            connection_id="00000000-0000-0000-0000-000000000202",
            external_bot_id="700001",
            username="practice_helper_bot",
            credential_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_OWNER_LIFECYCLE"
            ),
            webhook_secret_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_WEBHOOK_OWNER_LIFECYCLE"
            ),
        )

    def _controller(self) -> TelegramManagedBotWebhookController:
        return TelegramManagedBotWebhookController(
            credential_provider=EnvironmentCredentialProvider(self.environment),
            public_base_url="https://cp.example.test/base/",
            gateway_path_prefix="/clientplatform/managed-bots",
        )

    async def test_attach_verifies_identity_and_sets_tokenless_route(self) -> None:
        with patch("clientplatform.runtime.managed_bot_owner.Bot", _FakeBot):
            await self._controller().attach(self.material)
        bot = _FakeBot.instances[-1]
        self.assertEqual(bot.token, "telegram-token-value")
        self.assertEqual(bot.session.closed, 1)
        self.assertEqual(len(bot.webhooks), 1)
        webhook = bot.webhooks[0]
        self.assertEqual(
            webhook["url"],
            "https://cp.example.test/clientplatform/managed-bots/telegram/700001",
        )
        self.assertEqual(webhook["secret_token"], "webhook-secret-value")
        self.assertNotIn("telegram-token-value", str(webhook["url"]))
        self.assertNotIn("webhook-secret-value", str(webhook["url"]))

    async def test_identity_mismatch_is_fail_closed(self) -> None:
        _FakeBot.identity = SimpleNamespace(
            id=700002,
            username="practice_helper_bot",
        )
        with patch("clientplatform.runtime.managed_bot_owner.Bot", _FakeBot):
            with self.assertRaises(ManagedBotWebhookOperationFailed):
                await self._controller().attach(self.material)
        bot = _FakeBot.instances[-1]
        self.assertEqual(bot.webhooks, [])
        self.assertEqual(bot.session.closed, 1)

    async def test_username_mismatch_is_fail_closed(self) -> None:
        _FakeBot.identity = SimpleNamespace(
            id=700001,
            username="another_helper_bot",
        )
        with patch("clientplatform.runtime.managed_bot_owner.Bot", _FakeBot):
            with self.assertRaises(ManagedBotWebhookOperationFailed):
                await self._controller().attach(self.material)
        self.assertEqual(_FakeBot.instances[-1].webhooks, [])

    async def test_detach_does_not_drop_pending_telegram_updates(self) -> None:
        with patch("clientplatform.runtime.managed_bot_owner.Bot", _FakeBot):
            await self._controller().detach(self.material)
        bot = _FakeBot.instances[-1]
        self.assertEqual(bot.delete_calls, [False])
        self.assertEqual(bot.session.closed, 1)

    async def test_missing_secret_fails_before_bot_creation(self) -> None:
        controller = TelegramManagedBotWebhookController(
            credential_provider=EnvironmentCredentialProvider({}),
            public_base_url="https://cp.example.test",
        )
        with patch("clientplatform.runtime.managed_bot_owner.Bot", _FakeBot):
            with self.assertRaises(ManagedBotWebhookOperationFailed):
                await controller.attach(self.material)
        self.assertEqual(_FakeBot.instances, [])

    async def test_telegram_rejection_is_reported_and_session_closed(self) -> None:
        _FakeBot.set_result = False
        with patch("clientplatform.runtime.managed_bot_owner.Bot", _FakeBot):
            with self.assertRaises(ManagedBotWebhookOperationFailed):
                await self._controller().attach(self.material)
        self.assertEqual(_FakeBot.instances[-1].session.closed, 1)


if __name__ == "__main__":
    unittest.main()
