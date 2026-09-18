from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from handlers import clientplatform_event_lifecycle as lifecycle


BUSINESS_ID = "11111111-1111-4111-8111-111111111111"
EVENT_ID = "33333333-3333-4333-8333-333333333333"
TOKEN = "ERERERERQRGBEREREREREQ"


def _message(text: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
    )


def _reply() -> SimpleNamespace:
    return SimpleNamespace(answer=AsyncMock(), edit_reply_markup=AsyncMock())


def _callback(data: str) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
    )


def _session(position: int = 1, *, join_url: str | None = "https://room.example/live"):
    return lifecycle.EventWizardSession(
        position=position,
        starts_at=datetime(2026, 9, 25 + position - 1, 16, tzinfo=timezone.utc),
        ends_at=datetime(2026, 9, 25 + position - 1, 18, tzinfo=timezone.utc),
        local_label=f"{24 + position:02d}.09.2026 19:00–21:00",
        join_url=join_url,
    )


def _session_payload(position: int = 1, *, join_url: str | None = "https://room.example/live"):
    item = _session(position, join_url=join_url)
    return lifecycle._session_payload(item, join_url=item.join_url)


def _labels(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


class EventLifecycleExtendedHandlerTests(unittest.IsolatedAsyncioTestCase):
    def test_keyboard_helpers_cover_owner_choices(self) -> None:
        with patch.object(lifecycle.control, "_uuid_token", return_value="tok"):
            timezone_markup = lifecycle._timezone_keyboard(BUSINESS_ID)
            venue_markup = lifecycle._venue_keyboard(BUSINESS_ID)
            calendar_markup = lifecycle._calendar_keyboard(
                business_id=BUSINESS_ID,
                timezone_name="Europe/Moscow",
                year=2026,
                month=9,
                minimum_date=date(2026, 9, 18),
            )
            future_calendar = lifecycle._calendar_keyboard(
                business_id=BUSINESS_ID,
                timezone_name="Europe/Moscow",
                year=2027,
                month=3,
                minimum_date=date(2026, 9, 18),
            )
            start_markup = lifecycle._start_time_keyboard(BUSINESS_ID)
            duration_markup = lifecycle._duration_keyboard(BUSINESS_ID)
            telemost_markup = lifecycle._session_url_keyboard(
                business_id=BUSINESS_ID,
                venue_key="telemost",
            )
            other_markup = lifecycle._session_url_keyboard(
                business_id=BUSINESS_ID,
                venue_key="other",
            )

        self.assertIn("🕒 Москва", _labels(timezone_markup))
        self.assertIn("Яндекс Телемост", _labels(venue_markup))
        self.assertIn("✍️ Ввести вручную", _labels(calendar_markup))
        self.assertIn("‹", _labels(future_calendar))
        self.assertIn("19:00", _labels(start_markup))
        self.assertIn("2 часа", _labels(duration_markup))
        self.assertIn("↗️ Открыть Яндекс Телемост", _labels(telemost_markup))
        self.assertNotIn("↗️ Открыть Другой сервис", _labels(other_markup))

    def test_payload_action_and_public_url_helpers(self) -> None:
        with patch.object(lifecycle.control, "_uuid_token", side_effect=lambda value: value[:6]):
            rows = lifecycle._event_actions(
                event_id=EVENT_ID,
                business_id=BUSINESS_ID,
                join_ready=False,
                visual_requested=True,
            )
            ready_rows = lifecycle._event_actions(
                event_id=EVENT_ID,
                business_id=BUSINESS_ID,
                join_ready=True,
            )
        labels = [label for row in rows for label, _ in row]
        ready_labels = [label for row in ready_rows for label, _ in row]
        self.assertIn("🔗 Добавить ссылку на эфир", labels)
        self.assertIn("🎨 Картинки и креативы", labels)
        self.assertNotIn("🔗 Добавить ссылку на эфир", ready_labels)

        item = _session(join_url=None)
        payload = lifecycle._session_payload(item, join_url=None)
        restored = lifecycle._session_from_payload(payload)
        self.assertEqual(restored.position, 1)
        self.assertIsNone(restored.join_url)
        self.assertEqual(lifecycle._configured_sessions({"event_sessions": [payload]}), (restored,))
        with self.assertRaises(ValueError):
            lifecycle._session_from_payload("bad")
        with self.assertRaises(ValueError):
            lifecycle._configured_sessions({"event_sessions": "bad"})
        self.assertIn("3) Текст в тематической картинке", lifecycle._mode_prompt("анонс"))

        with patch.object(lifecycle.settings, "MESSENGER_PUBLIC_BASE_URL", "https://cp.example/"):
            self.assertEqual(lifecycle._public_base_url(), "https://cp.example")
        with patch.object(lifecycle.settings, "MESSENGER_PUBLIC_BASE_URL", "http://cp.example"):
            with self.assertRaises(ValueError):
                lifecycle._public_base_url()

    async def test_prompt_helpers_cover_manual_and_picker_paths(self) -> None:
        message = _message()
        state = AsyncMock()
        state.get_data.return_value = {"event_sessions": []}
        with patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"):
            await lifecycle._prompt_session_time(
                message,
                state,
                business_id=BUSINESS_ID,
                position=1,
                total=2,
                timezone_name="Europe/Moscow",
            )
            await lifecycle._prompt_venue(
                message,
                state,
                business_id=BUSINESS_ID,
                timezone_name="Europe/Moscow",
            )
            with patch.object(lifecycle, "local_today", return_value=date(2026, 9, 18)):
                await lifecycle._prompt_session_date(
                    message,
                    state,
                    business_id=BUSINESS_ID,
                    position=1,
                    total=2,
                    timezone_name="Europe/Moscow",
                )
            await lifecycle._prompt_session_url(
                message,
                state,
                business_id=BUSINESS_ID,
                position=1,
                total=1,
                venue_key="telemost",
            )
            await lifecycle._prompt_session_url(
                message,
                state,
                business_id=BUSINESS_ID,
                position=2,
                total=2,
                venue_key="other",
            )
        self.assertGreaterEqual(message.answer.await_count, 5)

    async def test_finish_content_setup_missing_and_visual(self) -> None:
        missing = AsyncMock()
        missing.get_data.return_value = {}
        missing_message = _message()
        await lifecycle._finish_content_setup(missing_message, missing)
        missing.clear.assert_awaited_once()

        complete = AsyncMock()
        complete.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "created_event_id": EVENT_ID,
            "created_title": "Большой вебинар",
            "created_local_times": ["25.09 19:00", "26.09 19:00"],
            "created_registration_url": "https://cp.example/e/public",
            "created_join_ready": True,
            "warmup_requested_days": 2,
            "warmup_drafts": [
                {"position": 1, "publish_date": "23.09.2026", "text": "Первый прогрев"},
                {"position": 2, "publish_date": "24.09.2026", "text": "Второй прогрев"},
            ],
            "warmup_mode": lifecycle.EventContentMode.TEXT_WITH_IMAGE.value,
            "event_day_mode": lifecycle.EventContentMode.TEXT.value,
            "post_event_mode": lifecycle.EventContentMode.TEXT_IN_IMAGE.value,
        }
        message = _message()
        with (
            patch.object(lifecycle.control, "_uuid_token", return_value="tok"),
            patch.object(lifecycle.control, "_keyboard", return_value="actions"),
        ):
            await lifecycle._finish_content_setup(message, complete)
        complete.clear.assert_awaited_once()
        self.assertEqual(message.answer.await_count, 3)
        self.assertIn("общий генератор", message.answer.await_args_list[0].args[0])

    async def test_start_title_days_and_timezone_paths(self) -> None:
        denied = _callback(f"cpev:new:{TOKEN}")
        actor = MagicMock(unsafe=True)
        actor.assert_can_manage_business.side_effect = lifecycle.TenantPermissionDenied("denied")
        with (
            patch.object(lifecycle.control, "_token_uuid", return_value=BUSINESS_ID),
            patch.object(lifecycle.control, "_actor", new=AsyncMock(return_value=actor)),
        ):
            await lifecycle.start_multisession_event_wizard(denied, AsyncMock())
        self.assertTrue(denied.answer.await_args.kwargs["show_alert"])

        empty_state = AsyncMock()
        empty_state.get_data.return_value = {}
        empty_message = _message("Название")
        await lifecycle.receive_title(empty_message, empty_state)
        empty_state.clear.assert_awaited_once()

        blank_state = AsyncMock()
        blank_state.get_data.return_value = {"event_business_id": BUSINESS_ID}
        blank_message = _message("   ")
        with patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"):
            await lifecycle.receive_title(blank_message, blank_state)
        self.assertIn("Введите название", blank_message.answer.await_args.args[0])

        cancel_state = AsyncMock()
        cancel_state.get_data.return_value = {"event_business_id": BUSINESS_ID}
        with patch.object(lifecycle, "_cancel", new=AsyncMock()) as cancel:
            await lifecycle.receive_title(_message("отмена"), cancel_state)
        cancel.assert_awaited_once()

        good_state = AsyncMock()
        good_state.get_data.return_value = {"event_business_id": BUSINESS_ID}
        with patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"):
            await lifecycle.receive_title(_message(" Новый   вебинар "), good_state)
        good_state.update_data.assert_awaited_once_with(event_title="Новый вебинар")

        invalid_days_state = AsyncMock()
        invalid_days_state.get_data.return_value = {"event_business_id": BUSINESS_ID}
        invalid_days = _message("99")
        with patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"):
            await lifecycle.receive_days(invalid_days, invalid_days_state)
        self.assertIn("Введите число", invalid_days.answer.await_args.args[0])

        tz_state = AsyncMock()
        tz_state.get_data.return_value = {"event_business_id": BUSINESS_ID, "event_days": 2}
        tz_message = _message()
        with (
            patch.object(lifecycle, "normalize_event_timezone", side_effect=ValueError("bad")),
            patch.object(lifecycle, "_timezone_keyboard", return_value="tz"),
        ):
            await lifecycle._accept_timezone(tz_message, tz_state, "Mars/Olympus")
        self.assertIn("Не удалось определить", tz_message.answer.await_args.args[0])

        stale_state = AsyncMock()
        stale_state.get_data.return_value = {"event_days": 0}
        await lifecycle._accept_timezone(_message(), stale_state, "Москва")
        stale_state.clear.assert_awaited_once()

    async def test_timezone_and_venue_callbacks(self) -> None:
        state = AsyncMock()
        state.get_data.return_value = {"event_business_id": BUSINESS_ID, "event_days": 2}
        reply = _reply()
        callback = _callback("cpev:tz:moscow")
        with (
            patch.object(lifecycle.control, "_callback_message", return_value=reply),
            patch.object(lifecycle, "_accept_timezone", new=AsyncMock()) as accept,
        ):
            await lifecycle.choose_moscow_timezone(callback, state)
        accept.assert_awaited_once_with(reply, state, "Москва")

        callback = _callback("cpev:tz:other")
        with (
            patch.object(lifecycle.control, "_callback_message", return_value=reply),
            patch.object(lifecycle, "_timezone_keyboard", return_value="tz"),
        ):
            await lifecycle.choose_other_timezone(callback, state)
        self.assertIn("Europe/Amsterdam", reply.answer.await_args.args[0])

        venue_state = AsyncMock()
        venue_state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "event_timezone": "Europe/Moscow",
            "event_days": 2,
            "event_session_index": 1,
        }
        unknown = _callback("cpev:venue:missing")
        await lifecycle.choose_webinar_venue(unknown, venue_state)
        self.assertTrue(unknown.answer.await_args.kwargs["show_alert"])

        ucr = _callback("cpev:venue:ucr")
        await lifecycle.choose_webinar_venue(ucr, venue_state)
        self.assertIn("UCR", ucr.answer.await_args.args[0])

        telemost = _callback("cpev:venue:telemost")
        with (
            patch.object(lifecycle.control, "_callback_message", return_value=reply),
            patch.object(lifecycle, "_prompt_session_date", new=AsyncMock()) as prompt,
        ):
            await lifecycle.choose_webinar_venue(telemost, venue_state)
        prompt.assert_awaited_once()
        venue_state.update_data.assert_awaited_with(event_platform="telemost")

        stale = AsyncMock()
        stale.get_data.return_value = {"event_timezone": "Europe/Moscow", "event_days": 2}
        stale_callback = _callback("cpev:venue:zoom")
        await lifecycle.choose_webinar_venue(stale_callback, stale)
        self.assertIn("устарел", stale_callback.answer.await_args.args[0])

    async def test_calendar_callbacks_quick_time_and_duration(self) -> None:
        state = AsyncMock()
        state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "event_timezone": "Europe/Moscow",
            "event_days": 2,
            "event_session_index": 1,
            "event_platform": "telemost",
            "event_sessions": [],
            "event_picker_min_date": "2026-09-18",
            "event_picker_date": "2026-09-25",
            "event_picker_start": "19:00",
        }
        reply = _reply()
        with (
            patch.object(lifecycle.control, "_callback_message", return_value=reply),
            patch.object(lifecycle, "local_today", return_value=date(2026, 9, 18)),
        ):
            month_cb = _callback("cpev:month:202610")
            await lifecycle.choose_calendar_month(month_cb, state)
            reply.edit_reply_markup.assert_awaited()

            bad_month = _callback("cpev:month:nope")
            await lifecycle.choose_calendar_month(bad_month, state)
            self.assertTrue(bad_month.answer.await_args.kwargs["show_alert"])

            date_cb = _callback("cpev:date:2026-09-25")
            await lifecycle.choose_calendar_date(date_cb, state)
            self.assertIn("Во сколько", reply.answer.await_args.args[0])

            bad_date = _callback("cpev:date:2020-01-01")
            await lifecycle.choose_calendar_date(bad_date, state)
            self.assertTrue(bad_date.answer.await_args.kwargs["show_alert"])

            invalid_start = _callback("cpev:start:abc")
            await lifecycle.choose_session_start(invalid_start, state)
            self.assertTrue(invalid_start.answer.await_args.kwargs["show_alert"])

            unsupported_start = _callback("cpev:start:2359")
            await lifecycle.choose_session_start(unsupported_start, state)
            self.assertTrue(unsupported_start.answer.await_args.kwargs["show_alert"])

            valid_start = _callback("cpev:start:1900")
            await lifecycle.choose_session_start(valid_start, state)
            state.update_data.assert_any_await(event_picker_start="19:00")

            malformed = _callback("cpev:duration")
            await lifecycle.choose_session_duration(malformed, state)
            self.assertTrue(malformed.answer.await_args.kwargs["show_alert"])

            with patch.object(lifecycle, "_prompt_session_url", new=AsyncMock()) as prompt_url:
                valid = _callback("cpev:duration:120")
                await lifecycle.choose_session_duration(valid, state)
            prompt_url.assert_awaited_once()
            pending = state.update_data.await_args_list[-1].kwargs["pending_session"]
            self.assertEqual(pending["local_label"], "25.09.2026 19:00–21:00")

            manual = _callback("cpev:manual-time")
            with patch.object(lifecycle, "_prompt_session_time", new=AsyncMock()) as prompt_time:
                await lifecycle.choose_manual_session_time(manual, state)
            prompt_time.assert_awaited_once()

    async def test_session_time_and_room_progression(self) -> None:
        invalid_state = AsyncMock()
        invalid_state.get_data.return_value = {}
        await lifecycle.receive_session_time(
            _message("25.09.2026 19:00-21:00"),
            invalid_state,
        )
        invalid_state.clear.assert_awaited_once()

        state = AsyncMock()
        state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "event_timezone": "Europe/Moscow",
            "event_days": 2,
            "event_session_index": 1,
            "event_platform": "telemost",
            "event_sessions": [],
        }
        with patch.object(lifecycle, "_prompt_session_url", new=AsyncMock()) as prompt_url:
            await lifecycle.receive_session_time(_message("25.09.2026 19:00-21:00"), state)
        prompt_url.assert_awaited_once()

        bad = _message("nonsense")
        with patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"):
            await lifecycle.receive_session_time(bad, state)
        self.assertIn("Не удалось понять", bad.answer.await_args.args[0])

        with patch.object(lifecycle, "_cancel", new=AsyncMock()) as cancel:
            await lifecycle.receive_session_time(_message("cancel"), state)
        cancel.assert_awaited_once()

        next_state = AsyncMock()
        next_state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "event_days": 2,
            "event_session_index": 1,
            "event_timezone": "Europe/Moscow",
            "event_platform": "telemost",
            "event_sessions": [],
            "pending_session": _session_payload(1, join_url=None),
        }
        with patch.object(lifecycle, "_prompt_session_date", new=AsyncMock()) as next_prompt:
            await lifecycle.receive_session_url(_message("https://room-one.example/live"), next_state)
        next_prompt.assert_awaited_once()
        next_state.update_data.assert_any_await(event_session_index=2, pending_session={})

        duplicate_state = AsyncMock()
        duplicate_state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "event_days": 2,
            "event_session_index": 2,
            "event_timezone": "Europe/Moscow",
            "event_platform": "telemost",
            "event_sessions": [_session_payload(1, join_url="https://same.example/live")],
            "pending_session": _session_payload(2, join_url=None),
        }
        duplicate = _message("https://same.example/live")
        await lifecycle.receive_session_url(duplicate, duplicate_state)
        self.assertIn("своя ссылка", duplicate.answer.await_args.args[0])

        final_state = AsyncMock()
        final_state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "event_days": 1,
            "event_session_index": 1,
            "event_timezone": "Europe/Moscow",
            "event_platform": "other",
            "event_sessions": [],
            "pending_session": _session_payload(1, join_url=None),
        }
        with patch.object(lifecycle, "_create_configured_event", new=AsyncMock()) as create:
            await lifecycle.receive_session_url(_message("-"), final_state)
        create.assert_awaited_once()

    async def test_single_event_creation_and_failures(self) -> None:
        missing = AsyncMock()
        missing.get_data.return_value = {}
        await lifecycle._create_configured_event(_message(), missing)
        missing.clear.assert_awaited_once()

        incomplete = AsyncMock()
        incomplete.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "event_title": "Один эфир",
            "event_timezone": "Europe/Moscow",
            "event_days": 1,
            "event_sessions": [],
        }
        incomplete_message = _message()
        with patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"):
            await lifecycle._create_configured_event(incomplete_message, incomplete)
        self.assertIn("не все дни", incomplete_message.answer.await_args.args[0])

        state = AsyncMock()
        state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "event_title": "Один эфир",
            "event_timezone": "Europe/Moscow",
            "event_days": 1,
            "event_sessions": [_session_payload(1, join_url=None)],
        }
        created = SimpleNamespace(
            event_id=EVENT_ID,
            provider_key="external",
            join_ready=False,
            registration_url=lambda base: f"{base}/e/webinar",
        )
        with (
            patch.object(lifecycle.control, "_actor", new=AsyncMock(return_value=object())),
            patch.object(lifecycle, "create_and_publish_online_event", return_value=created) as create,
            patch.object(lifecycle, "_public_base_url", return_value="https://cp.example"),
            patch.object(lifecycle, "_begin_warmup_choice", new=AsyncMock()) as warmup,
        ):
            await lifecycle._create_configured_event(_message(), state)
        self.assertIsNone(create.call_args.kwargs["request"].join_url)
        warmup.assert_awaited_once()

        with (
            patch.object(lifecycle.control, "_actor", new=AsyncMock(return_value=object())),
            patch.object(lifecycle, "create_and_publish_online_event", side_effect=RuntimeError("boom")),
            patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"),
        ):
            failed = _message()
            await lifecycle._create_configured_event(failed, state)
        self.assertIn("Не удалось создать", failed.answer.await_args.args[0])

    async def test_warmup_paths(self) -> None:
        missing = AsyncMock()
        missing.get_data.return_value = {}
        await lifecycle.receive_warmup_days(_message("1"), missing)
        missing.clear.assert_awaited_once()

        cancel_state = AsyncMock()
        cancel_state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "created_event_id": EVENT_ID,
            "created_join_ready": False,
        }
        with (
            patch.object(lifecycle.control, "_uuid_token", return_value="tok"),
            patch.object(lifecycle.control, "_keyboard", return_value="actions"),
        ):
            await lifecycle.receive_warmup_days(_message("отмена"), cancel_state)
        cancel_state.clear.assert_awaited_once()

        invalid_state = AsyncMock()
        invalid_state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "created_event_id": EVENT_ID,
            "max_warmup_days": 3,
        }
        invalid = _message("9")
        with patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"):
            await lifecycle.receive_warmup_days(invalid, invalid_state)
        self.assertIn("0 до 3", invalid.answer.await_args.args[0])

        actor = object()
        zero = AsyncMock()
        zero.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "created_event_id": EVENT_ID,
            "max_warmup_days": 3,
        }
        zero_plan = SimpleNamespace(requested_days=0, drafts=())
        with (
            patch.object(lifecycle.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(lifecycle, "get_event_warmup_plan", return_value=zero_plan),
            patch.object(lifecycle, "set_event_content_mode", return_value=None) as set_mode,
            patch.object(lifecycle, "_ask_event_day_mode", new=AsyncMock()) as ask_day,
        ):
            await lifecycle.receive_warmup_days(_message("0"), zero)
        set_mode.assert_called_once()
        ask_day.assert_awaited_once()

        warm = AsyncMock()
        warm.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "created_event_id": EVENT_ID,
            "max_warmup_days": 3,
        }
        draft = SimpleNamespace(position=1, publish_date=date(2026, 9, 24), text="Прогрев")
        plan = SimpleNamespace(requested_days=1, drafts=(draft,))
        with (
            patch.object(lifecycle.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(lifecycle, "get_event_warmup_plan", return_value=plan),
            patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"),
        ):
            await lifecycle.receive_warmup_days(_message("1"), warm)
        warm.set_state.assert_awaited_with(
            lifecycle.ClientPlatformEventLifecycleState.waiting_warmup_mode
        )

        failure = AsyncMock()
        failure.get_data.return_value = warm.get_data.return_value
        failed_message = _message("1")
        with (
            patch.object(lifecycle.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(lifecycle, "get_event_warmup_plan", side_effect=ValueError("bad")),
            patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"),
        ):
            await lifecycle.receive_warmup_days(failed_message, failure)
        self.assertIn("Не удалось подготовить", failed_message.answer.await_args.args[0])

    async def test_content_mode_chain(self) -> None:
        data = {"event_business_id": BUSINESS_ID, "created_event_id": EVENT_ID}
        actor = object()
        with (
            patch.object(lifecycle.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(lifecycle, "set_event_content_mode", return_value=None) as stored,
        ):
            await lifecycle._store_mode(
                message=_message("2"),
                data=data,
                stage=lifecycle.EventContentStage.WARMUP,
                mode=lifecycle.EventContentMode.TEXT_WITH_IMAGE,
            )
        stored.assert_called_once()

        warm = AsyncMock()
        warm.get_data.return_value = data
        with (
            patch.object(lifecycle, "_store_mode", new=AsyncMock()) as store,
            patch.object(lifecycle, "_ask_event_day_mode", new=AsyncMock()) as next_step,
        ):
            await lifecycle.receive_warmup_mode(_message("2"), warm)
        store.assert_awaited_once()
        next_step.assert_awaited_once()

        event_day = AsyncMock()
        event_day.get_data.return_value = data
        with (
            patch.object(lifecycle, "_store_mode", new=AsyncMock()),
            patch.object(lifecycle, "_ask_post_event_mode", new=AsyncMock()) as next_step,
        ):
            await lifecycle.receive_event_day_mode(_message("3"), event_day)
        next_step.assert_awaited_once()

        post = AsyncMock()
        post.get_data.return_value = data
        with (
            patch.object(lifecycle, "_store_mode", new=AsyncMock()),
            patch.object(lifecycle, "_finish_content_setup", new=AsyncMock()) as finish,
        ):
            await lifecycle.receive_post_event_mode(_message("1"), post)
        finish.assert_awaited_once()

        invalid = _message("нет такого")
        invalid_state = AsyncMock()
        invalid_state.get_data.return_value = data
        with patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"):
            await lifecycle.receive_event_day_mode(invalid, invalid_state)
        self.assertIn("Как оформить", invalid.answer.await_args.args[0])

        missing = AsyncMock()
        missing.get_data.return_value = {}
        await lifecycle.receive_post_event_mode(_message("1"), missing)
        missing.clear.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
