from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from clientplatform.domain.bot_provisioning import (
    BotProvisioningInvariantViolation,
    BotProvisioningStatus,
    BotProvisioningVerificationFailed,
    ManagedBotProvisioningRequest,
)
from handlers import clientplatform_bot_setup as setup


_BUSINESS_ID = "00000000-0000-0000-0000-000000000101"
_REQUEST_ID = "00000000-0000-0000-0000-000000000102"
_MEMBER_ID = "00000000-0000-0000-0000-000000000103"
_CONNECTION_ID = "00000000-0000-0000-0000-000000000104"
_MANAGED_BOT_ID = "00000000-0000-0000-0000-000000000105"
_TOKEN_REF = "secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_PRACTICE"
_WEBHOOK_REF = "secret://env/CLIENTPLATFORM_SECRET_WEBHOOK_PRACTICE"


def _raw_token_fixture() -> str:
    return "123456789:" + ("A" * 40)


def _request(status: BotProvisioningStatus) -> ManagedBotProvisioningRequest:
    has_references = status not in {
        BotProvisioningStatus.AWAITING_SECRET,
        BotProvisioningStatus.CANCELLED,
    }
    completed = status == BotProvisioningStatus.COMPLETED
    return ManagedBotProvisioningRequest(
        id=_REQUEST_ID,
        business_id=_BUSINESS_ID,
        created_by_member_id=_MEMBER_ID,
        provider="botfather",
        status=status,
        idempotency_key="owner-ui-regression-001",
        requested_username="practice_helper_bot",
        display_name="Practice Helper",
        credential_reference=_TOKEN_REF if has_references else None,
        webhook_secret_reference=_WEBHOOK_REF if has_references else None,
        external_bot_id="900001" if completed else None,
        verified_username="practice_helper_bot" if completed else None,
        connection_id=_CONNECTION_ID if completed else None,
        managed_bot_id=_MANAGED_BOT_ID if completed else None,
        attempts=1 if has_references else 0,
        created_at="2026-07-29T09:00:00+00:00",
        updated_at="2026-07-29T09:01:00+00:00",
        completed_at="2026-07-29T09:02:00+00:00" if completed else None,
        failed_at=(
            "2026-07-29T09:02:00+00:00"
            if status == BotProvisioningStatus.FAILED
            else None
        ),
        cancelled_at=(
            "2026-07-29T09:02:00+00:00"
            if status == BotProvisioningStatus.CANCELLED
            else None
        ),
        last_error_code=(
            "telegram_verification_failed"
            if status == BotProvisioningStatus.FAILED
            else None
        ),
    )


class _FakeState:
    def __init__(self, data: dict[str, object] | None = None) -> None:
        self.data = dict(data or {})
        self.states: list[object] = []
        self.cleared = 0

    async def get_data(self) -> dict[str, object]:
        return dict(self.data)

    async def update_data(self, **kwargs: object) -> None:
        self.data.update(kwargs)

    async def set_state(self, value: object) -> None:
        self.states.append(value)

    async def clear(self) -> None:
        self.cleared += 1
        self.data.clear()


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.deleted = 0
        self.answers: list[tuple[str, object | None]] = []

    async def delete(self) -> None:
        self.deleted += 1

    async def answer(self, text: str, reply_markup: object | None = None) -> None:
        self.answers.append((text, reply_markup))


class _FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=101)
        self.message = _FakeMessage("")
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(
        self,
        text: str | None = None,
        *,
        show_alert: bool = False,
    ) -> None:
        self.answers.append((text, show_alert))


