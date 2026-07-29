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
    delete_result = True

    def __init__(self, *, token: str) -> None:
        self.token = token
        self.session = _FakeSession()
        self.delete_calls: list[bool] = []
        self.instances.append(self)

    async def get_me(self):
        return self.identity

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
        _FakeBot.delete_result = True
        self.environment = {
            "CLIENTPLATFORM_SECRET_TELEGRAM_OWNER_LIFECYCLE": "telegram-token-value",
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
                "secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_OWNER_LIFECYCLE"
            ),
        )

    def _controller(self) -> TelegramManagedBotWebhookController:
        return TelegramManagedBotWebhookController(
            credential_provider=EnvironmentCredentialProvider(self.environment),
            public_base_url="http://ignored.invalid",
        )

    async def test_attach_verifies_identity_and_removes_webhook_for_polling(self) -> None:
        with patch("clientplatform.runtime.managed_bot_owner.Bot", _FakeBot):
            await self._controller().attach(self.material)
        bot = _FakeBot.instances[-1]
        self.assertEqual(bot.token, "telegram-token-value")
        self.assertEqual(bot.delete_calls, [False])
        self.assertEqual(bot.session.closed, 1)
        self.assertFalse(hasattr(bot, "set_webhook"))

    async def test_identity_mismatch_is_fail_closed(self) -> None:
        _FakeBot.identity = SimpleNamespace(
            id=700002,
            username="practice_helper_bot",
        )
        with patch("clientplatform.runtime.managed_bot_owner.Bot", _FakeBot):
            with self.assertRaises(ManagedBotWebhookOperationFailed):
                await self._controller().attach(self.material)
        self.assertEqual(_FakeBot.instances[-1].delete_calls, [])
        self.assertEqual(_FakeBot.instances[-1].session.closed, 1)

    async def test_username_mismatch_is_fail_closed(self) -> None:
        _FakeBot.identity = SimpleNamespace(
            id=700001,
            username="another_helper_bot",
        )
        with patch("clientplatform.runtime.managed_bot_owner.Bot", _FakeBot):
            with self.assertRaises(ManagedBotWebhookOperationFailed):
                await self._controller().attach(self.material)
        self.assertEqual(_FakeBot.instances[-1].delete_calls, [])

    async def test_detach_keeps_webhook_disabled(self) -> None:
        with patch("clientplatform.runtime.managed_bot_owner.Bot", _FakeBot):
            await self._controller().detach(self.material)
        bot = _FakeBot.instances[-1]
        self.assertEqual(bot.delete_calls, [False])
        self.assertEqual(bot.session.closed, 1)

    async def test_missing_secret_fails_before_bot_creation(self) -> None:
        controller = TelegramManagedBotWebhookController(
            credential_provider=EnvironmentCredentialProvider({}),
        )
        with patch("clientplatform.runtime.managed_bot_owner.Bot", _FakeBot):
            with self.assertRaises(ManagedBotWebhookOperationFailed):
                await controller.attach(self.material)
        self.assertEqual(_FakeBot.instances, [])

    async def test_delete_rejection_is_reported_and_session_closed(self) -> None:
        _FakeBot.delete_result = False
        with patch("clientplatform.runtime.managed_bot_owner.Bot", _FakeBot):
            with self.assertRaises(ManagedBotWebhookOperationFailed):
                await self._controller().attach(self.material)
        self.assertEqual(_FakeBot.instances[-1].session.closed, 1)


if __name__ == "__main__":
    unittest.main()
