from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import ReplyKeyboardMarkup, ReplyKeyboardRemove

from clientplatform.domain.bot_provisioning import (
    BotProvisioningProvider,
    BotProvisioningStatus,
    ManagedBotProvisioningRequest,
)
from handlers import clientplatform_managed_bot_onboarding as managed


_BUSINESS_ID = "00000000-0000-0000-0000-000000000101"
_MEMBER_ID = "00000000-0000-0000-0000-000000000102"
_REQUEST_ID = "00000000-0000-0000-0000-000000000103"
_CONNECTION_ID = "00000000-0000-0000-0000-000000000104"
_MANAGED_BOT_ID = "00000000-0000-0000-0000-000000000105"
_AUTO_ENV = {"CLIENTPLATFORM_MANAGED_BOT_AUTO_PROVISIONING_ENABLED": "1"}
_EVENT_AT = datetime(2026, 8, 7, 12, 5, tzinfo=timezone.utc)


def _request(status: BotProvisioningStatus) -> ManagedBotProvisioningRequest:
    has_reference = status not in {
        BotProvisioningStatus.AWAITING_SECRET,
        BotProvisioningStatus.CANCELLED,
    }
    completed = status == BotProvisioningStatus.COMPLETED
    reference = (
        f"vault://managed-bot/{_BUSINESS_ID}/00000000-0000-0000-0000-000000000106"
        if has_reference
        else None
    )
    return ManagedBotProvisioningRequest(
        id=_REQUEST_ID,
        business_id=_BUSINESS_ID,
        created_by_member_id=_MEMBER_ID,
        provider=BotProvisioningProvider.TELEGRAM_MANAGED,
        status=status,
        idempotency_key="managed-owner-ui-regression",
        requested_username=None,
        display_name="Практика",
        credential_reference=reference,
        webhook_secret_reference=reference,
        external_bot_id="900001" if completed else None,
        verified_username="practice_helper_bot" if completed else None,
        connection_id=_CONNECTION_ID if completed else None,
        managed_bot_id=_MANAGED_BOT_ID if completed else None,
        attempts=1 if has_reference else 0,
        created_at="2026-08-07T12:00:00+00:00",
        updated_at="2026-08-07T12:01:00+00:00",
        completed_at="2026-08-07T12:02:00+00:00" if completed else None,
        last_error_code=(
            "telegram_verification_failed"
            if status == BotProvisioningStatus.FAILED
            else None
        ),
    )


class _State:
    def __init__(self) -> None:
        self.cleared = 0

    async def clear(self) -> None:
        self.cleared += 1


class _Message:
    def __init__(self) -> None:
        self.answers: list[tuple[str, object | None]] = []
        self.from_user = SimpleNamespace(id=101)
        self.managed_bot_created = None
        self.bot = None
        self.date = _EVENT_AT

    async def answer(self, text: str, reply_markup=None) -> None:
        self.answers.append((text, reply_markup))


class _ManagerBot:
    def __init__(self, *, can_manage_bots: bool = True) -> None:
        self.can_manage_bots = can_manage_bots
        self.get_managed_bot_token = AsyncMock(return_value="900001:" + ("A" * 40))

    async def get_me(self):
        return SimpleNamespace(can_manage_bots=self.can_manage_bots)


class _Callback:
    def __init__(self, *, can_manage_bots: bool = True) -> None:
        self.data = f"cpm:n:{managed._business_token(_BUSINESS_ID)}"
        self.from_user = SimpleNamespace(id=101)
        self.bot = _ManagerBot(can_manage_bots=can_manage_bots)
        self.message = _Message()
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, *, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


