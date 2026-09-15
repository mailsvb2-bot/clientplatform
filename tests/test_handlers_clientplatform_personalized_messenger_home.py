from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from clientplatform.domain.activity import CapabilityStatus
from clientplatform.domain.tenancy import PlatformRole

import handlers.clientplatform_simple_experience as simple


class TelegramPersonalizedMessengerHomeTests(unittest.TestCase):
    def test_telegram_uses_business_aware_quick_actions_and_mini_app_escape(self) -> None:
        business_id = str(uuid4())
        capabilities = [
            SimpleNamespace(
                connector_key="consultations",
                status=CapabilityStatus.ACTIVE,
            ),
            SimpleNamespace(
                connector_key="programs",
                status=CapabilityStatus.ACTIVE,
            ),
        ]
        with patch.object(
            simple,
            "cockpit_web_app_url",
            return_value="https://app.clientplatform.test/clientplatform/cockpit",
        ):
            markup = simple._simple_keyboard(
                business_id,
                activity_description=(
                    "Я психолог. Провожу консультации, вебинары и обучающие программы."
                ),
                capabilities=capabilities,
                role=PlatformRole.OWNER,
            )

        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertEqual(
            labels,
            [
                "💬 Клиенты и обращения",
                "📅 Записать на консультацию",
                "🎥 Провести вебинар",
                "🎓 Материалы и программы",
                "👥 Найти клиентов",
                "📊 Результаты",
                "▦ Все возможности",
            ],
        )
        last = markup.inline_keyboard[-1][0]
        self.assertIsNone(last.callback_data)
        self.assertEqual(
            last.web_app.url,
            "https://app.clientplatform.test/clientplatform/cockpit",
        )


if __name__ == "__main__":
    unittest.main()
