from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from clientplatform.domain.bot_provisioning import (
    BotProvisioningProvider,
    BotProvisioningStatus,
    BotProvisioningWebhookConflict,
    ManagedBotProvisioningRequest,
)
from handlers import clientplatform_existing_bot_onboarding as existing


_BUSINESS_ID = "00000000-0000-0000-0000-000000000201"
_MEMBER_ID = "00000000-0000-0000-0000-000000000202"
_REQUEST_ID = "00000000-0000-0000-0000-000000000203"
_CONNECTION_ID = "00000000-0000-0000-0000-000000000204"
_MANAGED_BOT_ID = "00000000-0000-0000-0000-000000000205"


def _request(status: BotProvisioningStatus) -> ManagedBotProvisioningRequest:
    completed = status == BotProvisioningStatus.COMPLETED
    reference = (
        "vault://managed-bot/00000000-0000-0000-0000-000000000201/"
        "00000000-0000-0000-0000-000000000206"
    )
    return ManagedBotProvisioningRequest(
        id=_REQUEST_ID,
        business_id=_BUSINESS_ID,
        created_by_member_id=_MEMBER_ID,
        provider=BotProvisioningProvider.BOTFATHER,
        status=status,
        idempotency_key="existing-owner-ui-regression",
        requested_username=None,
        display_name=None,
        credential_reference=(
            reference if status != BotProvisioningStatus.AWAITING_SECRET else None
        ),
        webhook_secret_reference=(
            reference if status != BotProvisioningStatus.AWAITING_SECRET else None
        ),
        external_bot_id="900001" if completed else None,
        verified_username="existing_practice_bot" if completed else None,
        connection_id=_CONNECTION_ID if completed else None,
        managed_bot_id=_MANAGED_BOT_ID if completed else None,
        attempts=1 if status != BotProvisioningStatus.AWAITING_SECRET else 0,
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
        self.data = {
            "business_id": _BUSINESS_ID,
            "idempotency_key": "existing-owner-ui-test",
        }
        self.cleared = 0

    async def get_data(self):
        return dict(self.data)

    async def clear(self) -> None:
        self.cleared += 1


class _Message:
    def __init__(self, token: str) -> None:
        self.text = token
        self.from_user = SimpleNamespace(id=101)
        self.answers: list[tuple[str, object | None]] = []
        self.deleted = 0

    async def delete(self) -> None:
        self.deleted += 1

    async def answer(self, text: str, reply_markup=None) -> None:
        self.answers.append((text, reply_markup))


class ClientPlatformExistingBotOnboardingUiTests(unittest.IsolatedAsyncioTestCase):
    def test_primary_existing_bot_copy_contains_no_operator_vocabulary(self) -> None:
        texts = [
            existing._existing_status_text(
                _request(BotProvisioningStatus.AWAITING_SECRET)
            ),
            existing._existing_status_text(_request(BotProvisioningStatus.FAILED)),
            existing._existing_status_text(_request(BotProvisioningStatus.COMPLETED)),
        ]
        rendered = "\n".join(texts).lower()
        self.assertNotIn("clientplatform_secret_", rendered)
        self.assertNotIn("secret-store", rendered)
        self.assertNotIn("credential_reference", rendered)
        self.assertNotIn("polling", rendered)
        self.assertIn("токен", rendered)

    def test_existing_bot_button_uses_simple_one_step_route(self) -> None:
        markup = existing._existing_status_keyboard(_BUSINESS_ID, None)
        buttons = [button for row in markup.inline_keyboard for button in row]
        existing_button = next(
            button for button in buttons if "уже есть бот" in button.text.lower()
        )
        self.assertTrue(str(existing_button.callback_data).startswith("cpe:n:"))

    async def test_token_message_is_deleted_and_never_echoed(self) -> None:
        raw_token = "900001:" + ("A" * 40)
        message = _Message(raw_token)
        state = _State()
        completed = SimpleNamespace(
            business_id=_BUSINESS_ID,
            verified_username="existing_practice_bot",
        )
        events: list[str] = []

        async def delete_first(_message):
            events.append("delete")
            await _message.delete()
            return True

        async def connect(**kwargs):
            events.append("connect")
            self.assertEqual(kwargs["token"], raw_token)
            return completed

        with (
            patch.object(existing, "_delete_token_message", side_effect=delete_first),
            patch.object(existing.control, "_user_id", return_value=101),
            patch.object(
                existing.control,
                "_actor",
                new=AsyncMock(return_value=SimpleNamespace()),
            ),
            patch.object(existing, "connect_existing_telegram_bot", side_effect=connect),
        ):
            await existing.receive_existing_bot_token(message, state)

        self.assertEqual(events, ["delete", "connect"])
        self.assertEqual(message.deleted, 1)
        self.assertEqual(state.cleared, 1)
        rendered = " ".join(text for text, _markup in message.answers)
        self.assertNotIn(raw_token, rendered)
        self.assertIn("подключён", rendered)

    async def test_failure_is_sanitized_and_allows_retry(self) -> None:
        raw_token = "900001:" + ("B" * 40)
        message = _Message(raw_token)
        state = _State()
        with (
            patch.object(existing.control, "_user_id", return_value=101),
            patch.object(
                existing.control,
                "_actor",
                new=AsyncMock(return_value=SimpleNamespace()),
            ),
            patch.object(
                existing,
                "connect_existing_telegram_bot",
                new=AsyncMock(side_effect=ValueError("secret raw token must never leak")),
            ),
        ):
            await existing.receive_existing_bot_token(message, state)

        self.assertEqual(message.deleted, 1)
        self.assertEqual(state.cleared, 0)
        rendered = " ".join(text for text, _markup in message.answers)
        self.assertNotIn(raw_token, rendered)
        self.assertNotIn("secret raw token", rendered)
        self.assertIn("пришлите его ещё раз", rendered.lower())

    async def test_delete_failure_never_persists_or_verifies_token(self) -> None:
        raw_token = "900001:" + ("C" * 40)
        message = _Message(raw_token)
        state = _State()
        connector = AsyncMock()
        with (
            patch.object(
                existing,
                "_delete_token_message",
                new=AsyncMock(return_value=False),
            ),
            patch.object(existing, "connect_existing_telegram_bot", connector),
        ):
            await existing.receive_existing_bot_token(message, state)

        connector.assert_not_awaited()
        self.assertEqual(state.cleared, 0)
        rendered = " ".join(text for text, _markup in message.answers)
        self.assertNotIn(raw_token, rendered)
        self.assertIn("не стал использовать или сохранять", rendered)
        self.assertIn("обновите токен", rendered.lower())

    async def test_existing_webhook_is_explained_without_takeover_language(self) -> None:
        raw_token = "900001:" + ("D" * 40)
        message = _Message(raw_token)
        state = _State()
        with (
            patch.object(existing.control, "_user_id", return_value=101),
            patch.object(
                existing.control,
                "_actor",
                new=AsyncMock(return_value=SimpleNamespace()),
            ),
            patch.object(
                existing,
                "connect_existing_telegram_bot",
                new=AsyncMock(
                    side_effect=BotProvisioningWebhookConflict(
                        "hidden provider details"
                    )
                ),
            ),
        ):
            await existing.receive_existing_bot_token(message, state)

        self.assertEqual(message.deleted, 1)
        self.assertEqual(state.cleared, 0)
        rendered = " ".join(text for text, _markup in message.answers)
        self.assertNotIn(raw_token, rendered)
        self.assertNotIn("hidden provider details", rendered)
        self.assertIn("уже подключён к другому сервису", rendered.lower())
        self.assertIn("ничего не переключал", rendered.lower())


if __name__ == "__main__":
    unittest.main()
