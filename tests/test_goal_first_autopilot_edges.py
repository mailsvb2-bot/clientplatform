from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from clientplatform.domain.ad_connections import AdConnectionError
from clientplatform.domain.promotions import PromotionError
from handlers import clientplatform_goal_first_autopilot as goal


class FakeState:
    def __init__(self) -> None:
        self.state = None
        self.data = {}
        self.cleared = False

    async def set_state(self, value):
        self.state = value

    async def set_data(self, value):
        self.data = dict(value)

    async def clear(self):
        self.cleared = True
        self.data.clear()


async def direct(function, *args, **kwargs):
    return function(*args, **kwargs)


def event(target):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=101),
        message=target,
        bot=SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(username="clientplatform_bot"))
        ),
    )


def base_data():
    return {
        "business_id": "business-1",
        "business_token": "business-1",
        "slot_id": "slot-1",
        "connection_id": "connection-1",
        "external_campaign_id": "6001",
        "external_campaign_name": "Campaign",
    }


def promotion():
    return SimpleNamespace(
        campaign=SimpleNamespace(
            id="promotion-1",
            source_token="source-1",
        )
    )


def draft():
    return SimpleNamespace(
        campaign_name="Technical campaign name",
        job=SimpleNamespace(
            id="job-1",
            title="Есть свободное время",
            text="Запишитесь на консультацию",
        ),
    )


class GoalFirstAutopilotEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepared_result_hides_campaign_mechanics_and_stops_before_spend(self):
        target = SimpleNamespace(answer=AsyncMock())
        state = FakeState()
        with (
            patch.object(goal.asyncio, "to_thread", new=direct),
            patch.object(goal.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(goal.one_click, "create_slot_promotion", return_value=promotion()),
            patch.object(goal.one_click, "create_ad_publication_draft", return_value=draft()),
            patch.object(goal.one_click, "_target", return_value=target),
            patch.object(goal.one_click, "_username", new=AsyncMock(return_value="clientplatform_bot")),
        ):
            await goal._prepare_goal_result(
                event(target),
                state,
                data=base_data(),
                region_ids=(47,),
            )
        self.assertEqual(state.state, goal.ad.AdConnectionState.confirming_publication)
        self.assertEqual(state.data["job_id"], "job-1")
        text = target.answer.await_args.args[0]
        self.assertIn("✅ Всё готово", text)
        self.assertIn("Пока ничего не запущено", text)
        self.assertNotIn("Technical campaign name", text)
        labels = [
            button.text
            for row in target.answer.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertEqual(
            labels,
            [
                "✨ Добавить красивую картинку",
                "🚀 Запустить рекламу",
                "✏️ Изменить",
                "🏠 Не запускать",
            ],
        )

    async def test_promotion_failure_uses_existing_safe_failure_boundary(self):
        target = SimpleNamespace(answer=AsyncMock())
        state = FakeState()
        with (
            patch.object(goal.asyncio, "to_thread", new=direct),
            patch.object(goal.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(
                goal.one_click,
                "create_slot_promotion",
                side_effect=PromotionError("no promotion"),
            ),
            patch.object(goal.one_click, "_draft_failure", new=AsyncMock()) as failure,
        ):
            await goal._prepare_goal_result(
                event(target),
                state,
                data=base_data(),
                region_ids=(47,),
            )
        failure.assert_awaited_once()

    async def test_draft_failure_uses_existing_safe_failure_boundary(self):
        target = SimpleNamespace(answer=AsyncMock())
        state = FakeState()
        with (
            patch.object(goal.asyncio, "to_thread", new=direct),
            patch.object(goal.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(goal.one_click, "create_slot_promotion", return_value=promotion()),
            patch.object(
                goal.one_click,
                "create_ad_publication_draft",
                side_effect=AdConnectionError("provider unavailable"),
            ),
            patch.object(goal.one_click, "_username", new=AsyncMock(return_value="clientplatform_bot")),
            patch.object(goal.one_click, "_draft_failure", new=AsyncMock()) as failure,
        ):
            await goal._prepare_goal_result(
                event(target),
                state,
                data=base_data(),
                region_ids=(47,),
            )
        failure.assert_awaited_once()

    async def test_invalid_bot_username_uses_safe_failure_boundary(self):
        target = SimpleNamespace(answer=AsyncMock())
        state = FakeState()
        with (
            patch.object(goal.asyncio, "to_thread", new=direct),
            patch.object(goal.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(goal.one_click, "create_slot_promotion", return_value=promotion()),
            patch.object(goal.one_click, "_username", new=AsyncMock(side_effect=RuntimeError("missing"))),
            patch.object(goal.one_click, "_draft_failure", new=AsyncMock()) as failure,
        ):
            await goal._prepare_goal_result(
                event(target),
                state,
                data=base_data(),
                region_ids=(47,),
            )
        failure.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
