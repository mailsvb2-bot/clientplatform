from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers import clientplatform_goal_driven_experience as goal


class _Message:
    def __init__(self) -> None:
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


class _Callback:
    def __init__(self, data: str, message: _Message) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=101)
        self.message = message
        self.answer = AsyncMock()


class GoalDrivenOfferingPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_more_than_one_page_keeps_later_offerings_reachable(self) -> None:
        target = _Message()
        offerings = [
            SimpleNamespace(id=f"offering-{index}", title=f"Услуга {index:02d}")
            for index in range(18)
        ]
        with patch.object(goal.control, "_uuid_token", side_effect=lambda value: value):
            await goal._show_offering_page(
                target,
                business_token="business-1",
                offerings=offerings,
                page=0,
            )
            await goal._show_offering_page(
                target,
                business_token="business-1",
                offerings=offerings,
                page=2,
            )

        first_text, first_kwargs = target.answers[0]
        last_text, last_kwargs = target.answers[1]
        first_buttons = [
            button
            for row in first_kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        last_buttons = [
            button
            for row in last_kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("страница 1/3", first_text)
        self.assertTrue(any(button.text == "Дальше ➡️" for button in first_buttons))
        self.assertIn("страница 3/3", last_text)
        self.assertTrue(any(button.text == "🎯 Услуга 16" for button in last_buttons))
        self.assertTrue(any(button.text == "🎯 Услуга 17" for button in last_buttons))
        self.assertTrue(any(button.text == "⬅️ Назад" for button in last_buttons))

    async def test_page_callback_reloads_current_offerings(self) -> None:
        target = _Message()
        callback = _Callback("cpo:offers:business-1:1", target)
        offerings = [
            SimpleNamespace(id=f"offering-{index}", title=f"Услуга {index:02d}")
            for index in range(10)
        ]
        with (
            patch.object(goal.control, "_token_uuid", side_effect=lambda value: value),
            patch.object(goal.control, "_uuid_token", side_effect=lambda value: value),
            patch.object(goal.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(goal.control, "_callback_message", return_value=target),
            patch.object(goal, "_selectable_offerings", new=AsyncMock(return_value=offerings)),
        ):
            await goal.change_goal_offering_page(callback)

        callback.answer.assert_awaited_once_with()
        text, kwargs = target.answers[-1]
        self.assertIn("страница 2/2", text)
        labels = [
            button.text
            for row in kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("🎯 Услуга 08", labels)
        self.assertIn("🎯 Услуга 09", labels)
        self.assertIn("⬅️ Назад", labels)


if __name__ == "__main__":
    unittest.main()
