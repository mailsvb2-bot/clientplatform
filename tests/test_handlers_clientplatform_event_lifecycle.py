from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from handlers import clientplatform_event_lifecycle as lifecycle


BUSINESS_ID = "11111111-1111-4111-8111-111111111111"
TOKEN = "ERERERERQRGBEREREREREQ"
EVENT_ID = "33333333-3333-4333-8333-333333333333"


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


class EventLifecycleHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_wizard_enters_title_state(self) -> None:
        callback = _callback()
        state = AsyncMock()
        actor = MagicMock(unsafe=True)
        reply = SimpleNamespace(answer=AsyncMock())
        with (
            patch.object(lifecycle.control, "_token_uuid", return_value=BUSINESS_ID),
            patch.object(lifecycle.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(lifecycle.control, "_callback_message", return_value=reply),
            patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"),
        ):
            await lifecycle.start_multisession_event_wizard(callback, state)

        actor.assert_can_manage_business.assert_called_once_with()
        state.clear.assert_awaited_once_with()
        state.set_state.assert_awaited_once_with(
            lifecycle.ClientPlatformEventLifecycleState.waiting_title
        )
        self.assertIn("Как называется", reply.answer.await_args.args[0])

    async def test_arbitrary_session_count_asks_about_day_topics_before_timezone(self) -> None:
        message = _message("5 дней")
        state = AsyncMock()
        state.get_data.return_value = {"event_business_id": BUSINESS_ID}
        with (
            patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"),
            patch.object(lifecycle, "_topics_keyboard", return_value="topics"),
        ):
            await lifecycle.receive_days(message, state)
        state.update_data.assert_awaited_once_with(
            event_days=5,
            event_sessions=[],
            event_session_index=1,
            event_topics=[],
        )
        state.set_state.assert_awaited_once_with(
            lifecycle.ClientPlatformEventLifecycleState.waiting_topics_choice
        )
        self.assertIn("своё название темы", message.answer.await_args.args[0])

    async def test_common_topic_choice_advances_to_timezone(self) -> None:
        message = _message("")
        callback = SimpleNamespace(
            data="cpev:topics:no",
            from_user=SimpleNamespace(id=101),
            answer=AsyncMock(),
            message=message,
        )
        state = AsyncMock()
        state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "event_days": 3,
        }
        with (
            patch.object(lifecycle.control, "_callback_message", return_value=message),
            patch.object(lifecycle, "_timezone_keyboard", return_value="timezone"),
        ):
            await lifecycle.choose_common_event_topic(callback, state)
        state.update_data.assert_awaited_once_with(event_topics=[])
        state.set_state.assert_awaited_once_with(
            lifecycle.ClientPlatformEventLifecycleState.waiting_timezone
        )
        self.assertIn("По какому времени", message.answer.await_args.args[0])

    async def test_named_topics_require_one_title_per_day(self) -> None:
        message = _message("Первая тема\nВторая тема\nТретья тема")
        state = AsyncMock()
        state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "event_days": 3,
        }
        with patch.object(lifecycle, "_timezone_keyboard", return_value="timezone"):
            await lifecycle.receive_event_topics(message, state)
        state.update_data.assert_awaited_once_with(
            event_topics=["Первая тема", "Вторая тема", "Третья тема"]
        )
        state.set_state.assert_awaited_once_with(
            lifecycle.ClientPlatformEventLifecycleState.waiting_timezone
        )
        self.assertIn("По какому времени", message.answer.await_args.args[0])

    async def test_multiday_room_cannot_be_deferred(self) -> None:
        message = _message("-")
        state = AsyncMock()
        state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "event_days": 3,
            "event_session_index": 1,
            "event_timezone": "Europe/Moscow",
            "event_sessions": [],
            "pending_session": {
                "position": 1,
                "starts_at": "2026-09-25T16:00:00+00:00",
                "ends_at": "2026-09-25T18:00:00+00:00",
                "local_label": "25.09.2026 19:00–21:00",
            },
        }
        with patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"):
            await lifecycle.receive_session_url(message, state)
        self.assertIn("многодневного", message.answer.await_args.args[0].casefold())

    async def test_three_session_submit_builds_atomic_session_requests(self) -> None:
        state = AsyncMock()
        state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "event_title": "Три эфира",
            "event_timezone": "Europe/Moscow",
            "event_days": 3,
            "event_sessions": [
                {
                    "position": index,
                    "starts_at": datetime(2026, 9, 24 + index, 16, tzinfo=timezone.utc).isoformat(),
                    "ends_at": datetime(2026, 9, 24 + index, 18, tzinfo=timezone.utc).isoformat(),
                    "local_label": f"{24 + index}.09.2026 19:00–21:00",
                    "join_url": f"https://room-{index}.example.test/live",
                }
                for index in range(1, 4)
            ],
        }
        message = _message("unused")
        actor = MagicMock(unsafe=True)
        created = SimpleNamespace(
            event_id=EVENT_ID,
            provider_key="external",
            join_ready=True,
            registration_url=lambda base: f"{base}/e/public-slug",
        )
        with (
            patch.object(lifecycle.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(
                lifecycle,
                "create_and_publish_multisession_online_event",
                return_value=created,
            ) as create,
            patch.object(lifecycle, "_public_base_url", return_value="https://clientplatform.example.test"),
            patch.object(lifecycle, "_begin_warmup_choice", new=AsyncMock()) as warmup,
        ):
            await lifecycle._create_configured_event(message, state)
        request = create.call_args.kwargs["request"]
        self.assertEqual(len(request.sessions), 3)
        self.assertEqual(request.sessions[2].join_url, "https://room-3.example.test/live")
        self.assertEqual(request.sessions[0].ends_at.hour, 18)
        warmup.assert_awaited_once()

    async def test_day_topics_are_persisted_in_canonical_event_description(self) -> None:
        message = _message("")
        state = AsyncMock()
        state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "event_title": "Большой вебинар",
            "event_timezone": "Europe/Moscow",
            "event_days": 1,
            "event_topics": ["Практика первого дня"],
            "event_sessions": [
                {
                    "position": 1,
                    "starts_at": "2026-10-20T16:00:00+00:00",
                    "ends_at": "2026-10-20T18:00:00+00:00",
                    "local_label": "20.10.2026 19:00–21:00",
                    "join_url": "https://room.example.test/live",
                }
            ],
        }
        actor = object()
        created = SimpleNamespace(
            event_id="event-1",
            provider_key="external",
            join_ready=True,
            registration_url=lambda base: f"{base}/e/event-1",
        )
        with (
            patch.object(lifecycle.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(lifecycle, "create_and_publish_online_event", return_value=created) as create,
            patch.object(lifecycle, "_public_base_url", return_value="https://clientplatform.example.test"),
            patch.object(lifecycle, "_begin_warmup_choice", new=AsyncMock()),
        ):
            await lifecycle._create_configured_event(message, state)

        request = create.call_args.kwargs["request"]
        self.assertEqual(
            request.description,
            "Программа по дням:\nДень 1: Практика первого дня",
        )


    def test_lifecycle_router_is_composed_before_legacy_events_router(self) -> None:
        source = open("handlers/__init__.py", encoding="utf-8").read()
        lifecycle_pos = source.index('".clientplatform_event_lifecycle"')
        legacy_pos = source.index('".clientplatform_events"')
        self.assertLess(lifecycle_pos, legacy_pos)


if __name__ == "__main__":
    unittest.main()
