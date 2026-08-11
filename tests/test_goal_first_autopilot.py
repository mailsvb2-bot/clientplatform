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
    async def test_home_exposes_one_primary_goal_and_optional_secondary_navigation(self) -> None:
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
        self.assertIn("Главное действие", text)
        self.assertIn("Технические кабинеты", text)
        self.assertIn("свою картинку или видео", text)
        self.assertIn("свободных времён: 1", text)
        self.assertEqual(
            labels,
            ["🚀 Получить клиентов", "👥 Клиенты и запись", "⚙️ Ещё"],
        )
        self.assertEqual(
            markup.inline_keyboard[0][0].callback_data,
            "cpo:start:business-1",
        )

    async def test_first_region_question_is_business_language(self) -> None:
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
            patch.object(goal.asyncio, "to_thread", side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
        ):
            await goal._choose_goal_region(
                callback,
                state,
                data=data,
                campaign_id="6001",
                campaign_name="Campaign",
            )
        self.assertEqual(state.state, goal.one_click.OneClickOwnerState.waiting_region)
        self.assertIn("Осталось только указать регион", out.answer.await_args.args[0])
        self.assertIn("где искать клиентов", out.answer.await_args.args[0].lower())
        labels = [
            button.text
            for row in out.answer.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("Другой город", labels)
        self.assertNotIn("Другой регион", labels)

    async def test_saved_region_skips_question(self) -> None:
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
            external_campaign_id="6001",
            region_ids=(47,),
        )
        with (
            patch.object(
                goal.control,
                "_actor",
                new=AsyncMock(return_value="actor"),
            ),
            patch.object(goal.one_click, "list_ad_publications", return_value=[job]),
            patch.object(goal.asyncio, "to_thread", side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
            patch.object(
                goal,
                "_prepare_goal_result",
                new=AsyncMock(),
            ) as prepare,
        ):
            await goal._choose_goal_region(
                callback,
                state,
                data=data,
                campaign_id="6001",
                campaign_name="Campaign",
            )
        prepare.assert_awaited_once()
        self.assertEqual(prepare.await_args.kwargs["region_ids"], (47,))

    async def test_install_overlays_existing_orchestration_and_composes_goal_router(self) -> None:
        owner = SimpleNamespace()
        simple = SimpleNamespace(
            router=SimpleNamespace(include_router=lambda _router: None)
        )
        control = SimpleNamespace()
        previous_prepare = goal.one_click._prepare_draft
        previous_choose = goal.one_click._choose_campaign
        previous_home = goal.one_click._home_keyboard
        try:
            goal.install_goal_first_autopilot(
                owner_module=owner,
                simple_module=simple,
                control_module=control,
            )
            self.assertIs(owner.send_owner_dashboard, goal.send_goal_dashboard)
            self.assertIs(simple.send_simple_dashboard, goal.send_goal_dashboard)
            self.assertIs(control._send_dashboard, goal.send_goal_dashboard)
            self.assertIs(goal.one_click._prepare_draft, goal._prepare_goal_result)
            self.assertIs(goal.one_click._choose_campaign, goal._choose_goal_region)
            self.assertTrue(simple._goal_first_autopilot_composed)
        finally:
            goal.one_click._prepare_draft = previous_prepare
            goal.one_click._choose_campaign = previous_choose
            goal.one_click._home_keyboard = previous_home


if __name__ == "__main__":
    unittest.main()
