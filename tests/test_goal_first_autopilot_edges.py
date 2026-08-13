from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers import clientplatform_goal_first_autopilot as goal


class FakeState:
    def __init__(self, data=None) -> None:
        self.state = None
        self.data = dict(data or {})

    async def set_state(self, value):
        self.state = value

    async def set_data(self, value):
        self.data = dict(value)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.state = None
        self.data.clear()


class GoalFirstAutopilotEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_saved_region_asks_only_for_region(self) -> None:
        out = SimpleNamespace(answer=AsyncMock())
        callback = SimpleNamespace(from_user=SimpleNamespace(id=101), message=out)
        data = {
            "business_id": "business-1",
            "business_token": "business-1",
            "slot_id": "slot-1",
            "connection_id": "connection-1",
        }
        state = FakeState(data)
        with (
            patch.object(goal.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(goal.control, "_callback_message", return_value=out),
            patch.object(goal.one_click, "list_ad_publications", return_value=[]),
        ):
            await goal._choose_goal_region(callback, state, data=data)
        self.assertEqual(state.state, goal.one_click.OneClickOwnerState.waiting_region)
        text = out.answer.await_args.args[0]
        self.assertIn("где искать клиентов", text.lower())
        self.assertNotIn("выберите кампанию", text.lower())

    async def test_saved_region_reuses_account_level_setting(self) -> None:
        out = SimpleNamespace(answer=AsyncMock())
        callback = SimpleNamespace(from_user=SimpleNamespace(id=101), message=out)
        data = {
            "business_id": "business-1",
            "business_token": "business-1",
            "slot_id": "slot-1",
            "connection_id": "connection-1",
        }
        saved = SimpleNamespace(connection_id="connection-1", region_ids=(47,))
        prepare = AsyncMock()
        with (
            patch.object(goal.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(goal.one_click, "list_ad_publications", return_value=[saved]),
            patch.object(goal, "_prepare_goal_result", new=prepare),
        ):
            await goal._choose_goal_region(callback, FakeState(data), data=data)
        prepare.assert_awaited_once()
        self.assertEqual(prepare.await_args.kwargs["region_ids"], (47,))

    async def test_composed_draft_path_has_no_campaign_selector(self) -> None:
        self.assertIs(goal.one_click._prepare_draft, goal._prepare_goal_result)
        self.assertFalse(hasattr(goal.one_click, "_choose_campaign"))
        self.assertTrue(goal.one_click._managed_campaign_goal_first_installed)


if __name__ == "__main__":
    unittest.main()
