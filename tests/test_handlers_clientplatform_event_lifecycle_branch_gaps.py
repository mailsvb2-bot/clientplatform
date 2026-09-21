from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from handlers import clientplatform_event_lifecycle as lifecycle


BUSINESS_ID = "11111111-1111-4111-8111-111111111111"
EVENT_ID = "33333333-3333-4333-8333-333333333333"
TOKEN = "ERERERERQRGBEREREREREQ"


def message(text: str = "") -> SimpleNamespace:
    return SimpleNamespace(text=text, from_user=SimpleNamespace(id=101), answer=AsyncMock())


def callback(data: str) -> SimpleNamespace:
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=101), answer=AsyncMock())


class WebinarLifecycleBranchGapTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_and_noop_are_explicit_safe_exits(self) -> None:
        msg = message()
        state = AsyncMock()
        with (
            patch.object(lifecycle.control, "_uuid_token", return_value="tok"),
            patch.object(lifecycle.control, "_keyboard", return_value="kb"),
        ):
            await lifecycle._cancel(msg, state, BUSINESS_ID)
        state.clear.assert_awaited_once()
        self.assertIn("отменено", msg.answer.await_args.args[0])

        cb = callback("cpev:noop")
        await lifecycle.ignore_event_picker_noop(cb)
        cb.answer.assert_awaited_once_with()

    async def test_start_wizard_success_path(self) -> None:
        cb = callback(f"cpev:new:{TOKEN}")
        state = AsyncMock()
        reply = message()
        actor = MagicMock(unsafe=True)
        with (
            patch.object(lifecycle.control, "_token_uuid", return_value=BUSINESS_ID),
            patch.object(lifecycle.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(lifecycle.control, "_callback_message", return_value=reply),
            patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"),
        ):
            await lifecycle.start_multisession_event_wizard(cb, state)
        actor.assert_can_manage_business.assert_called_once()
        state.clear.assert_awaited_once()
        state.update_data.assert_awaited_once_with(event_business_id=BUSINESS_ID)
        state.set_state.assert_awaited_once_with(lifecycle.ClientPlatformEventLifecycleState.waiting_title)
        self.assertIn("Как называется", reply.answer.await_args.args[0])

    async def test_open_webinar_live_room_rejects_stale_callback(self) -> None:
        cb = callback("cpev:conduct:broken")
        await lifecycle.open_webinar_live_room(cb)
        cb.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

    async def test_open_webinar_live_room_requires_ready_link(self) -> None:
        cb = callback("cpev:conduct:event-token:business-token")
        actor = object()
        live = SimpleNamespace(
            title="Вебинар",
            sessions=(
                SimpleNamespace(
                    position=1,
                    local_start="25.09.2026 19:00",
                    join_ready=False,
                    join_url=None,
                ),
            ),
        )
        with (
            patch.object(
                lifecycle.control,
                "_token_uuid",
                side_effect=(EVENT_ID, BUSINESS_ID),
            ),
            patch.object(
                lifecycle.control,
                "_actor",
                new=AsyncMock(return_value=actor),
            ),
            patch.object(
                lifecycle.asyncio,
                "to_thread",
                new=AsyncMock(return_value=live),
            ),
        ):
            await lifecycle.open_webinar_live_room(cb)
        cb.answer.assert_awaited_once_with(
            "Сначала добавьте ссылку на эфир",
            show_alert=True,
        )

    async def test_open_webinar_live_room_renders_single_and_multiday_rooms(self) -> None:
        actor = object()
        single = SimpleNamespace(
            title="Один эфир",
            sessions=(
                SimpleNamespace(
                    position=1,
                    local_start="25.09.2026 19:00",
                    join_ready=True,
                    join_url="https://room.example.test/one",
                ),
            ),
        )
        multi = SimpleNamespace(
            title="Два эфира",
            sessions=(
                SimpleNamespace(
                    position=1,
                    local_start="25.09.2026 19:00",
                    join_ready=True,
                    join_url="https://room.example.test/one",
                ),
                SimpleNamespace(
                    position=2,
                    local_start="26.09.2026 19:00",
                    join_ready=True,
                    join_url="https://room.example.test/two",
                ),
            ),
        )
        for live, expected_label in (
            (single, "▶️ Открыть эфир"),
            (multi, "▶️ Открыть день 1"),
        ):
            with self.subTest(title=live.title):
                cb = callback("cpev:conduct:event-token:business-token")
                reply = message()
                with (
                    patch.object(
                        lifecycle.control,
                        "_token_uuid",
                        side_effect=(EVENT_ID, BUSINESS_ID),
                    ),
                    patch.object(
                        lifecycle.control,
                        "_uuid_token",
                        return_value="business-token",
                    ),
                    patch.object(
                        lifecycle.control,
                        "_actor",
                        new=AsyncMock(return_value=actor),
                    ),
                    patch.object(
                        lifecycle.asyncio,
                        "to_thread",
                        new=AsyncMock(return_value=live),
                    ),
                    patch.object(
                        lifecycle.control,
                        "_callback_message",
                        return_value=reply,
                    ),
                ):
                    await lifecycle.open_webinar_live_room(cb)

                cb.answer.assert_awaited_once_with()
                rendered_text = reply.answer.await_args.args[0]
                markup = reply.answer.await_args.kwargs["reply_markup"]
                self.assertIn(live.title, rendered_text)
                self.assertIn("День 1: 25.09.2026 19:00", rendered_text)
                self.assertEqual(
                    markup.inline_keyboard[0][0].text,
                    expected_label,
                )
                self.assertEqual(
                    markup.inline_keyboard[0][0].url,
                    "https://room.example.test/one",
                )
                self.assertEqual(
                    markup.inline_keyboard[-1][0].text,
                    lifecycle.BACK_TO_EVENTS_LABEL,
                )
                if len(live.sessions) > 1:
                    self.assertEqual(
                        markup.inline_keyboard[1][0].text,
                        "▶️ Открыть день 2",
                    )
                    self.assertIn("День 2: 26.09.2026 19:00", rendered_text)

    async def test_open_webinar_live_room_fails_closed_for_resolution_errors(self) -> None:
        actor = object()
        for error in (
            lifecycle.TenantPermissionDenied("denied"),
            LookupError("missing"),
            ValueError("invalid"),
            RuntimeError("unavailable"),
        ):
            with self.subTest(error=type(error).__name__):
                cb = callback("cpev:conduct:event-token:business-token")
                with (
                    patch.object(
                        lifecycle.control,
                        "_token_uuid",
                        side_effect=(EVENT_ID, BUSINESS_ID),
                    ),
                    patch.object(
                        lifecycle.control,
                        "_actor",
                        new=AsyncMock(return_value=actor),
                    ),
                    patch.object(
                        lifecycle.asyncio,
                        "to_thread",
                        new=AsyncMock(side_effect=error),
                    ),
                ):
                    await lifecycle.open_webinar_live_room(cb)
                cb.answer.assert_awaited_once_with(
                    "Не удалось открыть эфир этого вебинара",
                    show_alert=True,
                )

    async def test_receive_days_covers_missing_cancel_and_valid(self) -> None:
        missing = AsyncMock()
        missing.get_data.return_value = {}
        missing_msg = message("2")
        await lifecycle.receive_days(missing_msg, missing)
        missing.clear.assert_awaited_once()

        cancelled = AsyncMock()
        cancelled.get_data.return_value = {"event_business_id": BUSINESS_ID}
        with patch.object(lifecycle, "_cancel", new=AsyncMock()) as cancel:
            await lifecycle.receive_days(message("cancel"), cancelled)
        cancel.assert_awaited_once()

        valid = AsyncMock()
        valid.get_data.return_value = {"event_business_id": BUSINESS_ID}
        valid_msg = message("3")
        with patch.object(lifecycle, "_topics_keyboard", return_value="topics"):
            await lifecycle.receive_days(valid_msg, valid)
        valid.update_data.assert_awaited_once_with(
            event_days=3,
            event_sessions=[],
            event_session_index=1,
            event_topics=[],
        )
        valid.set_state.assert_awaited_once_with(
            lifecycle.ClientPlatformEventLifecycleState.waiting_topics_choice
        )
        self.assertIn("название темы", valid_msg.answer.await_args.args[0])

    async def test_receive_timezone_covers_missing_cancel_and_valid(self) -> None:
        missing = AsyncMock()
        missing.get_data.return_value = {}
        await lifecycle.receive_timezone(message("Москва"), missing)
        missing.clear.assert_awaited_once()

        cancelled = AsyncMock()
        cancelled.get_data.return_value = {"event_business_id": BUSINESS_ID}
        with patch.object(lifecycle, "_cancel", new=AsyncMock()) as cancel:
            await lifecycle.receive_timezone(message("отмена"), cancelled)
        cancel.assert_awaited_once()

        valid = AsyncMock()
        valid.get_data.return_value = {"event_business_id": BUSINESS_ID}
        with patch.object(lifecycle, "_accept_timezone", new=AsyncMock()) as accept:
            await lifecycle.receive_timezone(message("Europe/Amsterdam"), valid)
        accept.assert_awaited_once()

    async def test_accept_timezone_success_prompts_venue(self) -> None:
        state = AsyncMock()
        state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "event_days": 2,
        }
        msg = message()
        with patch.object(lifecycle, "_prompt_venue", new=AsyncMock()) as prompt:
            await lifecycle._accept_timezone(msg, state, "Москва")
        prompt.assert_awaited_once_with(
            msg,
            state,
            business_id=BUSINESS_ID,
            timezone_name="Europe/Moscow",
        )

    async def test_prompt_session_date_respects_previous_session_date(self) -> None:
        previous = lifecycle.EventWizardSession(
            position=1,
            starts_at=datetime(2026, 9, 25, 16, tzinfo=timezone.utc),
            ends_at=datetime(2026, 9, 25, 18, tzinfo=timezone.utc),
            local_label="25.09.2026 19:00–21:00",
            join_url="https://room.example/one",
        )
        state = AsyncMock()
        state.get_data.return_value = {
            "event_sessions": [lifecycle._session_payload(previous, join_url=previous.join_url)]
        }
        msg = message()
        with (
            patch.object(lifecycle, "local_today", return_value=date(2026, 9, 18)),
            patch.object(lifecycle, "_calendar_keyboard", return_value="calendar") as calendar,
        ):
            await lifecycle._prompt_session_date(
                msg,
                state,
                business_id=BUSINESS_ID,
                position=2,
                total=3,
                timezone_name="Europe/Moscow",
            )
        self.assertEqual(state.update_data.await_args.kwargs["event_picker_min_date"], "2026-09-25")
        self.assertEqual(calendar.call_args.kwargs["minimum_date"], date(2026, 9, 25))

    async def test_begin_warmup_choice_records_window_and_prompts(self) -> None:
        state = AsyncMock()
        msg = message()
        actor = object()
        window = SimpleNamespace(days_until_event=7, max_warmup_days=7)
        with (
            patch.object(lifecycle, "get_event_warmup_window", return_value=window),
            patch.object(lifecycle, "_cancel_keyboard", return_value="cancel"),
        ):
            await lifecycle._begin_warmup_choice(
                msg,
                state,
                actor=actor,
                business_id=BUSINESS_ID,
                event_id=EVENT_ID,
                title="Вебинар",
                local_times=("25.09.2026 19:00–21:00",),
                registration_url="https://cp.example/e/demo",
                provider_key="zoom",
                join_ready=True,
            )
        self.assertEqual(state.update_data.await_args.kwargs["max_warmup_days"], 7)
        state.set_state.assert_awaited_once_with(
            lifecycle.ClientPlatformEventLifecycleState.waiting_warmup_days
        )
        self.assertIn("До первого дня — 7", msg.answer.await_args.args[0])

    def test_venue_keyboard_handles_odd_catalog_length(self) -> None:
        original = lifecycle.WEBINAR_VENUES
        one = original[:1]
        with (
            patch.object(lifecycle, "WEBINAR_VENUES", one),
            patch.object(lifecycle.control, "_uuid_token", return_value="tok"),
            patch.object(lifecycle.control, "_keyboard", side_effect=lambda rows: rows),
        ):
            rows = lifecycle._venue_keyboard(BUSINESS_ID)
        self.assertEqual(len(rows[0]), 1)
        self.assertEqual(rows[0][0][0], one[0].label)
        self.assertEqual(len(rows[-1]), 1)


if __name__ == "__main__":
    unittest.main()