class ClientPlatformBotSetupUiTests(unittest.IsolatedAsyncioTestCase):
    def test_secret_reference_input_accepts_only_reviewed_environment_names(self) -> None:
        self.assertEqual(
            setup._secret_reference_from_input(
                "CLIENTPLATFORM_SECRET_TELEGRAM_PRACTICE"
            ),
            _TOKEN_REF,
        )
        self.assertEqual(
            setup._secret_reference_from_input(
                "secret://env/clientplatform_secret_webhook_practice"
            ),
            _WEBHOOK_REF,
        )
        with self.assertRaises(setup.RawSecretInputError):
            setup._secret_reference_from_input(_raw_token_fixture())
        with self.assertRaises(setup.RawSecretInputError):
            setup._secret_reference_from_input("raw:value")
        with self.assertRaises(ValueError):
            setup._secret_reference_from_input("TELEGRAM_TOKEN")

    def test_status_text_never_displays_secret_references(self) -> None:
        for status in BotProvisioningStatus:
            text = setup._status_text(_request(status))
            self.assertNotIn("CLIENTPLATFORM_SECRET_TELEGRAM_PRACTICE", text)
            self.assertNotIn("CLIENTPLATFORM_SECRET_WEBHOOK_PRACTICE", text)
            self.assertNotIn("secret://", text)

    def test_all_callback_payloads_fit_telegram_limit(self) -> None:
        requests = [None, *(_request(status) for status in BotProvisioningStatus)]
        for request in requests:
            markup = setup._status_keyboard(_BUSINESS_ID, request)
            for row in markup.inline_keyboard:
                for button in row:
                    self.assertIsNotNone(button.callback_data)
                    self.assertLessEqual(len(button.callback_data.encode("utf-8")), 64)

    def test_dashboard_button_installation_is_idempotent(self) -> None:
        def original(_business_id: str, _capabilities: list[object]):
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Клиенты", callback_data="cp:clients:x")]
                ]
            )

        module = SimpleNamespace(_dashboard_keyboard=original)
        setup.install_dashboard_button(module)
        setup.install_dashboard_button(module)
        markup = module._dashboard_keyboard(_BUSINESS_ID, [])
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertEqual(labels.count("Мой Telegram-бот"), 1)
        callback = markup.inline_keyboard[-1][0].callback_data
        self.assertTrue(str(callback).startswith("cpb:o:"))
        self.assertLessEqual(len(str(callback).encode("utf-8")), 64)

    async def test_accidentally_pasted_token_is_deleted_and_not_stored(self) -> None:
        raw_token = _raw_token_fixture()
        message = _FakeMessage(raw_token)
        state = _FakeState()
        await setup.receive_token_reference(message, state)
        self.assertEqual(message.deleted, 1)
        self.assertNotIn("token_reference", state.data)
        self.assertEqual(state.states, [])
        combined_answers = " ".join(text for text, _ in message.answers)
        self.assertNotIn(raw_token, combined_answers)
        self.assertIn("Сообщение удалено", combined_answers)

    async def test_valid_token_reference_advances_to_webhook_state(self) -> None:
        message = _FakeMessage("CLIENTPLATFORM_SECRET_TELEGRAM_PRACTICE")
        state = _FakeState()
        await setup.receive_token_reference(message, state)
        self.assertEqual(state.data["token_reference"], _TOKEN_REF)
        self.assertEqual(state.states[-1], setup.ManagedBotSetupState.webhook_reference)
        self.assertEqual(message.deleted, 0)

    async def test_same_reference_cannot_be_reused_for_webhook(self) -> None:
        message = _FakeMessage("CLIENTPLATFORM_SECRET_TELEGRAM_PRACTICE")
        state = _FakeState(
            {
                "business_id": _BUSINESS_ID,
                "request_id": _REQUEST_ID,
                "token_reference": _TOKEN_REF,
            }
        )
        await setup.receive_webhook_reference(message, state)
        self.assertEqual(state.cleared, 0)
        self.assertIn("отдельный секрет", message.answers[-1][0])

    async def test_mybot_command_without_business_prompts_onboarding(self) -> None:
        message = _FakeMessage("/mybot")
        state = _FakeState()
        with (
            patch.object(setup.control, "_user_id", return_value=101),
            patch.object(setup, "list_accessible_businesses", return_value=[]),
        ):
            await setup.open_my_bot_command(message, state)
        self.assertEqual(state.cleared, 1)
        self.assertIn("Сначала создайте бизнес", message.answers[-1][0])

    async def test_mybot_command_with_multiple_businesses_builds_selector(self) -> None:
        second_business_id = "00000000-0000-0000-0000-000000000106"
        accesses = [
            SimpleNamespace(
                business=SimpleNamespace(id=_BUSINESS_ID, name="Практика один")
            ),
            SimpleNamespace(
                business=SimpleNamespace(id=second_business_id, name="Практика два")
            ),
        ]
        message = _FakeMessage("/mybot")
        state = _FakeState()
        with (
            patch.object(setup.control, "_user_id", return_value=101),
            patch.object(setup, "list_accessible_businesses", return_value=accesses),
        ):
            await setup.open_my_bot_command(message, state)
        markup = message.answers[-1][1]
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertEqual(labels, ["Практика один", "Практика два"])

    async def test_begin_setup_starts_username_state(self) -> None:
        callback = _FakeCallback(f"cpb:n:{setup._business_token(_BUSINESS_ID)}")
        state = _FakeState()
        with (
            patch.object(setup, "_actor", new=AsyncMock(return_value=object())),
            patch.object(
                setup.control,
                "_callback_message",
                return_value=callback.message,
            ),
        ):
            await setup.begin_bot_setup(callback, state)
        self.assertEqual(state.states[-1], setup.ManagedBotSetupState.username)
        self.assertEqual(state.data["business_id"], _BUSINESS_ID)
        self.assertTrue(str(state.data["idempotency_key"]).startswith("owner-ui-"))
        self.assertIn("Шаг 1 из 3", callback.message.answers[-1][0])

    async def test_valid_username_creates_request_and_advances(self) -> None:
        message = _FakeMessage("@practice_helper_bot")
        state = _FakeState(
            {
                "business_id": _BUSINESS_ID,
                "idempotency_key": "owner-ui-test-001",
            }
        )
        with (
            patch.object(setup.control, "_user_id", return_value=101),
            patch.object(setup, "_actor", new=AsyncMock(return_value=object())),
            patch.object(
                setup,
                "create_botfather_provisioning",
                return_value=_request(BotProvisioningStatus.AWAITING_SECRET),
            ) as create_request,
        ):
            await setup.receive_bot_username(message, state)
        create_request.assert_called_once()
        self.assertEqual(state.data["request_id"], _REQUEST_ID)
        self.assertEqual(state.states[-1], setup.ManagedBotSetupState.token_reference)
        self.assertIn("Шаг 2 из 3", message.answers[-1][0])

    async def test_distinct_webhook_reference_is_submitted_and_clears_state(self) -> None:
        message = _FakeMessage("CLIENTPLATFORM_SECRET_WEBHOOK_PRACTICE")
        state = _FakeState(
            {
                "business_id": _BUSINESS_ID,
                "request_id": _REQUEST_ID,
                "token_reference": _TOKEN_REF,
            }
        )
        with (
            patch.object(setup.control, "_user_id", return_value=101),
            patch.object(setup, "_actor", new=AsyncMock(return_value=object())),
            patch.object(
                setup,
                "submit_botfather_secret_references",
                return_value=_request(BotProvisioningStatus.READY),
            ) as submit,
        ):
            await setup.receive_webhook_reference(message, state)
        submit.assert_called_once()
        self.assertEqual(state.cleared, 1)
        self.assertIn("Ссылки сохранены", message.answers[-1][0])
        self.assertIsNotNone(message.answers[-1][1])

    async def test_verify_success_reports_completion_and_refreshes_status(self) -> None:
        callback = _FakeCallback(
            f"cpb:v:{setup._business_token(_BUSINESS_ID)}:"
            f"{setup._request_token(_REQUEST_ID)}"
        )
        state = _FakeState()
        refresh = AsyncMock()
        with (
            patch.object(setup, "_actor", new=AsyncMock(return_value=object())),
            patch.object(
                setup.control,
                "_callback_message",
                return_value=callback.message,
            ),
            patch.object(
                setup,
                "finalize_botfather_provisioning",
                new=AsyncMock(return_value=_request(BotProvisioningStatus.COMPLETED)),
            ),
            patch.object(setup, "_send_status", new=refresh),
        ):
            await setup.verify_and_connect_bot(callback, state)
        answers = " ".join(text for text, _ in callback.message.answers)
        self.assertIn("Готово", answers)
        self.assertEqual(state.cleared, 1)
        refresh.assert_awaited_once()

    async def test_verify_failure_is_safe_and_refreshes_status(self) -> None:
        callback = _FakeCallback(
            f"cpb:v:{setup._business_token(_BUSINESS_ID)}:"
            f"{setup._request_token(_REQUEST_ID)}"
        )
        state = _FakeState()
        refresh = AsyncMock()
        with (
            patch.object(setup, "_actor", new=AsyncMock(return_value=object())),
            patch.object(
                setup.control,
                "_callback_message",
                return_value=callback.message,
            ),
            patch.object(
                setup,
                "finalize_botfather_provisioning",
                new=AsyncMock(
                    side_effect=BotProvisioningVerificationFailed("provider failure")
                ),
            ),
            patch.object(setup, "_send_status", new=refresh),
        ):
            await setup.verify_and_connect_bot(callback, state)
        answers = " ".join(text for text, _ in callback.message.answers)
        self.assertIn("Telegram не подтвердил", answers)
        self.assertNotIn(_TOKEN_REF, answers)
        self.assertEqual(state.cleared, 1)
        refresh.assert_awaited_once()

    async def test_cancel_success_and_active_verification_guard(self) -> None:
        callback_data = (
            f"cpb:c:{setup._business_token(_BUSINESS_ID)}:"
            f"{setup._request_token(_REQUEST_ID)}"
        )
        successful = _FakeCallback(callback_data)
        state = _FakeState()
        refresh = AsyncMock()
        with (
            patch.object(setup, "_actor", new=AsyncMock(return_value=object())),
            patch.object(
                setup.control,
                "_callback_message",
                return_value=successful.message,
            ),
            patch.object(
                setup,
                "cancel_botfather_provisioning",
                return_value=_request(BotProvisioningStatus.CANCELLED),
            ),
            patch.object(setup, "_send_status", new=refresh),
        ):
            await setup.cancel_bot_setup(successful, state)
        self.assertEqual(successful.answers[-1][0], "Подключение отменено")
        self.assertEqual(state.cleared, 1)
        refresh.assert_awaited_once()

        blocked = _FakeCallback(callback_data)
        blocked_state = _FakeState()
        with (
            patch.object(setup, "_actor", new=AsyncMock(return_value=object())),
            patch.object(
                setup,
                "cancel_botfather_provisioning",
                side_effect=BotProvisioningInvariantViolation("verifying"),
            ),
        ):
            await setup.cancel_bot_setup(blocked, blocked_state)
        self.assertTrue(blocked.answers[-1][1])
        self.assertEqual(blocked_state.cleared, 0)

    def test_lazy_handler_composition_contains_owner_wizard_once(self) -> None:
        import handlers

        entry, control = handlers._load_clientplatform_modules()
        entry_again, control_again = handlers._load_clientplatform_modules()
        self.assertIs(entry, entry_again)
        self.assertIs(control, control_again)
        names = [item.name for item in entry.router.sub_routers]
        self.assertEqual(names.count("clientplatform_bot_setup"), 1)
        dashboard = control._dashboard_keyboard(_BUSINESS_ID, [])
        labels = [button.text for row in dashboard.inline_keyboard for button in row]
        self.assertEqual(labels.count("Мой Telegram-бот"), 1)


if __name__ == "__main__":
    unittest.main()
