from __future__ import annotations

import importlib.util
import unittest
from types import SimpleNamespace
from uuid import uuid4


_AIOGRAM_AVAILABLE = importlib.util.find_spec("aiogram") is not None


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


@unittest.skipUnless(_AIOGRAM_AVAILABLE, "aiogram is not installed")
class ClientPlatformSalesGoalNavigationTests(unittest.TestCase):
    def test_owner_home_uses_canonical_acquisition_contract(self) -> None:
        from handlers import clientplatform_goal_dashboard as goal_dashboard
        from handlers import clientplatform_goal_first_safety as goal_contract

        business_id = str(uuid4())
        token = goal_dashboard.control._uuid_token(business_id)
        buttons = _buttons(goal_dashboard._goal_keyboard(business_id))
        by_text = {button.text: button.callback_data for button in buttons}
        action = goal_contract.ACQUIRE_CLIENTS

        self.assertEqual(by_text[action.label], action.callback(token))
        self.assertEqual(by_text["💬 Обращения и продажи"], f"cps:s:{token}")
        self.assertIn("👥 Клиенты и запись", by_text)
        self.assertIn("⚙️ Ещё", by_text)
        self.assertNotIn("🚀 Получить клиентов", by_text)

    def test_goal_schedule_resume_uses_same_acquisition_callback(self) -> None:
        from handlers import clientplatform_goal_first_safety as goal_contract
        from handlers import clientplatform_goal_schedule as goal_schedule

        message = SimpleNamespace(
            from_user=SimpleNamespace(id=101),
            bot=SimpleNamespace(),
        )
        resumed = goal_schedule._ResumeCallback(
            message=message,
            business_token="business-token",
        )

        self.assertEqual(
            resumed.data,
            goal_contract.ACQUIRE_CLIENTS.callback("business-token"),
        )

    def test_recovery_copy_is_derived_from_same_visible_action(self) -> None:
        from handlers import clientplatform_goal_first_safety as goal_contract

        action = goal_contract.ACQUIRE_CLIENTS
        message = action.recovery(
            "Этот шаг уже устарел.",
            continuation="и я начну заново.",
        )

        self.assertIn(f"«{action.label}»", message)
        self.assertEqual(
            message,
            f"Этот шаг уже устарел. Нажмите «{action.label}» и я начну заново.",
        )

    def test_goal_first_composition_reuses_canonical_owner_home_keyboard(self) -> None:
        from handlers import clientplatform_goal_dashboard as goal_dashboard
        from handlers import clientplatform_goal_first_autopilot as goal_first
        from handlers import clientplatform_one_click_experience as one_click

        self.assertIs(goal_first._goal_keyboard, goal_dashboard._goal_keyboard)
        self.assertIs(one_click._home_keyboard, goal_dashboard._goal_keyboard)

    def test_sales_workspace_uses_beginner_facing_labels(self) -> None:
        from handlers import clientplatform_sales as sales

        business_id = str(uuid4())
        buttons = _buttons(sales._home_keyboard(business_id))
        labels = {button.text for button in buttons}

        self.assertIn("💬 Обращения", labels)
        self.assertIn("🙋 Нужно подключиться", labels)
        self.assertIn("📊 Как идут продажи", labels)
        self.assertIn("🧩 Что предлагать", labels)
        self.assertNotIn("📊 Воронка", labels)
        self.assertNotIn("🪜 Линейка", labels)


if __name__ == "__main__":
    unittest.main()
