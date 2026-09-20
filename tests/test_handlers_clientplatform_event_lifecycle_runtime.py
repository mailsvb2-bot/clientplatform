from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from clientplatform.domain.event_content import EventContentMode, EventContentStage
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
        self.assertTrue(lifecycle._is_cancel(_message(" Отмена ")))
        self.assertFalse(lifecycle._is_cancel(_message("продолжить")))
        with (
            patch.object(lifecycle.control, "_uuid_token", side_effect=_token),
            patch.object(lifecycle.control, "_keyboard", side_effect=lambda rows: rows),
        ):
            actions = lifecycle._event_actions(
                event_id=EVENT_ID,
                business_id=BUSINESS_ID,
                join_ready=True,
                visual_requested=True,
            )
        self.assertTrue(any(button[0] == "🎨 Картинки и креативы" for row in actions for button in row))

    async def test_three_day_happy_path_asks_timezone_and_three_content_modes(self) -> None:
        state = _State({"event_business_id": BUSINESS_ID})
        actor = MagicMock(unsafe=True)
        created = SimpleNamespace(
            event_id=EVENT_ID,
            provider_key="external",
            join_ready=True,
            registration_url=lambda base: f"{base}/e/public-slug",
        )
        window = SimpleNamespace(days_until_event=8, max_warmup_days=7)
        warmup = SimpleNamespace(
            requested_days=2,
            drafts=(
                SimpleNamespace(position=1, publish_date=date(2026, 9, 23), text="Текст 1"),
                SimpleNamespace(position=2, publish_date=date(2026, 9, 24), text="Текст 2"),
            ),
        )
        parsed_sessions = [
            lifecycle.EventWizardSession(
                position=index,
                starts_at=datetime(2026, 9, 24 + index, 16, tzinfo=timezone.utc),
                ends_at=datetime(2026, 9, 24 + index, 18, tzinfo=timezone.utc),
                local_label=f"{24 + index}.09.2026 19:00–21:00",
            )
            for index in range(1, 4)
        ]
        with (
            patch.object(lifecycle.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(lifecycle.control, "_uuid_token", side_effect=_token),
            patch.object(lifecycle.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"),
            patch.object(lifecycle, "parse_session_window", side_effect=parsed_sessions),
            patch.object(
                lifecycle,
                "create_and_publish_multisession_online_event",
                return_value=created,
            ) as create,
            patch.object(lifecycle, "_public_base_url", return_value="https://clientplatform.example.test"),
            patch.object(lifecycle, "get_event_warmup_window", return_value=window),
            patch.object(lifecycle, "save_event_warmup_plan", return_value=warmup),
            patch.object(lifecycle, "set_event_content_mode") as set_mode,
        ):
            await lifecycle.receive_title(_message("Три дня практики"), state)
            await lifecycle.receive_days(_message("3"), state)
            self.assertEqual(state.current, lifecycle.ClientPlatformEventLifecycleState.waiting_topics_choice)
            await lifecycle.receive_event_topics(
                _message("Диагностика\nПрактика\nПлан действий"),
                state,
            )
            self.assertEqual(state.current, lifecycle.ClientPlatformEventLifecycleState.waiting_timezone)
            await lifecycle.receive_timezone(_message("Москва"), state)
            self.assertEqual(state.data["event_timezone"], "Europe/Moscow")
            for index in range(1, 4):
                await lifecycle.receive_session_time(
                    _message(f"{24 + index}.09.2026 19:00-21:00"), state
                )
                await lifecycle.receive_session_url(
                    _message(f"https://room-{index}.example.test/live"), state
                )
            request = create.call_args.kwargs["request"]
            self.assertEqual(len(request.sessions), 3)
            self.assertEqual(state.current, lifecycle.ClientPlatformEventLifecycleState.waiting_warmup_days)
            await lifecycle.receive_warmup_days(_message("2"), state)
            self.assertEqual(state.current, lifecycle.ClientPlatformEventLifecycleState.waiting_warmup_mode)
            await lifecycle.receive_warmup_mode(_message("2"), state)
            self.assertEqual(state.current, lifecycle.ClientPlatformEventLifecycleState.waiting_event_day_mode)
            await lifecycle.receive_event_day_mode(_message("3"), state)
            self.assertEqual(state.current, lifecycle.ClientPlatformEventLifecycleState.waiting_post_event_mode)
            final = _message("1")
            await lifecycle.receive_post_event_mode(final, state)

        self.assertEqual(state.clear_count, 1)
        selected = [(call.kwargs["stage"], call.kwargs["mode"]) for call in set_mode.call_args_list]
        self.assertEqual(
            selected,
            [
                (EventContentStage.WARMUP, EventContentMode.TEXT_WITH_IMAGE),
                (EventContentStage.EVENT_DAY, EventContentMode.TEXT_IN_IMAGE),
                (EventContentStage.POST_EVENT, EventContentMode.TEXT),
            ],
        )
        self.assertIn("Сообщения до вебинара: 2 дн. — Текст + картинка", final.answer.await_args_list[0].args[0])
        self.assertEqual(final.answer.await_count, 3)

    async def test_one_day_keeps_room_later_and_zero_warmup(self) -> None:
        state = _State(
            {
                "event_business_id": BUSINESS_ID,
                "event_title": "Один эфир",
                "event_days": 1,
                "event_timezone": "Europe/Moscow",
                "event_session_index": 1,
                "event_sessions": [],
            }
        )
        actor = MagicMock(unsafe=True)
        created = SimpleNamespace(
            event_id=EVENT_ID,
            provider_key="pending",
            join_ready=False,
            registration_url=lambda base: f"{base}/e/public-slug",
        )
        window = SimpleNamespace(days_until_event=5, max_warmup_days=4)
        plan = SimpleNamespace(requested_days=0, drafts=())
        parsed = lifecycle.EventWizardSession(
            position=1,
            starts_at=datetime(2026, 9, 25, 16, tzinfo=timezone.utc),
            ends_at=datetime(2026, 9, 25, 18, tzinfo=timezone.utc),
            local_label="25.09.2026 19:00–21:00",
        )
        with (
            patch.object(lifecycle.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"),
            patch.object(lifecycle, "parse_session_window", return_value=parsed),
            patch.object(lifecycle, "create_and_publish_online_event", return_value=created) as create,
            patch.object(lifecycle, "_public_base_url", return_value="https://clientplatform.example.test"),
            patch.object(lifecycle, "get_event_warmup_window", return_value=window),
            patch.object(lifecycle, "save_event_warmup_plan", return_value=plan),
            patch.object(lifecycle, "set_event_content_mode") as set_mode,
        ):
            time_message = _message("25.09.2026 19:00-21:00")
            await lifecycle.receive_session_time(time_message, state)
            self.assertIn("появится позже", time_message.answer.await_args.args[0])
            await lifecycle.receive_session_url(_message("-"), state)
            request = create.call_args.kwargs["request"]
            self.assertIsNone(request.join_url)
            await lifecycle.receive_warmup_days(_message("0"), state)
        set_mode.assert_called_once()
        self.assertEqual(set_mode.call_args.kwargs["stage"], EventContentStage.WARMUP)
        self.assertEqual(state.current, lifecycle.ClientPlatformEventLifecycleState.waiting_event_day_mode)

    async def test_invalid_timezone_and_multiday_missing_room_do_not_advance(self) -> None:
        timezone_state = _State({"event_business_id": BUSINESS_ID, "event_days": 2})
        timezone_message = _message("UTC+5")
        with patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"):
            await lifecycle.receive_timezone(timezone_message, timezone_state)
        self.assertIn("Не удалось определить", timezone_message.answer.await_args.args[0])
        room_state = _State(
            {
                "event_business_id": BUSINESS_ID,
                "event_days": 2,
                "event_timezone": "Europe/Moscow",
                "event_session_index": 1,
                "event_sessions": [],
                "pending_session": {
                    "position": 1,
                    "starts_at": "2026-09-25T16:00:00+00:00",
                    "ends_at": "2026-09-25T18:00:00+00:00",
                    "local_label": "25.09.2026 19:00–21:00",
                },
            }
        )
        room_message = _message("-")
        with patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"):
            await lifecycle.receive_session_url(room_message, room_state)
        self.assertIn("отдельная HTTPS-ссылка", room_message.answer.await_args.args[0])
        self.assertEqual(room_state.data["event_sessions"], [])


if __name__ == "__main__":
    unittest.main()
