from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers import clientplatform_event_lifecycle as lifecycle


BUSINESS_ID = "11111111-1111-4111-8111-111111111111"


class _State:
    def __init__(self, data: dict[str, object] | None = None) -> None:
        self.data = dict(data or {})
        self.current = None

    async def get_data(self) -> dict[str, object]:
        return dict(self.data)

    async def update_data(self, **values: object) -> None:
        self.data.update(values)

    async def set_state(self, value) -> None:
        self.current = value

    async def clear(self) -> None:
        self.data.clear()
        self.current = None


def _message(text: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
        edit_reply_markup=AsyncMock(),
    )


def _callback(data: str) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
    )


class EventSchedulePickerHandlerTests(unittest.IsolatedAsyncioTestCase):
    def test_picker_keyboards_offer_timezone_venue_calendar_time_and_duration(self) -> None:
        with patch.object(lifecycle.control, "_uuid_token", return_value="business-token"):
            timezone_keyboard = lifecycle._timezone_keyboard(BUSINESS_ID)
            timezone_callbacks = [
                button.callback_data
                for row in timezone_keyboard.inline_keyboard
                for button in row
                if button.callback_data
            ]
            self.assertIn("cpev:tz:moscow", timezone_callbacks)
            self.assertIn("cpev:tz:other", timezone_callbacks)

            venue_keyboard = lifecycle._venue_keyboard(BUSINESS_ID)
            venue_labels = [button.text for row in venue_keyboard.inline_keyboard for button in row]
            self.assertIn("Яндекс Телемост", venue_labels)
            self.assertIn("Zoom", venue_labels)
            self.assertIn("Webinar.ru", venue_labels)
            self.assertIn("GetCourse", venue_labels)
            self.assertIn("UCR", venue_labels)
            self.assertIn("Другой сервис", venue_labels)

            calendar_keyboard = lifecycle._calendar_keyboard(
                business_id=BUSINESS_ID,
                timezone_name="Europe/Moscow",
                year=2026,
                month=9,
                minimum_date=date(2026, 9, 17),
            )
            calendar_callbacks = [
                button.callback_data
                for row in calendar_keyboard.inline_keyboard
                for button in row
                if button.callback_data
            ]
            self.assertIn("cpev:date:2026-09-17", calendar_callbacks)
            self.assertNotIn("cpev:date:2026-09-16", calendar_callbacks)
            self.assertIn("cpev:manual-time", calendar_callbacks)

            time_keyboard = lifecycle._start_time_keyboard(BUSINESS_ID)
            self.assertTrue(
                any(
                    button.callback_data == "cpev:start:1900"
                    for row in time_keyboard.inline_keyboard
                    for button in row
                )
            )
            duration_keyboard = lifecycle._duration_keyboard(BUSINESS_ID)
            self.assertTrue(
                any(
                    button.callback_data == "cpev:duration:120"
                    for row in duration_keyboard.inline_keyboard
                    for button in row
                )
            )

    def test_calendar_has_obvious_month_navigation_two_months_ahead(self) -> None:
        with patch.object(lifecycle.control, "_uuid_token", return_value="business-token"):
            october = lifecycle._calendar_keyboard(
                business_id=BUSINESS_ID,
                timezone_name="Europe/Moscow",
                year=2026,
                month=10,
                minimum_date=date(2026, 9, 17),
            )
        buttons = [
            (button.text, button.callback_data)
            for row in october.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertIn(("⬅️ Сентябрь", "cpev:month:202609"), buttons)
        self.assertIn(("Ноябрь ➡️", "cpev:month:202611"), buttons)

        with patch.object(lifecycle.control, "_uuid_token", return_value="business-token"):
            november = lifecycle._calendar_keyboard(
                business_id=BUSINESS_ID,
                timezone_name="Europe/Moscow",
                year=2026,
                month=11,
                minimum_date=date(2026, 9, 17),
            )
        november_callbacks = {
            button.callback_data
            for row in november.inline_keyboard
            for button in row
            if button.callback_data
        }
        self.assertIn("cpev:date:2026-11-20", november_callbacks)

    def test_room_keyboard_opens_selected_external_service_but_not_other(self) -> None:
        with patch.object(lifecycle.control, "_uuid_token", return_value="business-token"):
            telemost = lifecycle._session_url_keyboard(
                business_id=BUSINESS_ID,
                venue_key="telemost",
            )
            self.assertEqual(telemost.inline_keyboard[0][0].url, "https://telemost.yandex.ru/")
            other = lifecycle._session_url_keyboard(
                business_id=BUSINESS_ID,
                venue_key="other",
            )
            self.assertTrue(all(button.url is None for row in other.inline_keyboard for button in row))

    async def test_moscow_button_reaches_platform_choice_without_typing_timezone(self) -> None:
        state = _State({"event_business_id": BUSINESS_ID, "event_days": 3})
        callback = _callback("cpev:tz:moscow")
        message = _message()
        with (
            patch.object(lifecycle.control, "_callback_message", return_value=message),
            patch.object(lifecycle, "_prompt_venue", new=AsyncMock()) as prompt,
        ):
            await lifecycle.choose_moscow_timezone(callback, state)
        prompt.assert_awaited_once_with(
            message,
            state,
            business_id=BUSINESS_ID,
            timezone_name="Europe/Moscow",
        )
        callback.answer.assert_awaited_once_with()

    async def test_external_venue_starts_calendar_and_ucr_fails_honestly(self) -> None:
        state = _State(
            {
                "event_business_id": BUSINESS_ID,
                "event_days": 2,
                "event_timezone": "Europe/Moscow",
                "event_session_index": 1,
            }
        )
        message = _message()
        telemost = _callback("cpev:venue:telemost")
        with (
            patch.object(lifecycle.control, "_callback_message", return_value=message),
            patch.object(lifecycle, "_prompt_session_date", new=AsyncMock()) as prompt,
        ):
            await lifecycle.choose_webinar_venue(telemost, state)
        self.assertEqual(state.data["event_platform"], "telemost")
        prompt.assert_awaited_once_with(
            message,
            state,
            business_id=BUSINESS_ID,
            position=1,
            total=2,
            timezone_name="Europe/Moscow",
        )

        ucr = _callback("cpev:venue:ucr")
        with patch.object(lifecycle, "_prompt_session_date", new=AsyncMock()) as prompt:
            await lifecycle.choose_webinar_venue(ucr, state)
        prompt.assert_not_awaited()
        self.assertTrue(ucr.answer.await_args.kwargs["show_alert"])
        self.assertIn("не выдаёт", ucr.answer.await_args.args[0])

    async def test_date_time_duration_builds_pending_session_without_manual_interval(self) -> None:
        state = _State(
            {
                "event_business_id": BUSINESS_ID,
                "event_days": 2,
                "event_timezone": "Europe/Moscow",
                "event_session_index": 1,
                "event_platform": "telemost",
                "event_sessions": [],
                "event_picker_min_date": "2026-09-17",
                "event_picker_date": "2026-09-25",
                "event_picker_start": "19:00",
            }
        )
        callback = _callback("cpev:duration:120")
        message = _message()
        with (
            patch.object(lifecycle, "local_today", return_value=date(2026, 9, 17)),
            patch.object(lifecycle.control, "_callback_message", return_value=message),
            patch.object(lifecycle, "_prompt_session_url", new=AsyncMock()) as prompt,
        ):
            await lifecycle.choose_session_duration(callback, state)
        pending = state.data["pending_session"]
        self.assertEqual(pending["position"], 1)
        self.assertEqual(pending["local_label"], "25.09.2026 19:00–21:00")
        prompt.assert_awaited_once_with(
            message,
            state,
            business_id=BUSINESS_ID,
            position=1,
            total=2,
            venue_key="telemost",
        )

    async def test_next_session_returns_to_calendar_not_manual_date_prompt(self) -> None:
        state = _State(
            {
                "event_business_id": BUSINESS_ID,
                "event_days": 2,
                "event_timezone": "Europe/Moscow",
                "event_session_index": 1,
                "event_platform": "zoom",
                "event_sessions": [],
                "pending_session": {
                    "position": 1,
                    "starts_at": "2026-09-25T16:00:00+00:00",
                    "ends_at": "2026-09-25T18:00:00+00:00",
                    "local_label": "25.09.2026 19:00–21:00",
                },
            }
        )
        message = _message("https://zoom.us/j/123456789")
        with patch.object(lifecycle, "_prompt_session_date", new=AsyncMock()) as prompt:
            await lifecycle.receive_session_url(message, state)
        self.assertEqual(state.data["event_session_index"], 2)
        prompt.assert_awaited_once_with(
            message,
            state,
            business_id=BUSINESS_ID,
            position=2,
            total=2,
            timezone_name="Europe/Moscow",
        )


if __name__ == "__main__":
    unittest.main()
