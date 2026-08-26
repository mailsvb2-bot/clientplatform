from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from clientplatform.domain.bookings import BookingSlotStatus
from handlers import clientplatform_goal_first_autopilot as goal


class FakeState:
    def __init__(self, data=None) -> None:
        self.data = dict(data or {})
        self.state = None

    async def set_state(self, value):
        self.state = value

    async def set_data(self, value):
        self.data = dict(value)

    async def clear(self):
        self.data.clear()
        self.state = None


class GoalFirstAutopilotTests(unittest.IsolatedAsyncioTestCase):
    async def test_home_exposes_acquisition_sales_and_secondary_navigation(self) -> None:
        out = SimpleNamespace(answer=AsyncMock())
        slot = SimpleNamespace(
            slot=SimpleNamespace(
                status=BookingSlotStatus.OPEN,
                starts_at="2026-08-20T09:00:00+00:00",
            ),
            local_start="20.08.2026 12:00",
            offering_title="Консультация",
        )
        snapshot = (
            "actor",
            SimpleNamespace(business=SimpleNamespace(name="Мой бизнес")),
            SimpleNamespace(activity_description="Помогаю клиентам решать задачи"),
            [],
            [],
            [],
            [slot],
        )
        with (
            patch.object(
                goal.one_click.simple,
                "_business_snapshot",
                new=AsyncMock(return_value=snapshot),
            ),
            patch.object(goal.control, "list_booking_slots", return_value=[slot]),
            patch.object(goal.control, "_uuid_token", side_effect=lambda value: value),
        ):
            await goal.send_goal_dashboard(
                out,
                user_id=101,
                business_id="business-1",
            )
        text = out.answer.await_args.args[0]
        markup = out.answer.await_args.kwargs["reply_markup"]
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertIn("Что нужно сделать сейчас", text)
        self.assertIn("Технические кабинеты", text)
        self.assertIn("сообщения клиентам не отправляются без Вашего подтверждения", text)
        self.assertIn("свободных времён: 1", text)
        self.assertEqual(
            labels,
            [
                "📈 Что сегодня",
                "🚀 Найти новых клиентов",
                "💬 Обращения и продажи",
                "💬 Мессенджеры",
                "👥 Клиенты и запись",
                "⚙️ Ещё",
            ],
        )
        self.assertEqual(
            markup.inline_keyboard[0][0].callback_data,
            "cpg:period:business-1:7",
        )
        self.assertEqual(
            markup.inline_keyboard[1][0].callback_data,
            "cpo:start:business-1",
        )
        self.assertEqual(
            markup.inline_keyboard[2][0].callback_data,
            "cps:s:business-1",
        )
        self.assertEqual(
            markup.inline_keyboard[3][0].callback_data,
            "cpa:business-1:messengers",
        )
        self.assertIn("ВКонтакте, MAX и Telegram", text)

    async def test_first_region_question_does_not_ask_for_yandex_campaign(self) -> None:
        out = SimpleNamespace(answer=AsyncMock())
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=101),
            message=out,
        )
        state = FakeState()
        data = {
            "business_id": "business-1",
            "business_token": "business-1",
            "slot_id": "slot-1",
            "connection_id": "connection-1",
        }
        with (
            patch.object(
                goal.control,
                "_actor",
                new=AsyncMock(return_value="actor"),
            ),
            patch.object(goal.control, "_callback_message", return_value=out),
            patch.object(goal.one_click, "list_ad_publications", return_value=[]),
            patch.object(
                goal.one_click.asyncio,
                "to_thread",
                side_effect=lambda fn, *a, **kw: fn(*a, **kw),
            ),
        ):
            await goal._choose_goal_region(
                callback,
                state,
                data=data,
                campaign_id="legacy-ignored",
                campaign_name="legacy-ignored",
            )
        self.assertEqual(state.state, goal.one_click.OneClickOwnerState.waiting_region)
        text = out.answer.await_args.args[0]
        self.assertIn("Осталось только указать регион", text)
        self.assertIn("создаст и привяжет сам", text)
        labels = [
            button.text
            for row in out.answer.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("Другой регион", labels)
        self.assertNotIn("Campaign", text)

    async def test_saved_region_skips_provider_campaign_selection(self) -> None:
        out = SimpleNamespace(answer=AsyncMock())
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=101),
            message=out,
        )
        state = FakeState()
        data = {
            "business_id": "business-1",
            "business_token": "business-1",
            "slot_id": "slot-1",
            "connection_id": "connection-1",
        }
        job = SimpleNamespace(
            connection_id="connection-1",
            external_campaign_id="old-campaign",
            region_ids=(47,),
        )
        prepare = AsyncMock()
        with (
            patch.object(
                goal.control,
                "_actor",
                new=AsyncMock(return_value="actor"),
            ),
            patch.object(goal.one_click, "list_ad_publications", return_value=[job]),
            patch.object(
                goal.one_click.asyncio,
                "to_thread",
                side_effect=lambda fn, *a, **kw: fn(*a, **kw),
            ),
            patch.object(goal.one_click, "_prepare_draft", new=prepare),
        ):
            await goal._choose_goal_region(
                callback,
                state,
                data=data,
                campaign_id="legacy-ignored",
                campaign_name="legacy-ignored",
            )
        prepare.assert_awaited_once()
        self.assertEqual(prepare.await_args.kwargs["region_ids"], (47,))
        self.assertNotIn("external_campaign_id", prepare.await_args.kwargs["data"])

    async def test_production_composition_keeps_managed_draft_owner(self) -> None:
        self.assertIs(goal.one_click._prepare_draft, goal._prepare_goal_result)
        self.assertFalse(hasattr(goal.one_click, "_choose_campaign"))
        self.assertTrue(goal.one_click._managed_campaign_goal_first_installed)


if __name__ == "__main__":
    unittest.main()