from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from clientplatform.domain.event_content import EventContentMode
from clientplatform.domain.tenancy import TenantPermissionDenied
from handlers import clientplatform_events as events


BUSINESS_ID = "11111111-1111-4111-8111-111111111111"
TOKEN = "ERERERERQRGBEREREREREQ"
EVENT_ID = "33333333-3333-4333-8333-333333333333"
EVENT_TOKEN = "MzMzMzMzQzODMzMzMzMzMzMzMz"


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

    def test_cancel_keyboard_uses_business_scoped_callback(self) -> None:
        with (
            patch.object(events.control, "_uuid_token", return_value=TOKEN),
            patch.object(events.control, "_keyboard", side_effect=lambda rows: rows),
        ):
            self.assertEqual(
                events._cancel_keyboard(BUSINESS_ID),
                [[("🎥 К вебинарам", f"cpev:cancel:{TOKEN}")]],
            )

    async def test_webinar_hub_routes_to_canonical_events_screen(self) -> None:
        callback = _callback()
        callback.data = f"cpev:home:{TOKEN}"
        reply = SimpleNamespace(answer=AsyncMock())
        with (
            patch.object(events.control, "_token_uuid", return_value=BUSINESS_ID),
            patch.object(events.control, "_callback_message", return_value=reply),
            patch(
                "handlers.clientplatform_cockpit_dispatch.send_cockpit_section",
                new=AsyncMock(),
            ) as send,
        ):
            await events.open_event_hub(callback)
        callback.answer.assert_awaited_once_with()
        send.assert_awaited_once_with(
            reply, user_id=101, business_id=BUSINESS_ID, section="events"
        )

    async def test_webinar_settings_callback_opens_dedicated_settings_screen(self) -> None:
        callback = _callback()
        callback.data = f"cpev:settings:{TOKEN}"
        reply = SimpleNamespace(answer=AsyncMock())
        with (
            patch.object(events.control, "_token_uuid", return_value=BUSINESS_ID),
            patch.object(events.control, "_callback_message", return_value=reply),
            patch.object(events, "_send_event_settings", new=AsyncMock()) as send,
        ):
            await events.open_event_settings(callback)
        callback.answer.assert_awaited_once_with()
        send.assert_awaited_once_with(
            reply, user_id=101, business_id=BUSINESS_ID
        )

    def test_event_settings_rows_keep_mutations_inside_settings_and_offer_back_to_hub(self) -> None:
        snapshot = SimpleNamespace(
            can_manage=True,
            can_enable_commercial_followups=True,
            can_expand_commercial_followups=True,
            commercial_followups_enabled=False,
            commercial_followup_segments=("no_show",),
            commercial_followup_channels=("email",),
        )
        with patch.object(events.control, "_keyboard", side_effect=lambda rows: rows):
            rows = events._settings_rows(snapshot, token=TOKEN)
        callbacks = [callback for row in rows for _label, callback in row]
        self.assertIn(f"cpev:followups:on:{TOKEN}", callbacks)
        self.assertIn(f"cpev:home:{TOKEN}", callbacks)
        self.assertNotIn(f"cpev:settings:{TOKEN}", callbacks)

    async def test_send_event_settings_renders_dedicated_screen(self) -> None:
        target = SimpleNamespace(answer=AsyncMock())
        snapshot = SimpleNamespace(
            can_manage=True,
            can_enable_commercial_followups=True,
            can_expand_commercial_followups=True,
            commercial_followups_enabled=False,
            commercial_followups_effective=False,
            commercial_followups_platform_available=True,
            commercial_followup_segments=("no_show",),
            commercial_followup_channels=("email",),
            limitations=(),
        )
        with (
            patch.object(events, "resolve_cockpit_events", return_value=snapshot) as resolve,
            patch.object(events.control, "_uuid_token", return_value=TOKEN),
            patch.object(events.control, "_keyboard", side_effect=lambda rows: rows),
        ):
            await events._send_event_settings(
                target, user_id=101, business_id=BUSINESS_ID
            )
        resolve.assert_called_once_with(
            telegram_user_id=101, requested_business_id=BUSINESS_ID, limit=5
        )
        text = target.answer.await_args.args[0]
        rows = target.answer.await_args.kwargs["reply_markup"]
        callbacks = [callback for row in rows for _label, callback in row]
        self.assertIn("⚙️ Автосообщения вебинара", text)
        self.assertIn(f"cpev:followups:on:{TOKEN}", callbacks)
        self.assertEqual(rows[-1], [("🎥 К вебинарам", f"cpev:home:{TOKEN}")])

    def test_event_settings_rows_reject_unknown_semantic_action(self) -> None:
        unknown = SimpleNamespace(kind="unknown", label="Неизвестно", key=None, enabled=None)
        with patch.object(events, "event_settings_actions", return_value=(unknown,)):
            with self.assertRaisesRegex(ValueError, "unsupported event settings action"):
                events._settings_rows(SimpleNamespace(), token=TOKEN)

    def test_event_settings_rows_for_read_only_snapshot_offer_only_back(self) -> None:
        snapshot = SimpleNamespace(can_manage=False)
        rows = events._settings_rows(snapshot, token=TOKEN)
        self.assertEqual(rows, [[("🎥 К вебинарам", f"cpev:home:{TOKEN}")]])

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
            patch.object(
                events, "get_business_profile", return_value=SimpleNamespace(timezone="Europe/Moscow")
            ),
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

    async def test_receive_details_handles_missing_state_and_starts_mobile_flow(self) -> None:
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
            patch.object(
                events,
                "get_business_profile",
                return_value=SimpleNamespace(timezone="Europe/Moscow"),
            ),
            patch.object(events, "_cancel_keyboard", return_value="cancel"),
        ):
            await events.receive_event_details(message, state)
        actor.assert_can_manage_business.assert_called_once_with()
        state.update_data.assert_awaited_once_with(event_title="только одно поле")
        state.set_state.assert_awaited_once_with(events.ClientPlatformEventState.waiting_time)
        state.clear.assert_not_awaited()
        answer = message.answer.await_args.args[0]
        self.assertIn("Когда провести вебинар?", answer)
        self.assertIn("Europe/Moscow", answer)
        self.assertIn("Добавить ссылку на эфир", answer)

    async def test_receive_time_requires_complete_business_scoped_state(self) -> None:
        message = _message("16.09.2026 23:30")
        state = AsyncMock()
        state.get_data.return_value = {"event_business_id": BUSINESS_ID}

        await events.receive_event_time(message, state)

        state.clear.assert_awaited_once_with()
        self.assertIn("Откройте вебинары заново", message.answer.await_args.args[0])

    async def test_receive_time_cancel_returns_to_webinars(self) -> None:
        message = _message("Отмена")
        state = AsyncMock()
        state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "event_title": "Простой вебинар",
        }
        actor = MagicMock(unsafe=True)
        with (
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events.control, "_uuid_token", return_value=TOKEN),
            patch.object(events.control, "_keyboard", side_effect=lambda rows: rows),
        ):
            await events.receive_event_time(message, state)

        actor.assert_can_manage_business.assert_called_once_with()
        state.clear.assert_awaited_once_with()
        self.assertIn("Создание вебинара отменено", message.answer.await_args.args[0])
        self.assertEqual(
            message.answer.await_args.kwargs["reply_markup"],
            [[("🎥 К вебинарам", f"cpev:home:{TOKEN}")]],
        )

    async def test_receive_time_reports_bad_date_without_clearing_state(self) -> None:
        message = _message("когда-нибудь вечером")
        state = AsyncMock()
        state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "event_title": "Простой вебинар",
        }
        actor = MagicMock(unsafe=True)
        with (
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(
                events,
                "get_business_profile",
                return_value=SimpleNamespace(timezone="Europe/Moscow"),
            ),
            patch.object(
                events, "parse_local_booking_start", side_effect=ValueError("bad time")
            ),
            patch.object(events, "_cancel_keyboard", return_value="cancel"),
        ):
            await events.receive_event_time(message, state)

        actor.assert_can_manage_business.assert_called_once_with()
        state.clear.assert_not_awaited()
        self.assertIn("Не удалось понять дату и время", message.answer.await_args.args[0])

    async def test_receive_time_creates_event_without_requiring_join_url(self) -> None:
        message = _message("16.09.2026 23:30")
        state = AsyncMock()
        state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "event_title": "Простой вебинар",
        }
        actor = MagicMock(unsafe=True)
        created = SimpleNamespace(
            event_id=EVENT_ID,
            provider_key="pending",
            email_notifications_enabled=False,
            registration_url=lambda base: f"{base}/e/public-slug",
        )

        def _token(value: str) -> str:
            return EVENT_TOKEN if value == EVENT_ID else TOKEN

        with (
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(
                events,
                "get_business_profile",
                return_value=SimpleNamespace(timezone="Europe/Moscow"),
            ),
            patch.object(
                events,
                "parse_local_booking_start",
                return_value="2026-09-16T20:30:00+00:00",
            ),
            patch.object(
                events, "create_and_publish_online_event", return_value=created
            ) as create,
            patch.object(
                events, "_public_base_url", return_value="https://clientplatform.example.test"
            ),
            patch.object(events.control, "_keyboard", side_effect=lambda rows: rows) as keyboard,
            patch.object(events.control, "_uuid_token", side_effect=_token),
        ):
            await events.receive_event_time(message, state)

        actor.assert_can_manage_business.assert_called_once_with()
        request = create.call_args.kwargs["request"]
        self.assertEqual(request.title, "Простой вебинар")
        self.assertEqual(request.timezone_name, "Europe/Moscow")
        self.assertIsNone(request.join_url)
        self.assertIsNone(request.offer_url)
        state.clear.assert_awaited_once_with()
        answer = message.answer.await_args.args[0]
        self.assertIn("✅ Вебинар опубликован", answer)
        self.assertIn("Ссылку на эфир можно добавить позже", answer)
        rows = keyboard.call_args.args[0]
        self.assertEqual(
            rows[0],
            [("🔗 Добавить ссылку на эфир", f"cpev:join:{EVENT_TOKEN}:{TOKEN}")],
        )
        self.assertEqual(
            rows[1],
            [("🗓 Контент-план", f"cpev:content:{EVENT_TOKEN}:{TOKEN}")],
        )
        self.assertEqual(
            rows[2],
            [("✨ Сделать анонс", f"cpev:announce:{EVENT_TOKEN}:{TOKEN}")],
        )

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
        self.assertIn("Не удалось создать вебинар", message.answer.await_args.args[0])
        state.clear.assert_not_awaited()

    async def test_receive_details_creates_provider_neutral_event_and_returns_registration(self) -> None:
        message = _message(
            "Эфир | 15.09.2026 19:00 | https://future-stage.example/room | https://shop.example/offer"
        )
        state = AsyncMock()
        state.get_data.return_value = {"event_business_id": BUSINESS_ID}
        actor = MagicMock(unsafe=True)
        created = SimpleNamespace(
            event_id=EVENT_ID,
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
            patch.object(events.control, "_keyboard", side_effect=lambda rows: rows) as keyboard,
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
        self.assertIn("E-mail напоминания включены", answer)
        post_create_rows = keyboard.call_args.args[0]
        callbacks = [callback for row in post_create_rows for _label, callback in row]
        self.assertIn(f"cpev:content:{TOKEN}:{TOKEN}", callbacks)
        self.assertIn(f"cpev:announce:{TOKEN}:{TOKEN}", callbacks)
        self.assertIn(f"cpev:home:{TOKEN}", callbacks)
        self.assertIn(f"cpev:new:{TOKEN}", callbacks)

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

    async def test_typed_cancel_exits_creation_and_returns_to_webinars(self) -> None:
        message = _message("Отмена")
        state = AsyncMock()
        state.get_data.return_value = {"event_business_id": BUSINESS_ID}
        actor = MagicMock(unsafe=True)
        with (
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events.control, "_uuid_token", return_value=TOKEN),
            patch.object(events.control, "_keyboard", side_effect=lambda rows: rows),
        ):
            await events.receive_event_details(message, state)
        state.clear.assert_awaited_once_with()
        self.assertIn("Создание вебинара отменено", message.answer.await_args.args[0])
        self.assertEqual(
            message.answer.await_args.kwargs["reply_markup"],
            [[("🎥 К вебинарам", f"cpev:home:{TOKEN}")]],
        )

    async def test_toggle_autosend_is_idempotent_when_preference_already_matches(self) -> None:
        callback = _callback()
        callback.data = f"cpev:followups:off:{TOKEN}"
        actor = MagicMock(unsafe=True)
        reply = SimpleNamespace(answer=AsyncMock())
        stored = SimpleNamespace(enabled=False)
        with (
            patch.object(events.control, "_token_uuid", return_value=BUSINESS_ID),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "get_business_event_followup_settings", return_value=stored),
            patch.object(events, "set_business_event_followups_enabled") as setter,
            patch.object(events.control, "_callback_message", return_value=reply),
            patch.object(events, "_send_event_settings", new=AsyncMock()) as refresh,
        ):
            await events.toggle_event_followups(callback)
        setter.assert_not_called()
        callback.answer.assert_awaited_once_with("Автоматические сообщения выключены")
        refresh.assert_awaited_once_with(
            reply, user_id=101, business_id=BUSINESS_ID
        )

    async def test_toggle_autosend_enables_business_setting_and_refreshes_events(self) -> None:
        callback = _callback()
        callback.data = f"cpev:followups:on:{TOKEN}"
        actor = MagicMock(unsafe=True)
        reply = SimpleNamespace(answer=AsyncMock())
        with (
            patch.object(events.control, "_token_uuid", return_value=BUSINESS_ID),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "get_business_event_followup_settings", return_value=None),
            patch.object(events, "set_business_event_followups_enabled", return_value=True) as setter,
            patch.object(events.control, "_callback_message", return_value=reply),
            patch.object(events, "_send_event_settings", new=AsyncMock()) as refresh,
        ):
            await events.toggle_event_followups(callback)
        setter.assert_called_once_with(actor=actor, enabled=True)
        callback.answer.assert_awaited_once_with("Автоматические сообщения включены")
        refresh.assert_awaited_once_with(
            reply, user_id=101, business_id=BUSINESS_ID
        )

    async def test_toggle_event_segment_updates_strategy_and_refreshes_events(self) -> None:
        callback = _callback()
        callback.data = f"cpev:seg:attended_unpaid:off:{TOKEN}"
        actor = MagicMock(unsafe=True)
        reply = SimpleNamespace(answer=AsyncMock())
        with (
            patch.object(events.control, "_token_uuid", return_value=BUSINESS_ID),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "set_business_event_followup_segment_enabled") as setter,
            patch.object(events.control, "_callback_message", return_value=reply),
            patch.object(events, "_send_event_settings", new=AsyncMock()) as refresh,
        ):
            await events.toggle_event_followup_segment(callback)
        setter.assert_called_once_with(actor=actor, segment="attended_unpaid", enabled=False)
        callback.answer.assert_awaited_once_with("Настройка участников сохранена")
        refresh.assert_awaited_once_with(
            reply, user_id=101, business_id=BUSINESS_ID
        )

    async def test_toggle_event_channel_updates_strategy_and_refreshes_events(self) -> None:
        callback = _callback()
        callback.data = f"cpev:ch:max:off:{TOKEN}"
        actor = MagicMock(unsafe=True)
        reply = SimpleNamespace(answer=AsyncMock())
        with (
            patch.object(events.control, "_token_uuid", return_value=BUSINESS_ID),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "set_business_event_followup_channel_enabled") as setter,
            patch.object(events.control, "_callback_message", return_value=reply),
            patch.object(events, "_send_event_settings", new=AsyncMock()) as refresh,
        ):
            await events.toggle_event_followup_channel(callback)
        setter.assert_called_once_with(actor=actor, channel="max", enabled=False)
        callback.answer.assert_awaited_once_with("Настройка канала сохранена")
        refresh.assert_awaited_once_with(
            reply, user_id=101, business_id=BUSINESS_ID
        )

    async def test_event_autosend_callbacks_surface_stale_permission_and_validation_errors(self) -> None:
        stale = _callback()
        stale.data = "cpev:followups:maybe:broken"
        await events.toggle_event_followups(stale)
        stale.answer.assert_awaited_once_with("Переключатель устарел", show_alert=True)

        actor = MagicMock(unsafe=True)
        for error, expected in (
            (TenantPermissionDenied("denied"), "Включить автоматические сообщения после мероприятия может владелец бизнеса"),
            (ValueError("platform stop"), "platform stop"),
        ):
            callback = _callback()
            callback.data = f"cpev:followups:on:{TOKEN}"
            with (
                patch.object(events.control, "_token_uuid", return_value=BUSINESS_ID),
                patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
                patch.object(events, "get_business_event_followup_settings", return_value=None),
                patch.object(events, "set_business_event_followups_enabled", side_effect=error),
            ):
                await events.toggle_event_followups(callback)
            callback.answer.assert_awaited_once_with(expected, show_alert=True)

    async def test_event_strategy_callbacks_surface_stale_permission_and_validation_errors(self) -> None:
        stale_segment = _callback()
        stale_segment.data = "cpev:seg:attended_unpaid:maybe:broken"
        await events.toggle_event_followup_segment(stale_segment)
        stale_segment.answer.assert_awaited_once_with("Настройка устарела", show_alert=True)

        stale_channel = _callback()
        stale_channel.data = "cpev:ch:max:maybe:broken"
        await events.toggle_event_followup_channel(stale_channel)
        stale_channel.answer.assert_awaited_once_with("Настройка устарела", show_alert=True)

        actor = MagicMock(unsafe=True)
        cases = (
            (events.toggle_event_followup_segment, "cpev:seg:attended_unpaid:on", "set_business_event_followup_segment_enabled", TenantPermissionDenied("denied"), "Расширить группы участников вебинара может только владелец бизнеса"),
            (events.toggle_event_followup_segment, "cpev:seg:attended_unpaid:off", "set_business_event_followup_segment_enabled", ValueError("last segment"), "last segment"),
            (events.toggle_event_followup_channel, "cpev:ch:max:on", "set_business_event_followup_channel_enabled", TenantPermissionDenied("denied"), "Расширить каналы сообщений участникам вебинара может только владелец бизнеса"),
            (events.toggle_event_followup_channel, "cpev:ch:max:off", "set_business_event_followup_channel_enabled", ValueError("last channel"), "last channel"),
        )
        for handler, prefix, setter_name, error, expected in cases:
            callback = _callback()
            callback.data = f"{prefix}:{TOKEN}"
            with (
                patch.object(events.control, "_token_uuid", return_value=BUSINESS_ID),
                patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
                patch.object(events, setter_name, side_effect=error),
            ):
                await handler(callback)
            callback.answer.assert_awaited_once_with(expected, show_alert=True)

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
            patch.object(events.control, "_keyboard", side_effect=lambda rows: rows) as keyboard,
        ):
            await events.cancel_event_wizard(callback, state)
        state.clear.assert_awaited_once_with()
        callback.answer.assert_awaited_once_with("Создание отменено")
        reply.answer.assert_awaited_once()
        self.assertEqual(
            keyboard.call_args.args[0],
            [[("🎥 К вебинарам", f"cpev:home:{TOKEN}")]],
        )


    async def test_receive_details_without_join_target_offers_join_and_announce(self) -> None:
        message = _message("Эфир | 15.09.2026 19:00 | - | -")
        state = AsyncMock()
        state.get_data.return_value = {"event_business_id": BUSINESS_ID}
        actor = MagicMock(unsafe=True)
        created = SimpleNamespace(
            event_id=EVENT_ID,
            join_ready=False,
            provider_key="pending",
            email_notifications_enabled=False,
            registration_url=lambda base: f"{base}/e/public-slug",
        )

        def token(value: str) -> str:
            return EVENT_TOKEN if value == EVENT_ID else TOKEN

        with (
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "get_business_profile", return_value=SimpleNamespace(timezone="Europe/Moscow")),
            patch.object(events, "parse_local_booking_start", return_value="2026-09-15T16:00:00+00:00"),
            patch.object(events, "create_and_publish_online_event", return_value=created) as create,
            patch.object(events, "_public_base_url", return_value="https://clientplatform.example.test"),
            patch.object(events.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(events.control, "_uuid_token", side_effect=token),
        ):
            await events.receive_event_details(message, state)

        self.assertIsNone(create.call_args.kwargs["request"].join_url)
        self.assertIsNone(create.call_args.kwargs["request"].offer_url)
        rows = message.answer.await_args.kwargs["reply_markup"]
        callbacks = [callback for row in rows for _label, callback in row]
        self.assertIn(f"cpev:join:{EVENT_TOKEN}:{TOKEN}", callbacks)
        self.assertIn(f"cpev:content:{EVENT_TOKEN}:{TOKEN}", callbacks)
        self.assertIn(f"cpev:announce:{EVENT_TOKEN}:{TOKEN}", callbacks)
        self.assertEqual(
            callbacks.count(f"cpev:content:{EVENT_TOKEN}:{TOKEN}"),
            1,
        )
        self.assertIn("ссылку на эфир можно добавить позже", message.answer.await_args.args[0].casefold())

    async def test_announcement_callback_covers_success_stale_and_safe_failure(self) -> None:
        reply = SimpleNamespace(answer=AsyncMock())
        actor = MagicMock(unsafe=True)
        draft = SimpleNamespace(
            title="Вебинар",
            text="AI-анонс по подтверждённым фактам",
            generated_by="ai:test:model",
            registration_url=lambda *, public_base_url, source: f"{public_base_url}/e/demo?source={source}",
        )
        callback = _callback()
        callback.data = f"cpev:announce:{EVENT_TOKEN}:{TOKEN}"

        def decode(value: str) -> str:
            return EVENT_ID if value == EVENT_TOKEN else BUSINESS_ID

        with (
            patch.object(events.control, "_token_uuid", side_effect=decode),
            patch.object(events.control, "_uuid_token", return_value=TOKEN),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "draft_event_announcement", new=AsyncMock(return_value=draft)),
            patch.object(
                events,
                "get_event_content_plan",
                return_value=SimpleNamespace(event_day=EventContentMode.TEXT),
            ),
            patch.object(events, "_public_base_url", return_value="https://clientplatform.example.test"),
            patch.object(events.control, "_callback_message", return_value=reply),
        ):
            await events.create_event_announcement(callback)

        actor.assert_can_manage_business.assert_called_once_with()
        callback.answer.assert_awaited_once_with("Анонс готов")
        text = reply.answer.await_args.args[0]
        self.assertIn("требует Вашего подтверждения", text)
        self.assertIn("🔗 Ссылка для рекламы:", text)
        self.assertIn("source=ads", text)
        markup = reply.answer.await_args.kwargs["reply_markup"]
        urls = [row[0].url for row in markup.inline_keyboard[:3]]
        self.assertTrue(any("source%3Dtelegram" in (url or "") for url in urls))
        self.assertTrue(any("source%3Dvk" in (url or "") for url in urls))
        self.assertTrue(any("source%3Dmax" in (url or "") for url in urls))

        stale = _callback()
        stale.data = "cpev:announce:broken"
        await events.create_event_announcement(stale)
        stale.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

        failed = _callback()
        failed.data = f"cpev:announce:{EVENT_TOKEN}:{TOKEN}"
        with (
            patch.object(events.control, "_token_uuid", side_effect=decode),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "draft_event_announcement", new=AsyncMock(side_effect=ValueError("bad"))),
        ):
            await events.create_event_announcement(failed)
        failed.answer.assert_awaited_once_with("Не удалось подготовить анонс", show_alert=True)

    async def test_join_target_start_covers_success_stale_and_permission_denial(self) -> None:
        state = AsyncMock()
        reply = SimpleNamespace(answer=AsyncMock())
        actor = MagicMock(unsafe=True)
        callback = _callback()
        callback.data = f"cpev:join:{EVENT_TOKEN}:{TOKEN}"

        def decode(value: str) -> str:
            return EVENT_ID if value == EVENT_TOKEN else BUSINESS_ID

        with (
            patch.object(events.control, "_token_uuid", side_effect=decode),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events.control, "_callback_message", return_value=reply),
            patch.object(events, "_cancel_keyboard", return_value="cancel"),
        ):
            await events.start_event_join_target(callback, state)
        actor.assert_can_manage_business.assert_called_once_with()
        state.clear.assert_awaited_once_with()
        state.set_state.assert_awaited_once_with(events.ClientPlatformEventState.waiting_join_url)
        state.update_data.assert_awaited_once_with(event_business_id=BUSINESS_ID, event_id=EVENT_ID)
        callback.answer.assert_awaited_once_with()
        self.assertIn("HTTPS-ссылку", reply.answer.await_args.args[0])

        stale = _callback()
        stale.data = "cpev:join:broken"
        await events.start_event_join_target(stale, AsyncMock())
        stale.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

        denied = _callback()
        denied.data = f"cpev:join:{EVENT_TOKEN}:{TOKEN}"
        denied_actor = MagicMock(unsafe=True)
        denied_actor.assert_can_manage_business.side_effect = TenantPermissionDenied("denied")
        with (
            patch.object(events.control, "_token_uuid", side_effect=decode),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=denied_actor)),
        ):
            await events.start_event_join_target(denied, AsyncMock())
        denied.answer.assert_awaited_once_with(
            "Изменить ссылку может владелец или администратор", show_alert=True
        )

    async def test_join_target_receive_covers_missing_invalid_and_success(self) -> None:
        missing = _message("https://stream.example/live")
        missing_state = AsyncMock()
        missing_state.get_data.return_value = {}
        await events.receive_event_join_target(missing, missing_state)
        missing_state.clear.assert_awaited_once_with()
        self.assertIn("Откройте вебинары заново", missing.answer.await_args.args[0])

        invalid = _message("http://unsafe.example/live")
        invalid_state = AsyncMock()
        invalid_state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "event_id": EVENT_ID,
        }
        actor = MagicMock(unsafe=True)
        with (
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "set_event_join_target", side_effect=ValueError("https required")),
            patch.object(events, "_cancel_keyboard", return_value="cancel"),
        ):
            await events.receive_event_join_target(invalid, invalid_state)
        invalid_state.clear.assert_not_awaited()
        self.assertIn("полный HTTPS-адрес", invalid.answer.await_args.args[0])

        success = _message("https://stream.example/live")
        success_state = AsyncMock()
        success_state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "event_id": EVENT_ID,
        }
        updated = SimpleNamespace(provider_key="external")
        with (
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "set_event_join_target", return_value=updated) as setter,
            patch.object(events.control, "_uuid_token", return_value=TOKEN),
            patch.object(events.control, "_keyboard", side_effect=lambda rows: rows),
        ):
            await events.receive_event_join_target(success, success_state)
        setter.assert_called_once_with(
            actor=actor, event_id=EVENT_ID, join_url="https://stream.example/live"
        )
        success_state.clear.assert_awaited_once_with()
        self.assertIn("Площадка: external", success.answer.await_args.args[0])
        self.assertEqual(
            success.answer.await_args.kwargs["reply_markup"],
            [[("🎥 К вебинарам", f"cpev:home:{TOKEN}")]],
        )


    def test_webinar_callback_payloads_fit_telegram_limit(self) -> None:
        samples = (
            f"cpev:content:{EVENT_TOKEN}:{TOKEN}",
            f"cpev:fp:{EVENT_TOKEN}:{TOKEN}",
            f"cpev:ws:{EVENT_TOKEN}:{TOKEN}",
            f"cpev:wd:{EVENT_TOKEN}:14:{TOKEN}",
            f"cpev:wo:{EVENT_TOKEN}:{TOKEN}",
            f"cpev:wt:{EVENT_TOKEN}:14:{TOKEN}",
            f"cpev:we:{EVENT_TOKEN}:14:{TOKEN}",
            f"cpev:wr:{EVENT_TOKEN}:14:{TOKEN}",
            f"cpev:announce:{EVENT_TOKEN}:{TOKEN}",
        )
        for payload in samples:
            self.assertLessEqual(len(payload.encode("utf-8")), 64, payload)

    def test_content_plan_helpers_cover_warmup_and_empty_states(self) -> None:
        item = SimpleNamespace(id=EVENT_ID, title="Вебинар")
        snapshot = SimpleNamespace(items=(SimpleNamespace(id="other"), item))
        self.assertIs(events._event_item(snapshot, EVENT_ID), item)
        with self.assertRaisesRegex(ValueError, "вебинар не найден"):
            events._event_item(snapshot, "missing")

        def token(value: str) -> str:
            return EVENT_TOKEN if value == EVENT_ID else TOKEN

        with patch.object(events.control, "_uuid_token", side_effect=token):
            warm = events._content_plan_rows(
                event_id=EVENT_ID,
                business_id=BUSINESS_ID,
                has_warmup=True,
            )
            empty = events._content_plan_rows(
                event_id=EVENT_ID,
                business_id=BUSINESS_ID,
                has_warmup=False,
            )
        self.assertIn("cpev:wt:", warm[0][0][1])
        self.assertIn("Изменить дни", warm[1][0][0])
        self.assertIn("Настроить сообщения до вебинара", empty[0][0][0])
        self.assertEqual(warm[-1][0][1], f"cpev:home:{TOKEN}")

    async def test_send_content_plan_renders_all_autosend_states(self) -> None:
        actor = MagicMock(unsafe=True)
        item = SimpleNamespace(id=EVENT_ID, title="Вебинар")
        modes = SimpleNamespace(
            warmup=EventContentMode.TEXT,
            event_day=EventContentMode.TEXT_WITH_IMAGE,
            post_event=EventContentMode.TEXT_IN_IMAGE,
        )
        scheduled = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
        draft1 = SimpleNamespace(
            publish_date=scheduled.date(),
            scheduled_at=scheduled,
            position=1,
            text="Первый",
            source="template",
        )
        draft2 = SimpleNamespace(
            publish_date=datetime(2026, 9, 21, tzinfo=timezone.utc).date(),
            scheduled_at=scheduled,
            position=2,
            text="Второй",
            source="owner",
        )

        def token(value: str) -> str:
            return EVENT_TOKEN if value == EVENT_ID else TOKEN

        cases = (
            (True, True, (draft1, draft2), "🟢 включены", "2 дн."),
            (True, False, (draft1,), "🟡 включены", "1 дн."),
            (False, False, (), "⚪️ выключены", "не настроен"),
        )
        for enabled, effective, drafts, status, warmup_text in cases:
            target = SimpleNamespace(answer=AsyncMock())
            snapshot = SimpleNamespace(
                items=(item,),
                commercial_followups_enabled=enabled,
                commercial_followups_effective=effective,
            )
            plan = SimpleNamespace(
                drafts=drafts,
                requested_days=len(drafts),
                timezone_name="Europe/Moscow",
            )
            with (
                patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
                patch.object(events, "resolve_cockpit_events", return_value=snapshot),
                patch.object(events, "get_saved_event_warmup_plan", return_value=plan),
                patch.object(events, "get_event_content_plan", return_value=modes),
                patch.object(events.control, "_uuid_token", side_effect=token),
                patch.object(events.control, "_keyboard", side_effect=lambda rows: rows),
            ):
                await events._send_event_content_plan(
                    target,
                    user_id=101,
                    business_id=BUSINESS_ID,
                    event_id=EVENT_ID,
                )
            text = target.answer.await_args.args[0]
            self.assertIn(status, text)
            self.assertIn(warmup_text, text)
            self.assertIn("24 часа, 3 часа и 15 минут", text)
        self.assertGreaterEqual(actor.assert_can_manage_business.call_count, 3)

    async def test_warmup_preview_covers_navigation_owner_and_empty_plan(self) -> None:
        actor = MagicMock(unsafe=True)
        target = SimpleNamespace(answer=AsyncMock())
        empty = SimpleNamespace(drafts=(), requested_days=0, timezone_name="Europe/Moscow")
        with (
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "get_saved_event_warmup_plan", return_value=empty),
            patch.object(events, "_send_event_content_plan", new=AsyncMock()) as fallback,
        ):
            await events._send_warmup_preview(
                target,
                user_id=101,
                business_id=BUSINESS_ID,
                event_id=EVENT_ID,
                position=1,
            )
        fallback.assert_awaited_once()

        drafts = tuple(
            SimpleNamespace(
                position=index,
                publish_date=datetime(2026, 9, 19 + index, tzinfo=timezone.utc).date(),
                scheduled_at=datetime(2026, 9, 19 + index, 9, tzinfo=timezone.utc),
                text=f"Текст {index}",
                source="owner" if index == 2 else "template",
                slot_key=f"before:{4 - index}",
            )
            for index in (1, 2, 3)
        )
        plan = SimpleNamespace(
            drafts=drafts,
            requested_days=3,
            timezone_name="Europe/Moscow",
        )

        def token(value: str) -> str:
            return EVENT_TOKEN if value == EVENT_ID else TOKEN

        for position, expected_source in ((1, "Автотекст"), (2, "Ваш текст"), (99, "Автотекст")):
            target = SimpleNamespace(answer=AsyncMock())
            with (
                patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
                patch.object(events, "get_saved_event_warmup_plan", return_value=plan),
                patch.object(
                    events,
                    "get_event_content_plan",
                    return_value=SimpleNamespace(warmup=EventContentMode.TEXT),
                ),
                patch.object(events, "get_event_content_asset", return_value=None),
                patch.object(events.control, "_uuid_token", side_effect=token),
                patch.object(events.control, "_keyboard", side_effect=lambda rows: rows),
            ):
                await events._send_warmup_preview(
                    target,
                    user_id=101,
                    business_id=BUSINESS_ID,
                    event_id=EVENT_ID,
                    position=position,
                )
            text = target.answer.await_args.args[0]
            rows = target.answer.await_args.kwargs["reply_markup"]
            self.assertIn(expected_source, text)
            callbacks = [callback for row in rows for _label, callback in row]
            self.assertTrue(any(callback.startswith("cpev:we:") for callback in callbacks))
            if position == 2:
                self.assertTrue(any(callback.startswith("cpev:wr:") for callback in callbacks))
                self.assertTrue(any(label == "⬅️" for row in rows for label, _ in row))
                self.assertTrue(any(label == "➡️" for row in rows for label, _ in row))

    async def test_followup_plan_uses_canonical_templates_and_navigation(self) -> None:
        target = SimpleNamespace(answer=AsyncMock())
        actor = MagicMock(unsafe=True)
        snapshot = SimpleNamespace(items=(SimpleNamespace(id=EVENT_ID, title="Вебинар"),))
        previews = (
            SimpleNamespace(
                segment="no_show",
                segment_label="Не пришли",
                stage=1,
                offset_label="+1 час",
                text="{name}, про «{title}»: {offer}",
                source="template",
                slot_key="no_show:1",
            ),
            SimpleNamespace(
                segment="no_show",
                segment_label="Не пришли",
                stage=2,
                offset_label="+24 часа",
                text="Ещё раз {offer}",
                source="template",
                slot_key="no_show:2",
            ),
            SimpleNamespace(
                segment="attended_unpaid",
                segment_label="Были",
                stage=1,
                offset_label="+1 час",
                text="{name}: {offer}",
                source="owner",
                slot_key="attended_unpaid:1",
            ),
        )

        def token(value: str) -> str:
            return EVENT_TOKEN if value == EVENT_ID else TOKEN

        with (
            patch.object(events, "resolve_cockpit_events", return_value=snapshot),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "get_event_followup_content_plan", return_value=previews),
            patch.object(
                events,
                "get_event_content_plan",
                return_value=SimpleNamespace(post_event=EventContentMode.TEXT),
            ),
            patch.object(events, "get_event_content_asset", return_value=None),
            patch.object(events.control, "_uuid_token", side_effect=token),
            patch.object(events.control, "_keyboard", side_effect=lambda rows: rows),
        ):
            await events._send_followup_plan(
                target,
                user_id=101,
                business_id=BUSINESS_ID,
                event_id=EVENT_ID,
            )
        text = target.answer.await_args.args[0]
        self.assertIn("Дожим 1/3", text)
        self.assertIn("Группа: Не пришли", text)
        self.assertIn("Источник: автотекст", text)
        self.assertIn("Имя", text)
        self.assertIn("[ссылка на предложение]", text)
        rows = target.answer.await_args.kwargs["reply_markup"]
        callbacks = [callback for row in rows for _label, callback in row]
        self.assertIn(f"cpev:fp:{EVENT_TOKEN}:1:{TOKEN}", callbacks)
        self.assertIn(f"cpev:fe:{EVENT_TOKEN}:0:{TOKEN}", callbacks)
        self.assertIn(f"cpev:content:{EVENT_TOKEN}:{TOKEN}", callbacks)

    async def test_content_plan_callbacks_cover_stale_and_success_paths(self) -> None:
        reply = SimpleNamespace(answer=AsyncMock())

        def decode(value: str) -> str:
            return EVENT_ID if value == EVENT_TOKEN else BUSINESS_ID

        stale = _callback()
        stale.data = "cpev:content:broken"
        await events.open_event_content_plan(stale)
        stale.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

        callback = _callback()
        callback.data = f"cpev:content:{EVENT_TOKEN}:{TOKEN}"
        with (
            patch.object(events.control, "_token_uuid", side_effect=decode),
            patch.object(events.control, "_callback_message", return_value=reply),
            patch.object(events, "_send_event_content_plan", new=AsyncMock()) as send,
        ):
            await events.open_event_content_plan(callback)
        callback.answer.assert_awaited_once_with()
        send.assert_awaited_once_with(
            reply,
            user_id=101,
            business_id=BUSINESS_ID,
            event_id=EVENT_ID,
        )

        stale_follow = _callback()
        stale_follow.data = "cpev:fp:broken"
        await events.open_followup_content_plan(stale_follow)
        stale_follow.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

        follow = _callback()
        follow.data = f"cpev:fp:{EVENT_TOKEN}:{TOKEN}"
        with (
            patch.object(events.control, "_token_uuid", side_effect=decode),
            patch.object(events.control, "_callback_message", return_value=reply),
            patch.object(events, "_send_followup_plan", new=AsyncMock()) as send_follow,
        ):
            await events.open_followup_content_plan(follow)
        follow.answer.assert_awaited_once_with()
        send_follow.assert_awaited_once_with(
            reply,
            user_id=101,
            business_id=BUSINESS_ID,
            event_id=EVENT_ID,
            index=0,
        )

    async def test_followup_text_edit_cancel_reset_and_success_paths(self) -> None:
        actor = MagicMock(unsafe=True)
        preview = SimpleNamespace(
            segment="attended_unpaid",
            segment_label="Были",
            stage=2,
            offset_label="+24 часа",
            text="Шаблон {offer}",
            source="owner",
        )

        def decode(value: str) -> str:
            return EVENT_ID if value == EVENT_TOKEN else BUSINESS_ID

        edit = _callback()
        edit.data = f"cpev:fe:{EVENT_TOKEN}:0:{TOKEN}"
        edit_state = AsyncMock()
        reply = SimpleNamespace(answer=AsyncMock())
        with (
            patch.object(events.control, "_token_uuid", side_effect=decode),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "get_event_followup_content_plan", return_value=(preview,)),
            patch.object(events.control, "_callback_message", return_value=reply),
        ):
            await events.edit_followup_text(edit, edit_state)
        edit_state.set_state.assert_awaited_once_with(
            events.ClientPlatformEventState.waiting_followup_text
        )
        edit_state.update_data.assert_awaited_once_with(
            event_business_id=BUSINESS_ID,
            content_event_id=EVENT_ID,
            followup_index=0,
            followup_segment="attended_unpaid",
            followup_stage=2,
        )
        self.assertIn("Пришлите новый текст дожима", reply.answer.await_args.args[0])

        cancel = _message("Отмена")
        cancel_state = AsyncMock()
        cancel_state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "content_event_id": EVENT_ID,
            "followup_index": 0,
            "followup_segment": "attended_unpaid",
            "followup_stage": 2,
        }
        with patch.object(events, "_send_followup_plan", new=AsyncMock()) as send:
            await events.receive_followup_text(cancel, cancel_state)
        cancel_state.clear.assert_awaited_once_with()
        send.assert_awaited_once_with(
            cancel,
            user_id=101,
            business_id=BUSINESS_ID,
            event_id=EVENT_ID,
            index=0,
        )

        saved = _message("Мой собственный дожим для {name}: {offer}")
        saved_state = AsyncMock()
        saved_state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "content_event_id": EVENT_ID,
            "followup_index": 0,
            "followup_segment": "attended_unpaid",
            "followup_stage": 2,
        }
        with (
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "set_event_followup_text") as setter,
            patch.object(events, "_send_followup_plan", new=AsyncMock()) as send,
        ):
            await events.receive_followup_text(saved, saved_state)
        setter.assert_called_once_with(
            actor=actor,
            event_id=EVENT_ID,
            segment="attended_unpaid",
            stage=2,
            text="Мой собственный дожим для {name}: {offer}",
        )
        saved_state.clear.assert_awaited_once_with()
        send.assert_awaited_once_with(
            saved,
            user_id=101,
            business_id=BUSINESS_ID,
            event_id=EVENT_ID,
            index=0,
        )

        reset = _callback()
        reset.data = f"cpev:fr:{EVENT_TOKEN}:0:{TOKEN}"
        with (
            patch.object(events.control, "_token_uuid", side_effect=decode),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "get_event_followup_content_plan", return_value=(preview,)),
            patch.object(events, "reset_event_followup_text") as resetter,
            patch.object(events, "_send_followup_plan", new=AsyncMock()) as send,
            patch.object(events.control, "_callback_message", return_value=reply),
        ):
            await events.reset_followup_text(reset)
        resetter.assert_called_once_with(
            actor=actor,
            event_id=EVENT_ID,
            segment="attended_unpaid",
            stage=2,
        )
        reset.answer.assert_awaited_once_with("Автотекст восстановлен")
        send.assert_awaited_once_with(
            reply,
            user_id=101,
            business_id=BUSINESS_ID,
            event_id=EVENT_ID,
            index=0,
        )


    async def test_followup_plan_empty_and_owner_navigation_edges(self) -> None:
        target = SimpleNamespace(answer=AsyncMock())
        actor = MagicMock(unsafe=True)
        snapshot = SimpleNamespace(items=(SimpleNamespace(id=EVENT_ID, title="Вебинар"),))

        with (
            patch.object(events, "resolve_cockpit_events", return_value=snapshot),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "get_event_followup_content_plan", return_value=()),
            patch.object(
                events,
                "get_event_content_plan",
                return_value=SimpleNamespace(post_event=EventContentMode.TEXT),
            ),
        ):
            await events._send_followup_plan(
                target,
                user_id=101,
                business_id=BUSINESS_ID,
                event_id=EVENT_ID,
            )
        target.answer.assert_awaited_once_with("Для этого вебинара нет сообщений дожима.")

        target.answer.reset_mock()
        previews = (
            SimpleNamespace(
                segment="no_show",
                segment_label="Не пришли",
                stage=1,
                offset_label="+1 час",
                text="Первый {offer}",
                source="template",
                slot_key="no_show:1",
            ),
            SimpleNamespace(
                segment="attended_unpaid",
                segment_label="Были",
                stage=3,
                offset_label="+48 часов",
                text="{name}, последний {offer}",
                source="owner",
                slot_key="attended_unpaid:3",
            ),
        )

        def token(value: str) -> str:
            return EVENT_TOKEN if value == EVENT_ID else TOKEN

        with (
            patch.object(events, "resolve_cockpit_events", return_value=snapshot),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "get_event_followup_content_plan", return_value=previews),
            patch.object(
                events,
                "get_event_content_plan",
                return_value=SimpleNamespace(post_event=EventContentMode.TEXT),
            ),
            patch.object(events, "get_event_content_asset", return_value=None),
            patch.object(events.control, "_uuid_token", side_effect=token),
            patch.object(events.control, "_keyboard", side_effect=lambda rows: rows),
        ):
            await events._send_followup_plan(
                target,
                user_id=101,
                business_id=BUSINESS_ID,
                event_id=EVENT_ID,
                index=99,
            )
        text = target.answer.await_args.args[0]
        self.assertIn("Дожим 2/2", text)
        self.assertIn("Источник: ваш текст", text)
        callbacks = [
            callback
            for row in target.answer.await_args.kwargs["reply_markup"]
            for _label, callback in row
        ]
        self.assertIn(f"cpev:fp:{EVENT_TOKEN}:0:{TOKEN}", callbacks)
        self.assertIn(f"cpev:fr:{EVENT_TOKEN}:1:{TOKEN}", callbacks)
        self.assertFalse(any(callback == f"cpev:fp:{EVENT_TOKEN}:2:{TOKEN}" for callback in callbacks))

    async def test_followup_callbacks_cover_stale_range_and_error_paths(self) -> None:
        actor = MagicMock(unsafe=True)
        preview = SimpleNamespace(
            segment="no_show",
            segment_label="Не пришли",
            stage=1,
            offset_label="+1 час",
            text="Автотекст {offer}",
            source="template",
        )

        def decode(value: str) -> str:
            return EVENT_ID if value == EVENT_TOKEN else BUSINESS_ID

        bad_page = _callback()
        bad_page.data = f"cpev:fp:{EVENT_TOKEN}:oops:{TOKEN}"
        await events.open_followup_content_plan(bad_page)
        bad_page.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

        bad_edit = _callback()
        bad_edit.data = "cpev:fe:broken"
        await events.edit_followup_text(bad_edit, AsyncMock())
        bad_edit.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

        out_of_range = _callback()
        out_of_range.data = f"cpev:fe:{EVENT_TOKEN}:9:{TOKEN}"
        with (
            patch.object(events.control, "_token_uuid", side_effect=decode),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "get_event_followup_content_plan", return_value=(preview,)),
        ):
            await events.edit_followup_text(out_of_range, AsyncMock())
        out_of_range.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

        state = AsyncMock()
        state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "content_event_id": EVENT_ID,
            "followup_index": 0,
            "followup_segment": "no_show",
            "followup_stage": 1,
        }
        too_long = _message("x" * 3501)
        await events.receive_followup_text(too_long, state)
        too_long.answer.assert_awaited_once_with("Текст должен быть от 1 до 3500 символов.")

        rejected = _message("Мой текст")
        with (
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "set_event_followup_text", side_effect=ValueError("ошибка текста")),
        ):
            await events.receive_followup_text(rejected, state)
        rejected.answer.assert_awaited_once_with("ошибка текста")

        bad_reset = _callback()
        bad_reset.data = "cpev:fr:broken"
        await events.reset_followup_text(bad_reset)
        bad_reset.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

        reset_range = _callback()
        reset_range.data = f"cpev:fr:{EVENT_TOKEN}:9:{TOKEN}"
        with (
            patch.object(events.control, "_token_uuid", side_effect=decode),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "get_event_followup_content_plan", return_value=(preview,)),
        ):
            await events.reset_followup_text(reset_range)
        reset_range.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

        reset_error = _callback()
        reset_error.data = f"cpev:fr:{EVENT_TOKEN}:0:{TOKEN}"
        with (
            patch.object(events.control, "_token_uuid", side_effect=decode),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "get_event_followup_content_plan", return_value=(preview,)),
            patch.object(events, "reset_event_followup_text", side_effect=ValueError("ошибка сброса")),
        ):
            await events.reset_followup_text(reset_error)
        reset_error.answer.assert_awaited_once_with("ошибка сброса", show_alert=True)


    async def test_warmup_setup_covers_quick_custom_and_zero_windows(self) -> None:
        actor = MagicMock(unsafe=True)

        def decode(value: str) -> str:
            return EVENT_ID if value == EVENT_TOKEN else BUSINESS_ID

        def token(value: str) -> str:
            return EVENT_TOKEN if value == EVENT_ID else TOKEN

        for maximum in (6, 0):
            callback = _callback()
            callback.data = f"cpev:ws:{EVENT_TOKEN}:{TOKEN}"
            state = AsyncMock()
            reply = SimpleNamespace(answer=AsyncMock())
            with (
                patch.object(events.control, "_token_uuid", side_effect=decode),
                patch.object(events.control, "_uuid_token", side_effect=token),
                patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
                patch.object(
                    events,
                    "get_event_warmup_window",
                    return_value=SimpleNamespace(max_warmup_days=maximum),
                ),
                patch.object(events.control, "_callback_message", return_value=reply),
                patch.object(events.control, "_keyboard", side_effect=lambda rows: rows),
            ):
                await events.open_warmup_setup(callback, state)
            rows = reply.answer.await_args.kwargs["reply_markup"]
            labels = [label for row in rows for label, _ in row]
            self.assertIn("0", labels)
            if maximum:
                self.assertIn("6", labels)
                self.assertIn("✍️ Другое число", labels)
            else:
                self.assertNotIn("✍️ Другое число", labels)
            state.clear.assert_awaited_once_with()
            callback.answer.assert_awaited_once_with()

        stale = _callback()
        stale.data = "cpev:ws:broken"
        await events.open_warmup_setup(stale, AsyncMock())
        stale.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

    async def test_set_warmup_days_covers_stale_validation_and_success(self) -> None:
        actor = MagicMock(unsafe=True)
        reply = SimpleNamespace(answer=AsyncMock())

        def decode(value: str) -> str:
            return EVENT_ID if value == EVENT_TOKEN else BUSINESS_ID

        stale = _callback()
        stale.data = f"cpev:wd:{EVENT_TOKEN}:x:{TOKEN}"
        await events.set_warmup_days(stale, AsyncMock())
        stale.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

        invalid = _callback()
        invalid.data = f"cpev:wd:{EVENT_TOKEN}:9:{TOKEN}"
        with (
            patch.object(events.control, "_token_uuid", side_effect=decode),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "save_event_warmup_plan", side_effect=ValueError("слишком много")),
        ):
            await events.set_warmup_days(invalid, AsyncMock())
        invalid.answer.assert_awaited_once_with("слишком много", show_alert=True)

        callback = _callback()
        callback.data = f"cpev:wd:{EVENT_TOKEN}:3:{TOKEN}"
        state = AsyncMock()
        with (
            patch.object(events.control, "_token_uuid", side_effect=decode),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "save_event_warmup_plan") as save,
            patch.object(events.control, "_callback_message", return_value=reply),
            patch.object(events, "_send_event_content_plan", new=AsyncMock()) as refresh,
        ):
            await events.set_warmup_days(callback, state)
        save.assert_called_once_with(actor=actor, event_id=EVENT_ID, requested_days=3)
        state.clear.assert_awaited_once_with()
        callback.answer.assert_awaited_once_with("Сообщения до вебинара сохранены")
        refresh.assert_awaited_once()

    async def test_custom_warmup_days_input_covers_cancel_invalid_and_success(self) -> None:
        actor = MagicMock(unsafe=True)
        reply = SimpleNamespace(answer=AsyncMock())

        def decode(value: str) -> str:
            return EVENT_ID if value == EVENT_TOKEN else BUSINESS_ID

        stale = _callback()
        stale.data = "cpev:wo:broken"
        await events.request_custom_warmup_days(stale, AsyncMock())
        stale.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

        callback = _callback()
        callback.data = f"cpev:wo:{EVENT_TOKEN}:{TOKEN}"
        state = AsyncMock()
        with (
            patch.object(events.control, "_token_uuid", side_effect=decode),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(
                events,
                "get_event_warmup_window",
                return_value=SimpleNamespace(max_warmup_days=8),
            ),
            patch.object(events.control, "_callback_message", return_value=reply),
        ):
            await events.request_custom_warmup_days(callback, state)
        state.set_state.assert_awaited_once_with(events.ClientPlatformEventState.waiting_warmup_days)
        state.update_data.assert_awaited_once_with(
            event_business_id=BUSINESS_ID,
            content_event_id=EVENT_ID,
            max_warmup_days=8,
        )

        cancel = _message("Отмена")
        cancel_state = AsyncMock()
        cancel_state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "content_event_id": EVENT_ID,
            "max_warmup_days": 8,
        }
        with patch.object(events, "_send_event_content_plan", new=AsyncMock()) as send:
            await events.receive_custom_warmup_days(cancel, cancel_state)
        cancel_state.clear.assert_awaited_once_with()
        send.assert_awaited_once()

        invalid = _message("девять")
        invalid_state = AsyncMock()
        invalid_state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "content_event_id": EVENT_ID,
            "max_warmup_days": 8,
        }
        await events.receive_custom_warmup_days(invalid, invalid_state)
        self.assertIn("Нужно число", invalid.answer.await_args.args[0])
        invalid_state.clear.assert_not_awaited()

        success = _message("6")
        success_state = AsyncMock()
        success_state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "content_event_id": EVENT_ID,
            "max_warmup_days": 8,
        }
        with (
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "save_event_warmup_plan") as save,
            patch.object(events, "_send_event_content_plan", new=AsyncMock()) as send,
        ):
            await events.receive_custom_warmup_days(success, success_state)
        save.assert_called_once_with(actor=actor, event_id=EVENT_ID, requested_days=6)
        success_state.clear.assert_awaited_once_with()
        send.assert_awaited_once()

    async def test_warmup_text_callbacks_cover_edit_cancel_validation_reset_and_success(self) -> None:
        actor = MagicMock(unsafe=True)
        reply = SimpleNamespace(answer=AsyncMock())

        def decode(value: str) -> str:
            return EVENT_ID if value == EVENT_TOKEN else BUSINESS_ID

        stale_open = _callback()
        stale_open.data = f"cpev:wt:{EVENT_TOKEN}:x:{TOKEN}"
        await events.open_warmup_text(stale_open)
        stale_open.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

        opened = _callback()
        opened.data = f"cpev:wt:{EVENT_TOKEN}:2:{TOKEN}"
        with (
            patch.object(events.control, "_token_uuid", side_effect=decode),
            patch.object(events.control, "_callback_message", return_value=reply),
            patch.object(events, "_send_warmup_preview", new=AsyncMock()) as preview,
        ):
            await events.open_warmup_text(opened)
        preview.assert_awaited_once_with(
            reply,
            user_id=101,
            business_id=BUSINESS_ID,
            event_id=EVENT_ID,
            position=2,
        )

        stale_edit = _callback()
        stale_edit.data = f"cpev:we:{EVENT_TOKEN}:x:{TOKEN}"
        await events.edit_warmup_text(stale_edit, AsyncMock())
        stale_edit.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

        edit = _callback()
        edit.data = f"cpev:we:{EVENT_TOKEN}:2:{TOKEN}"
        edit_state = AsyncMock()
        with (
            patch.object(events.control, "_token_uuid", side_effect=decode),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events.control, "_callback_message", return_value=reply),
        ):
            await events.edit_warmup_text(edit, edit_state)
        actor.assert_can_manage_business.assert_called()
        edit_state.set_state.assert_awaited_once_with(events.ClientPlatformEventState.waiting_content_text)
        edit_state.update_data.assert_awaited_once_with(
            event_business_id=BUSINESS_ID,
            content_event_id=EVENT_ID,
            content_position=2,
        )

        cancel = _message("Отмена")
        cancel_state = AsyncMock()
        cancel_state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "content_event_id": EVENT_ID,
            "content_position": 2,
        }
        with patch.object(events, "_send_warmup_preview", new=AsyncMock()) as preview:
            await events.receive_warmup_text(cancel, cancel_state)
        cancel_state.clear.assert_awaited_once_with()
        preview.assert_awaited_once()

        invalid = _message("")
        invalid_state = AsyncMock()
        invalid_state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "content_event_id": EVENT_ID,
            "content_position": 2,
        }
        await events.receive_warmup_text(invalid, invalid_state)
        self.assertIn("от 1 до 3500", invalid.answer.await_args.args[0])

        rejected = _message("Мой текст")
        rejected_state = AsyncMock()
        rejected_state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "content_event_id": EVENT_ID,
            "content_position": 2,
        }
        with (
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "set_event_warmup_text", side_effect=ValueError("нет плана")),
        ):
            await events.receive_warmup_text(rejected, rejected_state)
        self.assertEqual(rejected.answer.await_args.args[0], "нет плана")
        rejected_state.clear.assert_not_awaited()

        saved = _message("Полностью свой текст {name}")
        saved_state = AsyncMock()
        saved_state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "content_event_id": EVENT_ID,
            "content_position": 2,
        }
        with (
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "set_event_warmup_text") as setter,
            patch.object(events, "_send_warmup_preview", new=AsyncMock()) as preview,
        ):
            await events.receive_warmup_text(saved, saved_state)
        setter.assert_called_once_with(
            actor=actor,
            event_id=EVENT_ID,
            position=2,
            text="Полностью свой текст {name}",
        )
        saved_state.clear.assert_awaited_once_with()
        preview.assert_awaited_once()

        stale_reset = _callback()
        stale_reset.data = f"cpev:wr:{EVENT_TOKEN}:x:{TOKEN}"
        await events.reset_warmup_text(stale_reset)
        stale_reset.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

        reset_error = _callback()
        reset_error.data = f"cpev:wr:{EVENT_TOKEN}:2:{TOKEN}"
        with (
            patch.object(events.control, "_token_uuid", side_effect=decode),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "reset_event_warmup_text", side_effect=ValueError("нет текста")),
        ):
            await events.reset_warmup_text(reset_error)
        reset_error.answer.assert_awaited_once_with("нет текста", show_alert=True)

        reset = _callback()
        reset.data = f"cpev:wr:{EVENT_TOKEN}:2:{TOKEN}"
        with (
            patch.object(events.control, "_token_uuid", side_effect=decode),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "reset_event_warmup_text") as resetter,
            patch.object(events.control, "_callback_message", return_value=reply),
            patch.object(events, "_send_warmup_preview", new=AsyncMock()) as preview,
        ):
            await events.reset_warmup_text(reset)
        resetter.assert_called_once_with(actor=actor, event_id=EVENT_ID, position=2)
        reset.answer.assert_awaited_once_with("Автотекст восстановлен")
        preview.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
