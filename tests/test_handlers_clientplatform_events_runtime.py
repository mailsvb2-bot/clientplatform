from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from clientplatform.domain.tenancy import TenantPermissionDenied
from handlers import clientplatform_events as events


BUSINESS_ID = "11111111-1111-4111-8111-111111111111"
TOKEN = "ERERERERQRGBEREREREREQ"


def _callback() -> SimpleNamespace:
    return SimpleNamespace(
        data=f"cpev:new:{TOKEN}",
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
    )


def _message(text: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
    )


class EventHandlerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_public_base_url_requires_https_and_normalizes_slash(self) -> None:
        with patch.object(events.settings, "MESSENGER_PUBLIC_BASE_URL", "http://unsafe.test"):
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                events._public_base_url()
        with patch.object(events.settings, "MESSENGER_PUBLIC_BASE_URL", "https://events.example.test/"):
            self.assertEqual(events._public_base_url(), "https://events.example.test")

    async def test_start_wizard_denies_member_without_management_permission(self) -> None:
        callback = _callback()
        state = AsyncMock()
        actor = MagicMock(unsafe=True)
        actor.assert_can_manage_business.side_effect = TenantPermissionDenied("denied")
        with (
            patch.object(events.control, "_token_uuid", return_value=BUSINESS_ID),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
        ):
            await events.start_event_wizard(callback, state)
        callback.answer.assert_awaited_once_with(
            "Создавать мероприятия может владелец или администратор",
            show_alert=True,
        )
        state.clear.assert_not_awaited()

    async def test_start_wizard_sets_business_scoped_state(self) -> None:
        callback = _callback()
        state = AsyncMock()
        actor = MagicMock(unsafe=True)
        reply = SimpleNamespace(answer=AsyncMock())
        with (
            patch.object(events.control, "_token_uuid", return_value=BUSINESS_ID),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events.control, "_callback_message", return_value=reply),
            patch.object(events, "_cancel_keyboard", return_value="cancel"),
        ):
            await events.start_event_wizard(callback, state)
        actor.assert_can_manage_business.assert_called_once_with()
        state.clear.assert_awaited_once_with()
        state.set_state.assert_awaited_once_with(events.ClientPlatformEventState.waiting_details)
        state.update_data.assert_awaited_once_with(event_business_id=BUSINESS_ID)
        callback.answer.assert_awaited_once_with()
        reply.answer.assert_awaited_once()

    async def test_receive_details_handles_missing_state_and_bad_shape(self) -> None:
        message = _message("bad")
        state = AsyncMock()
        state.get_data.return_value = {}
        await events.receive_event_details(message, state)
        state.clear.assert_awaited_once_with()
        self.assertIn("Откройте кабинет заново", message.answer.await_args.args[0])

        message = _message("только одно поле")
        state = AsyncMock()
        state.get_data.return_value = {"event_business_id": BUSINESS_ID}
        actor = MagicMock(unsafe=True)
        with (
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "_cancel_keyboard", return_value="cancel"),
        ):
            await events.receive_event_details(message, state)
        actor.assert_can_manage_business.assert_called_once_with()
        self.assertIn("Нужны 3–4 поля", message.answer.await_args.args[0])

    async def test_receive_details_reports_validation_error_without_clearing_state(self) -> None:
        message = _message("Эфир | 15.09.2026 19:00 | http://unsafe.example")
        state = AsyncMock()
        state.get_data.return_value = {"event_business_id": BUSINESS_ID}
        actor = MagicMock(unsafe=True)
        with (
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "get_business_profile", return_value=SimpleNamespace(timezone="Europe/Moscow")),
            patch.object(events, "parse_local_booking_start", side_effect=ValueError("bad time")),
            patch.object(events, "_cancel_keyboard", return_value="cancel"),
        ):
            await events.receive_event_details(message, state)
        self.assertIn("Не удалось создать мероприятие", message.answer.await_args.args[0])
        state.clear.assert_not_awaited()

    async def test_receive_details_creates_provider_neutral_event_and_returns_registration(self) -> None:
        message = _message(
            "Эфир | 15.09.2026 19:00 | https://future-stage.example/room | https://shop.example/offer"
        )
        state = AsyncMock()
        state.get_data.return_value = {"event_business_id": BUSINESS_ID}
        actor = MagicMock(unsafe=True)
        created = SimpleNamespace(
            provider_key="future_stage_2030",
            email_notifications_enabled=True,
            registration_url=lambda base: f"{base}/e/public-slug",
        )
        with (
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "get_business_profile", return_value=SimpleNamespace(timezone="Europe/Moscow")),
            patch.object(
                events,
                "parse_local_booking_start",
                return_value=datetime(2026, 9, 15, 16, 0, tzinfo=timezone.utc).isoformat(),
            ),
            patch.object(events, "create_and_publish_online_event", return_value=created) as create,
            patch.object(events, "_public_base_url", return_value="https://clientplatform.example.test"),
            patch.object(events.control, "_keyboard", return_value="keyboard"),
            patch.object(events.control, "_uuid_token", return_value=TOKEN),
        ):
            await events.receive_event_details(message, state)
        actor.assert_can_manage_business.assert_called_once_with()
        request = create.call_args.kwargs["request"]
        self.assertEqual(request.title, "Эфир")
        self.assertEqual(request.join_url, "https://future-stage.example/room")
        self.assertEqual(request.offer_url, "https://shop.example/offer")
        state.clear.assert_awaited_once_with()
        answer = message.answer.await_args.args[0]
        self.assertIn("future_stage_2030", answer)
        self.assertIn("https://clientplatform.example.test/e/public-slug", answer)
        self.assertIn("Напоминания по e-mail включены", answer)

    async def test_receive_details_supports_external_provider_without_offer_or_email(self) -> None:
        message = _message("Эфир | 15.09.2026 19:00 | https://stream.example/room | -")
        state = AsyncMock()
        state.get_data.return_value = {"event_business_id": BUSINESS_ID}
        actor = MagicMock(unsafe=True)
        created = SimpleNamespace(
            provider_key="external",
            email_notifications_enabled=False,
            registration_url=lambda base: f"{base}/e/public-slug",
        )
        with (
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "get_business_profile", return_value=SimpleNamespace(timezone="Europe/Moscow")),
            patch.object(events, "parse_local_booking_start", return_value="2026-09-15T16:00:00+00:00"),
            patch.object(events, "create_and_publish_online_event", return_value=created) as create,
            patch.object(events, "_public_base_url", return_value="https://clientplatform.example.test"),
            patch.object(events.control, "_keyboard", return_value="keyboard"),
            patch.object(events.control, "_uuid_token", return_value=TOKEN),
        ):
            await events.receive_event_details(message, state)
        self.assertIsNone(create.call_args.kwargs["request"].offer_url)
        answer = message.answer.await_args.args[0]
        self.assertIn("внешняя площадка", answer)
        self.assertIn("E-mail не подключён", answer)

    async def test_cancel_rejects_stale_business_and_clears_matching_state(self) -> None:
        callback = _callback()
        callback.data = f"cpev:cancel:{TOKEN}"
        state = AsyncMock()
        state.get_data.return_value = {"event_business_id": "22222222-2222-4222-8222-222222222222"}
        with (
            patch.object(events.control, "_token_uuid", return_value=BUSINESS_ID),
            patch.object(events.control, "_actor", new=AsyncMock()),
        ):
            await events.cancel_event_wizard(callback, state)
        callback.answer.assert_awaited_once_with("Этот шаг уже устарел", show_alert=True)
        state.clear.assert_not_awaited()

        callback = _callback()
        callback.data = f"cpev:cancel:{TOKEN}"
        state = AsyncMock()
        state.get_data.return_value = {"event_business_id": BUSINESS_ID}
        reply = SimpleNamespace(answer=AsyncMock())
        with (
            patch.object(events.control, "_token_uuid", return_value=BUSINESS_ID),
            patch.object(events.control, "_actor", new=AsyncMock()),
            patch.object(events.control, "_callback_message", return_value=reply),
            patch.object(events.control, "_keyboard", return_value="keyboard"),
        ):
            await events.cancel_event_wizard(callback, state)
        state.clear.assert_awaited_once_with()
        callback.answer.assert_awaited_once_with("Создание отменено")
        reply.answer.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
