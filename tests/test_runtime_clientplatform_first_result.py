from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import handlers
from handlers import clientplatform_first_result as first
from handlers import clientplatform_owner_journey as owner


_BUSINESS_ID = "00000000-0000-0000-0000-000000000301"


class _State:
    def __init__(self) -> None:
        self.cleared = 0

    async def clear(self) -> None:
        self.cleared += 1


class _Message:
    def __init__(self) -> None:
        self.answers: list[tuple[str, object | None]] = []

    async def answer(self, text: str, reply_markup=None) -> None:
        self.answers.append((text, reply_markup))


class _Callback:
    def __init__(self) -> None:
        token = first.control._uuid_token(_BUSINESS_ID)
        self.data = f"cps:firstgoal:{token}"
        self.from_user = SimpleNamespace(id=101)
        self.message = _Message()
        self.answers = 0

    async def answer(self, *args, **kwargs) -> None:
        self.answers += 1


class ClientPlatformFirstResultUiTests(unittest.IsolatedAsyncioTestCase):
    def test_composed_owner_dashboard_keeps_one_primary_action_and_full_menu_entry(self) -> None:
        handlers._load_clientplatform_modules()
        markup = owner._owner_keyboard(_BUSINESS_ID)
        buttons = [button for row in markup.inline_keyboard for button in row]
        self.assertEqual(
            [button.text for button in buttons],
            ["🚀 Найти новых клиентов", "🧭 Все разделы"],
        )
        self.assertTrue(str(buttons[0].callback_data).startswith("cpo:start:"))
        self.assertTrue(str(buttons[1].callback_data).startswith("cpo:more:"))
        self.assertFalse(
            any(str(button.callback_data or "").startswith("cps:next:") for button in buttons)
        )

    async def test_first_result_menu_offers_independent_human_goals(self) -> None:
        callback = _Callback()
        state = _State()
        with (
            patch.object(
                first.control,
                "_actor",
                new=AsyncMock(return_value=SimpleNamespace()),
            ),
            patch.object(
                first.control,
                "_callback_message",
                return_value=callback.message,
            ),
        ):
            await first.choose_first_result(callback, state)

        self.assertEqual(state.cleared, 1)
        text, markup = callback.message.answers[-1]
        self.assertIn("Что Вы хотите получить первым?", text)
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertIn("📅 Принимать записи", labels)
        self.assertIn("📚 Выдавать материалы", labels)
        self.assertIn("👥 Подключить клиента", labels)
        self.assertIn("🤖 Настроить Telegram-бота", labels)
        self.assertNotIn("Создать программу", labels)


if __name__ == "__main__":
    unittest.main()
