from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import InlineKeyboardMarkup

from clientplatform.domain.bot_provisioning import BotProvisioningStatus
from clientplatform.domain.connections import (
    ConnectionNotFound,
    ConnectionStatus,
    ManagedBotStatus,
)
from clientplatform.domain.managed_bot_owner import (
    ManagedBotOwnerLifecycleResult,
    ManagedBotOwnerSnapshot,
)
from handlers import clientplatform_bot_lifecycle as lifecycle

_BUSINESS_ID = "00000000-0000-0000-0000-000000000201"
_BOT_ID = "00000000-0000-0000-0000-000000000203"
_CONNECTION_ID = "00000000-0000-0000-0000-000000000202"


def _snapshot(status: ManagedBotStatus) -> ManagedBotOwnerSnapshot:
    connection_status = (
        ConnectionStatus.ACTIVE
        if status == ManagedBotStatus.ACTIVE
        else ConnectionStatus.DISABLED
        if status == ManagedBotStatus.DISABLED
        else ConnectionStatus.REVOKED
    )
    return ManagedBotOwnerSnapshot(
        managed_bot_id=_BOT_ID,
        business_id=_BUSINESS_ID,
        connection_id=_CONNECTION_ID,
        external_bot_id="700001",
        username="practice_helper_bot",
        display_name="Помощник практики",
        bot_status=status,
        connection_status=connection_status,
        pending_events=1,
        processing_events=2,
        retry_events=3,
        processed_events=4,
        dead_events=5,
        bot_updated_at="2026-07-29T10:00:00+00:00",
        connection_updated_at="2026-07-29T10:00:00+00:00",
        last_processed_at="2026-07-29T09:59:00+00:00",
        last_dead_at="2026-07-29T09:58:00+00:00",
    )


class _FakeState:
    def __init__(self) -> None:
        self.cleared = 0

    async def clear(self) -> None:
        self.cleared += 1


class _FakeMessage:
    def __init__(self) -> None:
        self.answers: list[tuple[str, object | None]] = []

    async def answer(self, text: str, reply_markup: object | None = None) -> None:
        self.answers.append((text, reply_markup))


class _FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=101)
        self.message = _FakeMessage()
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(
        self,
        text: str | None = None,
        *,
        show_alert: bool = False,
    ) -> None:
        self.answers.append((text, show_alert))


