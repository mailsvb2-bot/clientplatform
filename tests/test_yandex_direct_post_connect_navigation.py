from __future__ import annotations

import unittest

from handlers.clientplatform_goal_first_safety import ACQUIRE_CLIENTS
from handlers.clientplatform_yandex_screen_code import (
    _connected_account_keyboard,
    _connected_account_message,
)


_BUSINESS_TOKEN = "12345678-1234-5678-1234-567812345678"


class YandexDirectPostConnectNavigationTests(unittest.TestCase):
    def test_connected_account_primary_action_enters_canonical_client_acquisition(self) -> None:
        keyboard = _connected_account_keyboard(_BUSINESS_TOKEN)

        self.assertEqual(keyboard.inline_keyboard[0][0].text, ACQUIRE_CLIENTS.label)
        self.assertEqual(
            keyboard.inline_keyboard[0][0].callback_data,
            ACQUIRE_CLIENTS.callback(_BUSINESS_TOKEN),
        )
        self.assertEqual(
            keyboard.inline_keyboard[1][0].callback_data,
            f"cpa:home:{_BUSINESS_TOKEN}",
        )
        for row in keyboard.inline_keyboard:
            for button in row:
                if button.callback_data:
                    self.assertLessEqual(len(button.callback_data.encode("utf-8")), 64)

    def test_connected_account_message_explains_next_steps_without_provider_jargon(self) -> None:
        message = _connected_account_message("owner-login")

        self.assertIn("✅ Яндекс Директ подключён", message)
        self.assertIn("Кабинет: owner-login", message)
        self.assertIn("Что делать дальше", message)
        self.assertIn(ACQUIRE_CLIENTS.label, message)
        self.assertIn("где искать клиентов", message)
        self.assertIn("какой бюджет допустим", message)
        self.assertIn("Без отдельного подтверждения", message)
        self.assertNotIn("читать кампании", message)
        self.assertNotIn("DRAFT", message)
        self.assertNotIn("CampaignId", message)


if __name__ == "__main__":
    unittest.main()
