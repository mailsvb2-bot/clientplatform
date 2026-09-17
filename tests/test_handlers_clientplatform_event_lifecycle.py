from __future__ import annotations

import unittest
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
        state.update_data.assert_awaited_once_with(event_business_id=BUSINESS_ID)
        state.set_state.assert_awaited_once_with(
            lifecycle.ClientPlatformEventLifecycleState.waiting_title
        )
        self.assertIn("Как называется", reply.answer.await_args.args[0])

    async def test_two_day_choice_advances_to_day_one_time(self) -> None:
        message = _message("2")
        state = AsyncMock()
        state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "event_title": "Два дня практики",
        }
        actor = MagicMock(unsafe=True)
        with (
            patch.object(lifecycle.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(
                lifecycle,
                "get_business_profile",
                return_value=SimpleNamespace(timezone="Europe/Moscow"),
            ),
            patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"),
        ):
            await lifecycle.receive_days(message, state)

        state.update_data.assert_awaited_once_with(
            event_days=2,
            event_timezone="Europe/Moscow",
        )
        state.set_state.assert_awaited_once_with(
            lifecycle.ClientPlatformEventLifecycleState.waiting_day1_time
        )
        self.assertIn("день 1", message.answer.await_args.args[0])

    async def test_second_day_requires_a_distinct_room(self) -> None:
        message = _message("https://room-one.example.test/live")
        state = AsyncMock()
        state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "day1_url": "https://room-one.example.test/live",
        }
        with patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"):
            await lifecycle.receive_day2_url(message, state)

        self.assertIn("другая ссылка", message.answer.await_args.args[0])
        state.clear.assert_not_awaited()

    async def test_two_day_submit_builds_two_atomic_session_requests(self) -> None:
        message = _message("https://room-two.example.test/live")
        state = AsyncMock()
        state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "event_title": "Два дня практики",
            "event_timezone": "Europe/Moscow",
            "day1_starts_at": "2026-09-25T16:00:00+00:00",
            "day1_local_time": "25.09.2026 19:00",
            "day1_url": "https://room-one.example.test/live",
            "day2_starts_at": "2026-09-26T16:00:00+00:00",
            "day2_local_time": "26.09.2026 19:00",
        }
        actor = MagicMock(unsafe=True)
        created = SimpleNamespace(
            event_id=EVENT_ID,
            provider_key="auto",
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
            patch.object(
                lifecycle,
                "_public_base_url",
                return_value="https://clientplatform.example.test",
            ),
            patch.object(lifecycle, "_begin_warmup_choice", new=AsyncMock()) as warmup,
        ):
            await lifecycle.receive_day2_url(message, state)

        request = create.call_args.kwargs["request"]
        self.assertEqual(len(request.sessions), 2)
        self.assertEqual(
            request.sessions[0].join_url,
            "https://room-one.example.test/live",
        )
        self.assertEqual(
            request.sessions[1].join_url,
            "https://room-two.example.test/live",
        )
        self.assertLess(request.sessions[0].starts_at, request.sessions[1].starts_at)
        warmup.assert_awaited_once()

    def test_lifecycle_router_is_composed_before_legacy_events_router(self) -> None:
        source = open("handlers/__init__.py", encoding="utf-8").read()
        lifecycle_pos = source.index('".clientplatform_event_lifecycle"')
        legacy_pos = source.index('".clientplatform_events"')
        self.assertLess(lifecycle_pos, legacy_pos)


if __name__ == "__main__":
    unittest.main()