class ClientPlatformManagedBotOnboardingUiTests(unittest.IsolatedAsyncioTestCase):
    def test_managed_status_never_asks_owner_for_token(self) -> None:
        with patch.dict("os.environ", _AUTO_ENV, clear=False):
            texts = [managed._managed_status_text(None)]
            texts.extend(
                managed._managed_status_text(_request(status))
                for status in BotProvisioningStatus
            )
        rendered = "\n".join(texts).lower()
        self.assertNotIn("clientplatform_secret_", rendered)
        self.assertNotIn("secret-store", rendered)
        self.assertNotIn("скопируйте токен", rendered)
        self.assertIn("токен", rendered)
        self.assertIn("не нужно", rendered)

    def test_disabled_runtime_hides_native_creation_button(self) -> None:
        with patch.dict(
            "os.environ",
            {"CLIENTPLATFORM_MANAGED_BOT_AUTO_PROVISIONING_ENABLED": "0"},
            clear=False,
        ):
            markup = managed._managed_status_keyboard(_BUSINESS_ID, None)
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertNotIn("✨ Создать моего бота", labels)
        self.assertIn("Подключить существующего бота", labels)

    async def test_manager_capability_opens_native_managed_bot_request(self) -> None:
        callback = _Callback(can_manage_bots=True)
        state = _State()
        with (
            patch.dict("os.environ", _AUTO_ENV, clear=False),
            patch.object(managed.control, "_actor", new=AsyncMock(return_value=object())),
            patch.object(managed, "_business_name", new=AsyncMock(return_value="Практика")),
            patch.object(
                managed,
                "begin_telegram_managed_bot_onboarding",
                return_value=_request(BotProvisioningStatus.AWAITING_SECRET),
            ) as begin,
            patch.object(
                managed.control,
                "_callback_message",
                return_value=callback.message,
            ),
        ):
            await managed.request_managed_bot_creation(callback, state)

        begin.assert_called_once()
        self.assertEqual(state.cleared, 1)
        markup = callback.message.answers[-1][1]
        self.assertIsInstance(markup, ReplyKeyboardMarkup)
        button = markup.keyboard[0][0]
        self.assertIsNotNone(button.request_managed_bot)
        self.assertEqual(button.request_managed_bot.suggested_name, "Практика")
        self.assertIn("Никаких токенов", callback.message.answers[-1][0])

    async def test_missing_manager_capability_fails_before_creating_request(self) -> None:
        callback = _Callback(can_manage_bots=False)
        state = _State()
        with (
            patch.dict("os.environ", _AUTO_ENV, clear=False),
            patch.object(managed.control, "_actor", new=AsyncMock(return_value=object())),
            patch.object(
                managed,
                "begin_telegram_managed_bot_onboarding",
            ) as begin,
        ):
            await managed.request_managed_bot_creation(callback, state)
        begin.assert_not_called()
        self.assertEqual(state.cleared, 0)
        self.assertTrue(callback.answers[-1][1])

    async def test_created_bot_token_is_consumed_in_memory_and_never_rendered(self) -> None:
        raw_token = "900001:" + ("A" * 40)
        manager_bot = _ManagerBot()
        manager_bot.get_managed_bot_token = AsyncMock(return_value=raw_token)
        child = SimpleNamespace(
            id=900001,
            username="practice_helper_bot",
            first_name="Помощник",
            last_name="Практики",
        )
        message = _Message()
        message.bot = manager_bot
        message.managed_bot_created = SimpleNamespace(bot=child)
        state = _State()
        completed = _request(BotProvisioningStatus.COMPLETED)
        with (
            patch.object(managed.control, "_user_id", return_value=101),
            patch.object(
                managed,
                "complete_telegram_managed_bot_onboarding",
                new=AsyncMock(return_value=completed),
            ) as complete,
        ):
            await managed.receive_managed_bot_created(message, state)

        manager_bot.get_managed_bot_token.assert_awaited_once_with(user_id=900001)
        self.assertEqual(complete.await_args.kwargs["token"], raw_token)
        self.assertEqual(complete.await_args.kwargs["event_at"], _EVENT_AT)
        self.assertEqual(state.cleared, 1)
        rendered = " ".join(text for text, _ in message.answers)
        self.assertNotIn(raw_token, rendered)
        self.assertIn("подключён", rendered)
        self.assertIsInstance(message.answers[0][1], ReplyKeyboardRemove)


if __name__ == "__main__":
    unittest.main()