class ClientPlatformManagedBotLifecycleUiTests(unittest.IsolatedAsyncioTestCase):
    def test_snapshot_text_contains_no_secret_material(self) -> None:
        text = lifecycle._snapshot_text(_snapshot(ManagedBotStatus.ACTIVE))
        self.assertIn("@practice_helper_bot", text)
        self.assertIn("Очередь этого бота", text)
        self.assertIn("Состояние подключения: активно", text)
        self.assertIn("ожидают: 1", text)
        self.assertIn("обрабатываются: 2", text)
        self.assertNotIn("secret://", text)
        self.assertNotIn("CLIENTPLATFORM_SECRET", text)

    def test_keyboard_actions_follow_lifecycle_status_and_fit_limit(self) -> None:
        expected = {
            ManagedBotStatus.ACTIVE: {"cpbl:dc:", "cpbl:rc:"},
            ManagedBotStatus.DISABLED: {"cpbl:ax:", "cpbl:rc:"},
            ManagedBotStatus.REVOKED: set(),
        }
        for status, action_prefixes in expected.items():
            markup = lifecycle._snapshot_keyboard(_snapshot(status))
            callbacks = [
                str(button.callback_data)
                for row in markup.inline_keyboard
                for button in row
            ]
            for callback in callbacks:
                self.assertLessEqual(len(callback.encode("utf-8")), 64)
            lifecycle_actions = {
                prefix
                for prefix in ("cpbl:dc:", "cpbl:ax:", "cpbl:rc:")
                if any(callback.startswith(prefix) for callback in callbacks)
            }
            self.assertEqual(lifecycle_actions, action_prefixes)

    def test_lifecycle_entry_is_installed_once_for_completed_bot(self) -> None:
        def original(_business_id: str, _request: object) -> InlineKeyboardMarkup:
            return lifecycle.control._keyboard(
                [[("Обновить", "cpb:o:existing")]]
            )

        module = SimpleNamespace(_status_keyboard=original)
        lifecycle.install_lifecycle_controls(module)
        lifecycle.install_lifecycle_controls(module)
        request = SimpleNamespace(
            status=BotProvisioningStatus.COMPLETED,
            managed_bot_id=_BOT_ID,
        )
        markup = module._status_keyboard(_BUSINESS_ID, request)
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertEqual(labels.count("Управление и состояние"), 1)

    def test_lazy_router_composition_contains_lifecycle_once(self) -> None:
        import handlers

        handlers._load_clientplatform_modules()
        handlers._load_clientplatform_modules()
        setup = handlers.clientplatform_bot_setup
        names = [item.name for item in setup.router.sub_routers]
        self.assertEqual(names.count("clientplatform_bot_lifecycle"), 1)

    async def test_stale_snapshot_callback_returns_generic_unavailable_message(self) -> None:
        message = _FakeMessage()
        with patch.object(
            lifecycle,
            "_send_snapshot",
            new=AsyncMock(side_effect=ConnectionNotFound("foreign route")),
        ):
            sent = await lifecycle._safe_send_snapshot(
                message,
                user_id=101,
                business_id=_BUSINESS_ID,
                managed_bot_id=_BOT_ID,
            )
        self.assertFalse(sent)
        self.assertIn("больше недоступно", message.answers[-1][0])
        self.assertNotIn("foreign route", message.answers[-1][0])

    async def test_revoke_requires_separate_confirmation_callback(self) -> None:
        business_token, bot_token = lifecycle._tokens(_BUSINESS_ID, _BOT_ID)
        callback = _FakeCallback(f"cpbl:rc:{business_token}:{bot_token}")
        state = _FakeState()
        with (
            patch.object(
                lifecycle.control,
                "_callback_message",
                return_value=callback.message,
            ),
            patch.object(
                lifecycle,
                "revoke_managed_bot_for_owner",
                new=AsyncMock(),
            ) as revoke,
        ):
            await lifecycle.confirm_revoke(callback, state)
        revoke.assert_not_awaited()
        self.assertEqual(state.cleared, 1)
        text, markup = callback.message.answers[-1]
        self.assertIn("необратимо", text)
        confirm_callbacks = [
            str(button.callback_data)
            for row in markup.inline_keyboard
            for button in row
            if str(button.callback_data).startswith("cpbl:rx:")
        ]
        self.assertEqual(len(confirm_callbacks), 1)

    async def test_confirmed_revoke_calls_service_and_refreshes_safe_snapshot(self) -> None:
        business_token, bot_token = lifecycle._tokens(_BUSINESS_ID, _BOT_ID)
        callback = _FakeCallback(f"cpbl:rx:{business_token}:{bot_token}")
        state = _FakeState()
        result = ManagedBotOwnerLifecycleResult(
            snapshot=_snapshot(ManagedBotStatus.REVOKED),
            webhook_synchronized=True,
        )
        with (
            patch.object(
                lifecycle,
                "_actor",
                new=AsyncMock(return_value=SimpleNamespace()),
            ),
            patch.object(
                lifecycle.control,
                "_callback_message",
                return_value=callback.message,
            ),
            patch.object(
                lifecycle,
                "revoke_managed_bot_for_owner",
                new=AsyncMock(return_value=result),
            ) as revoke,
            patch.object(
                lifecycle,
                "_safe_send_snapshot",
                new=AsyncMock(return_value=True),
            ) as refresh,
        ):
            await lifecycle.execute_revoke(callback, state)
        revoke.assert_awaited_once()
        refresh.assert_awaited_once()
        self.assertEqual(state.cleared, 1)
        self.assertIn("отозвано навсегда", callback.message.answers[0][0])

    async def test_disable_warning_does_not_claim_webhook_was_removed(self) -> None:
        business_token, bot_token = lifecycle._tokens(_BUSINESS_ID, _BOT_ID)
        callback = _FakeCallback(f"cpbl:dx:{business_token}:{bot_token}")
        state = _FakeState()
        result = ManagedBotOwnerLifecycleResult(
            snapshot=_snapshot(ManagedBotStatus.DISABLED),
            webhook_synchronized=False,
            warning_code="webhook_detach_failed",
        )
        with (
            patch.object(
                lifecycle,
                "_actor",
                new=AsyncMock(return_value=SimpleNamespace()),
            ),
            patch.object(
                lifecycle.control,
                "_callback_message",
                return_value=callback.message,
            ),
            patch.object(
                lifecycle,
                "disable_managed_bot_for_owner",
                new=AsyncMock(return_value=result),
            ),
            patch.object(
                lifecycle,
                "_safe_send_snapshot",
                new=AsyncMock(return_value=True),
            ),
        ):
            await lifecycle.execute_disable(callback, state)
        combined = " ".join(text for text, _ in callback.message.answers)
        self.assertIn("Telegram не подтвердил удаление webhook", combined)
        self.assertNotIn("webhook удалён", combined.lower())


if __name__ == "__main__":
    unittest.main()
