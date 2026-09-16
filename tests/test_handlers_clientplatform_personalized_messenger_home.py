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

class TelegramComposedOwnerDashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_composed_owner_home_uses_shared_quick_menu(self) -> None:
        from unittest.mock import AsyncMock
        import handlers

        # Exercise the same lazy composition path used by production rather than
        # relying on another test to have composed handlers first.
        handlers._load_clientplatform_modules()
        import handlers.clientplatform_goal_dashboard as goal
        import handlers.clientplatform_owner_journey as owner

        business_id = str(uuid4())
        actor = SimpleNamespace(role=PlatformRole.OWNER)
        access = SimpleNamespace(business=SimpleNamespace(name="Практика", id=business_id))
        profile = SimpleNamespace(activity_description="Психолог, консультации и вебинары")
        capabilities = [SimpleNamespace(connector_key="consultations", status=CapabilityStatus.ACTIVE)]
        message = SimpleNamespace(answer=AsyncMock())
        markup = object()
        next_action = SimpleNamespace(
            action_key="none", title="Нет срочных задач", reason="Всё спокойно"
        )
        self.assertIs(owner.send_owner_dashboard, goal.send_goal_dashboard)
        with (
            patch.object(
                goal.one_click.simple,
                "_business_snapshot",
                new=AsyncMock(return_value=(actor, access, profile, capabilities, [], [], [])),
            ),
            patch.object(goal.asyncio, "to_thread", new=AsyncMock(return_value=next_action)),
            patch.object(goal.one_click.simple, "_simple_keyboard", return_value=markup) as quick,
            patch.object(goal, "_goal_keyboard") as legacy,
        ):
            await owner.send_owner_dashboard(message, user_id=101, business_id=business_id)

        quick.assert_called_once_with(
            business_id,
            activity_description=profile.activity_description,
            capabilities=capabilities,
            role=PlatformRole.OWNER,
        )
        legacy.assert_not_called()
        self.assertIs(message.answer.await_args.kwargs["reply_markup"], markup)
        text = message.answer.await_args.args[0]
        self.assertIn("Быстрые действия подобраны под этот бизнес", text)
        self.assertIn("Главное сейчас", text)
        self.assertNotIn("Не знаете, что нажать?", text)


if __name__ == "__main__":
    unittest.main()
