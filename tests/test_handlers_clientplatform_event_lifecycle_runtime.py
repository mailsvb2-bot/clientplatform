from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from handlers import clientplatform_event_lifecycle as lifecycle


BUSINESS_ID = "11111111-1111-4111-8111-111111111111"
EVENT_ID = "33333333-3333-4333-8333-333333333333"
TOKEN = "ERERERERQRGBEREREREREQ"
EVENT_TOKEN = "MzMzMzMzQzODMzMzMzMzMzMzMz"


class _State:
    def __init__(self, data: dict[str, object] | None = None) -> None:
        self.data = dict(data or {})
        self.current = None
        self.clear_count = 0

    async def get_data(self) -> dict[str, object]:
        return dict(self.data)

    async def update_data(self, **values: object) -> None:
        self.data.update(values)

    async def set_state(self, value) -> None:
        self.current = value

    async def clear(self) -> None:
        self.clear_count += 1
        self.data.clear()
        self.current = None


def _message(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
    )


def _callback() -> SimpleNamespace:
    return SimpleNamespace(
        data=f"cpev:new:{TOKEN}",
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
    )


def _token(value: str) -> str:
    return EVENT_TOKEN if value == EVENT_ID else TOKEN


class EventLifecycleRuntimeCoverageTests(unittest.IsolatedAsyncioTestCase):
    def test_helpers_keep_https_cancel_and_action_contracts(self) -> None:
        with patch.object(
            lifecycle.settings,
            "MESSENGER_PUBLIC_BASE_URL",
            " http://unsafe.example/ ",
            create=True,
        ):
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                lifecycle._public_base_url()
        with patch.object(
            lifecycle.settings,
            "MESSENGER_PUBLIC_BASE_URL",
            " https://clientplatform.example.test/ ",
            create=True,
        ):
            self.assertEqual(
                lifecycle._public_base_url(),
                "https://clientplatform.example.test",
            )

        self.assertEqual(lifecycle._normalized_text(_message("  два   дня  ")), "два дня")
        self.assertTrue(lifecycle._is_cancel(_message(" Отмена ")))
        self.assertFalse(lifecycle._is_cancel(_message("продолжить")))

        with (
            patch.object(lifecycle.control, "_uuid_token", side_effect=_token),
            patch.object(lifecycle.control, "_keyboard", side_effect=lambda rows: rows),
        ):
            cancel = lifecycle._cancel_keyboard(BUSINESS_ID)
            not_ready = lifecycle._event_actions(
                event_id=EVENT_ID,
                business_id=BUSINESS_ID,
                join_ready=False,
            )
            ready = lifecycle._event_actions(
                event_id=EVENT_ID,
                business_id=BUSINESS_ID,
                join_ready=True,
            )
        self.assertEqual(cancel, [[(lifecycle.BACK_TO_EVENTS_LABEL, f"cpev:cancel:{TOKEN}")]])
        self.assertEqual(not_ready[0][0][0], "🔗 Добавить ссылку на эфир")
        self.assertNotEqual(ready[0][0][0], "🔗 Добавить ссылку на эфир")

    async def test_start_wizard_denies_non_manager(self) -> None:
        callback = _callback()
        state = _State()
        actor = MagicMock(unsafe=True)
        actor.assert_can_manage_business.side_effect = lifecycle.TenantPermissionDenied("denied")
        with (
            patch.object(lifecycle.control, "_token_uuid", return_value=BUSINESS_ID),
            patch.object(lifecycle.control, "_actor", new=AsyncMock(return_value=actor)),
        ):
            await lifecycle.start_multisession_event_wizard(callback, state)
        self.assertEqual(state.clear_count, 0)
        callback.answer.assert_awaited_once_with(
            "Создавать мероприятия может владелец или администратор",
            show_alert=True,
        )

    async def test_title_and_day_count_recovery_cancel_and_validation(self) -> None:
        missing = _State()
        missing_message = _message("Название")
        await lifecycle.receive_title(missing_message, missing)
        self.assertEqual(missing.clear_count, 1)
        self.assertIn("Откройте вебинары заново", missing_message.answer.await_args.args[0])

        with (
            patch.object(lifecycle.control, "_uuid_token", side_effect=_token),
            patch.object(lifecycle.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"),
        ):
            blank = _State({"event_business_id": BUSINESS_ID})
            blank_message = _message("   ")
            await lifecycle.receive_title(blank_message, blank)
            self.assertIn("Введите название", blank_message.answer.await_args.args[0])

            cancelled = _State({"event_business_id": BUSINESS_ID})
            cancelled_message = _message("Отмена")
            await lifecycle.receive_title(cancelled_message, cancelled)
            self.assertEqual(cancelled.clear_count, 1)
            self.assertIn("отменено", cancelled_message.answer.await_args.args[0])

            missing_days = _State()
            missing_days_message = _message("2")
            await lifecycle.receive_days(missing_days_message, missing_days)
            self.assertEqual(missing_days.clear_count, 1)

            invalid = _State({"event_business_id": BUSINESS_ID})
            invalid_message = _message("3")
            await lifecycle.receive_days(invalid_message, invalid)
            self.assertIn("1 или 2", invalid_message.answer.await_args.args[0])

            cancel_days = _State({"event_business_id": BUSINESS_ID})
            cancel_days_message = _message("cancel")
            await lifecycle.receive_days(cancel_days_message, cancel_days)
            self.assertEqual(cancel_days.clear_count, 1)

    async def test_two_day_happy_path_reaches_warmup_and_renders_drafts(self) -> None:
        state = _State({"event_business_id": BUSINESS_ID})
        actor = MagicMock(unsafe=True)
        created = SimpleNamespace(
            event_id=EVENT_ID,
            provider_key="external",
            join_ready=True,
            registration_url=lambda base: f"{base}/e/public-slug",
        )
        window = SimpleNamespace(days_until_event=8, max_warmup_days=7)
        plan = SimpleNamespace(
            requested_days=2,
            drafts=(
                SimpleNamespace(position=1, publish_date=date(2026, 9, 23), text="Текст 1"),
                SimpleNamespace(position=2, publish_date=date(2026, 9, 24), text="Текст 2"),
            ),
        )

        def parse_start(value: str, *, timezone_name: str) -> str:
            self.assertEqual(timezone_name, "Europe/Moscow")
            if value.startswith("25."):
                return "2026-09-25T16:00:00+00:00"
            return "2026-09-26T16:00:00+00:00"

        with (
            patch.object(lifecycle.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(lifecycle.control, "_uuid_token", side_effect=_token),
            patch.object(lifecycle.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"),
            patch.object(
                lifecycle,
                "get_business_profile",
                return_value=SimpleNamespace(timezone="Europe/Moscow"),
            ),
            patch.object(lifecycle, "parse_local_booking_start", side_effect=parse_start),
            patch.object(
                lifecycle,
                "create_and_publish_multisession_online_event",
                return_value=created,
            ) as create,
            patch.object(
                lifecycle,
                "_public_base_url",
                return_value="https://clientplatform.example.test",
            ),
            patch.object(lifecycle, "get_event_warmup_window", return_value=window),
            patch.object(lifecycle, "get_event_warmup_plan", return_value=plan),
        ):
            await lifecycle.receive_title(_message("Два дня практики"), state)
            self.assertEqual(state.current, lifecycle.ClientPlatformEventLifecycleState.waiting_days)
            await lifecycle.receive_days(_message("два дня"), state)
            await lifecycle.receive_day1_time(_message("25.09.2026 19:00"), state)
            await lifecycle.receive_day1_url(
                _message("https://room-one.example.test/live"), state
            )
            await lifecycle.receive_day2_time(_message("26.09.2026 19:00"), state)
            await lifecycle.receive_day2_url(
                _message("https://room-two.example.test/live"), state
            )
            self.assertEqual(
                state.current,
                lifecycle.ClientPlatformEventLifecycleState.waiting_warmup_days,
            )
            self.assertEqual(state.data["max_warmup_days"], 7)
            request = create.call_args.kwargs["request"]
            self.assertEqual(len(request.sessions), 2)
            self.assertEqual(request.sessions[0].join_url, "https://room-one.example.test/live")
            self.assertEqual(request.sessions[1].join_url, "https://room-two.example.test/live")

            warmup_message = _message("2")
            await lifecycle.receive_warmup_days(warmup_message, state)

        self.assertEqual(state.clear_count, 1)
        self.assertEqual(warmup_message.answer.await_count, 3)
        rendered = [call.args[0] for call in warmup_message.answer.await_args_list]
        self.assertIn("День 1: 25.09.2026 19:00", rendered[0])
        self.assertIn("День 2: 26.09.2026 19:00", rendered[0])
        self.assertIn("Текст 1", rendered[1])
        self.assertIn("Текст 2", rendered[2])

    async def test_one_day_happy_path_allows_room_later_and_zero_warmup(self) -> None:
        state = _State({"event_business_id": BUSINESS_ID})
        actor = MagicMock(unsafe=True)
        created = SimpleNamespace(
            event_id=EVENT_ID,
            provider_key="pending",
            join_ready=False,
            registration_url=lambda base: f"{base}/e/public-slug",
        )
        window = SimpleNamespace(days_until_event=5, max_warmup_days=4)
        plan = SimpleNamespace(requested_days=0, drafts=())
        with (
            patch.object(lifecycle.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(lifecycle.control, "_uuid_token", side_effect=_token),
            patch.object(lifecycle.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"),
            patch.object(
                lifecycle,
                "get_business_profile",
                return_value=SimpleNamespace(timezone="Europe/Moscow"),
            ),
            patch.object(
                lifecycle,
                "parse_local_booking_start",
                return_value="2026-09-25T16:00:00+00:00",
            ),
            patch.object(
                lifecycle,
                "create_and_publish_online_event",
                return_value=created,
            ) as create,
            patch.object(
                lifecycle,
                "_public_base_url",
                return_value="https://clientplatform.example.test",
            ),
            patch.object(lifecycle, "get_event_warmup_window", return_value=window),
            patch.object(lifecycle, "get_event_warmup_plan", return_value=plan),
        ):
            await lifecycle.receive_title(_message("Один эфир"), state)
            await lifecycle.receive_days(_message("один день"), state)
            time_message = _message("25.09.2026 19:00")
            await lifecycle.receive_day1_time(time_message, state)
            self.assertIn("Можно отправить", time_message.answer.await_args.args[0])
            await lifecycle.receive_day1_url(_message("-"), state)
            request = create.call_args.kwargs["request"]
            self.assertIsNone(request.join_url)
            self.assertEqual(
                state.current,
                lifecycle.ClientPlatformEventLifecycleState.waiting_warmup_days,
            )
            final = _message("0")
            await lifecycle.receive_warmup_days(final, state)

        self.assertEqual(state.clear_count, 1)
        self.assertEqual(final.answer.await_count, 1)
        self.assertIn("Прогрев: 0 дн.", final.answer.await_args.args[0])

    async def test_day_one_time_and_room_validation_paths(self) -> None:
        missing = _State()
        missing_message = _message("25.09.2026 19:00")
        await lifecycle.receive_day1_time(missing_message, missing)
        self.assertEqual(missing.clear_count, 1)

        with (
            patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"),
            patch.object(lifecycle.control, "_uuid_token", side_effect=_token),
            patch.object(lifecycle.control, "_keyboard", side_effect=lambda rows: rows),
        ):
            invalid = _State(
                {
                    "event_business_id": BUSINESS_ID,
                    "event_timezone": "Europe/Moscow",
                    "event_days": 1,
                }
            )
            with patch.object(
                lifecycle,
                "parse_local_booking_start",
                side_effect=ValueError("bad"),
            ):
                invalid_message = _message("не дата")
                await lifecycle.receive_day1_time(invalid_message, invalid)
            self.assertIn("Не удалось понять", invalid_message.answer.await_args.args[0])

            cancelled = _State(
                {
                    "event_business_id": BUSINESS_ID,
                    "event_timezone": "Europe/Moscow",
                }
            )
            await lifecycle.receive_day1_time(_message("Отмена"), cancelled)
            self.assertEqual(cancelled.clear_count, 1)

            no_business = _State()
            await lifecycle.receive_day1_url(_message("https://room.example"), no_business)
            self.assertEqual(no_business.clear_count, 1)

            missing_two_day_room = _State(
                {"event_business_id": BUSINESS_ID, "event_days": 2}
            )
            missing_room_message = _message("-")
            await lifecycle.receive_day1_url(missing_room_message, missing_two_day_room)
            self.assertIn("нужна отдельная", missing_room_message.answer.await_args.args[0])

            bad_url = _State({"event_business_id": BUSINESS_ID, "event_days": 1})
            bad_url_message = _message("http://room.example")
            await lifecycle.receive_day1_url(bad_url_message, bad_url)
            self.assertIn("https://", bad_url_message.answer.await_args.args[0])

            cancelled_url = _State({"event_business_id": BUSINESS_ID, "event_days": 1})
            await lifecycle.receive_day1_url(_message("cancel"), cancelled_url)
            self.assertEqual(cancelled_url.clear_count, 1)

    async def test_single_day_creation_failure_is_recoverable(self) -> None:
        state = _State(
            {
                "event_business_id": BUSINESS_ID,
                "event_title": "Эфир",
                "event_timezone": "Europe/Moscow",
                "day1_starts_at": "2026-09-25T16:00:00+00:00",
                "day1_local_time": "25.09.2026 19:00",
                "day1_url": "https://room.example/live",
            }
        )
        message = _message("unused")
        with (
            patch.object(lifecycle.control, "_actor", new=AsyncMock(return_value=MagicMock())),
            patch.object(
                lifecycle,
                "create_and_publish_online_event",
                side_effect=RuntimeError("boom"),
            ),
            patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"),
        ):
            await lifecycle._create_single_day_event(message, state)
        self.assertIn("Не удалось создать", message.answer.await_args.args[0])
        self.assertEqual(state.clear_count, 0)

    async def test_day_two_time_and_room_validation_paths(self) -> None:
        missing = _State()
        await lifecycle.receive_day2_time(_message("26.09.2026 19:00"), missing)
        self.assertEqual(missing.clear_count, 1)

        with (
            patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"),
            patch.object(lifecycle.control, "_uuid_token", side_effect=_token),
            patch.object(lifecycle.control, "_keyboard", side_effect=lambda rows: rows),
        ):
            cancelled = _State(
                {
                    "event_business_id": BUSINESS_ID,
                    "event_timezone": "Europe/Moscow",
                }
            )
            await lifecycle.receive_day2_time(_message("Отмена"), cancelled)
            self.assertEqual(cancelled.clear_count, 1)

            earlier = _State(
                {
                    "event_business_id": BUSINESS_ID,
                    "event_timezone": "Europe/Moscow",
                    "day1_starts_at": "2026-09-25T16:00:00+00:00",
                }
            )
            with patch.object(
                lifecycle,
                "parse_local_booking_start",
                return_value="2026-09-25T15:00:00+00:00",
            ):
                earlier_message = _message("25.09.2026 18:00")
                await lifecycle.receive_day2_time(earlier_message, earlier)
            self.assertIn("позже дня 1", earlier_message.answer.await_args.args[0])

            missing_url_state = _State()
            await lifecycle.receive_day2_url(_message("https://room-two.example"), missing_url_state)
            self.assertEqual(missing_url_state.clear_count, 1)

            cancel_url_state = _State({"event_business_id": BUSINESS_ID})
            await lifecycle.receive_day2_url(_message("cancel"), cancel_url_state)
            self.assertEqual(cancel_url_state.clear_count, 1)

            bad_url_state = _State(
                {"event_business_id": BUSINESS_ID, "day1_url": "https://one.example"}
            )
            bad_message = _message("http://two.example")
            await lifecycle.receive_day2_url(bad_message, bad_url_state)
            self.assertIn("https://", bad_message.answer.await_args.args[0])

            same_url_state = _State(
                {"event_business_id": BUSINESS_ID, "day1_url": "https://one.example"}
            )
            same_message = _message("https://one.example")
            await lifecycle.receive_day2_url(same_message, same_url_state)
            self.assertIn("другая ссылка", same_message.answer.await_args.args[0])

    async def test_two_day_creation_failure_is_recoverable(self) -> None:
        state = _State(
            {
                "event_business_id": BUSINESS_ID,
                "event_title": "Два дня",
                "event_timezone": "Europe/Moscow",
                "day1_starts_at": "2026-09-25T16:00:00+00:00",
                "day1_local_time": "25.09.2026 19:00",
                "day1_url": "https://one.example/live",
                "day2_starts_at": "2026-09-26T16:00:00+00:00",
                "day2_local_time": "26.09.2026 19:00",
            }
        )
        message = _message("https://two.example/live")
        with (
            patch.object(lifecycle.control, "_actor", new=AsyncMock(return_value=MagicMock())),
            patch.object(
                lifecycle,
                "create_and_publish_multisession_online_event",
                side_effect=RuntimeError("boom"),
            ),
            patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"),
        ):
            await lifecycle.receive_day2_url(message, state)
        self.assertIn("Не удалось создать двухдневный", message.answer.await_args.args[0])
        self.assertEqual(state.clear_count, 0)

    async def test_warmup_recovery_cancel_range_and_generation_error(self) -> None:
        missing = _State()
        missing_message = _message("1")
        await lifecycle.receive_warmup_days(missing_message, missing)
        self.assertEqual(missing.clear_count, 1)
        self.assertIn("Вебинар создан", missing_message.answer.await_args.args[0])

        base = {
            "event_business_id": BUSINESS_ID,
            "created_event_id": EVENT_ID,
            "created_join_ready": False,
            "max_warmup_days": 3,
            "created_title": "Вебинар",
            "created_local_times": ["25.09.2026 19:00"],
            "created_registration_url": "https://clientplatform.example.test/e/public",
        }
        with (
            patch.object(lifecycle.control, "_uuid_token", side_effect=_token),
            patch.object(lifecycle.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"),
        ):
            cancelled = _State(base)
            cancelled_message = _message("Отмена")
            await lifecycle.receive_warmup_days(cancelled_message, cancelled)
            self.assertEqual(cancelled.clear_count, 1)
            self.assertIn("уже создан", cancelled_message.answer.await_args.args[0])

            non_number = _State(base)
            non_number_message = _message("много")
            await lifecycle.receive_warmup_days(non_number_message, non_number)
            self.assertIn("от 0 до 3", non_number_message.answer.await_args.args[0])

            too_many = _State(base)
            too_many_message = _message("4")
            await lifecycle.receive_warmup_days(too_many_message, too_many)
            self.assertIn("от 0 до 3", too_many_message.answer.await_args.args[0])

            generation_error = _State(base)
            error_message = _message("1")
            with (
                patch.object(
                    lifecycle.control,
                    "_actor",
                    new=AsyncMock(return_value=MagicMock()),
                ),
                patch.object(
                    lifecycle,
                    "get_event_warmup_plan",
                    side_effect=ValueError("bad plan"),
                ),
            ):
                await lifecycle.receive_warmup_days(error_message, generation_error)
            self.assertIn("Не удалось подготовить", error_message.answer.await_args.args[0])
            self.assertEqual(generation_error.clear_count, 0)


if __name__ == "__main__":
    unittest.main()
