from __future__ import annotations

import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4


_AIOGRAM_AVAILABLE = importlib.util.find_spec("aiogram") is not None


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


@unittest.skipUnless(_AIOGRAM_AVAILABLE, "aiogram is not installed")
class ClientPlatformSalesGoalNavigationTests(unittest.IsolatedAsyncioTestCase):
    def test_owner_home_uses_canonical_acquisition_contract(self) -> None:
        from handlers import clientplatform_goal_dashboard as goal_dashboard
        from handlers import clientplatform_goal_first_safety as goal_contract

        business_id = str(uuid4())
        token = goal_dashboard.control._uuid_token(business_id)
        buttons = _buttons(goal_dashboard._goal_keyboard(business_id))
        by_text = {button.text: button.callback_data for button in buttons}
        action = goal_contract.ACQUIRE_CLIENTS

        self.assertEqual(by_text[action.label], action.callback(token))
        self.assertEqual(by_text["🧭 Что можно сделать"], f"cpo:more:{token}")
        self.assertEqual(len(buttons), 2)
        self.assertNotIn("💬 Обращения и продажи", by_text)
        self.assertNotIn("👥 Клиенты и запись", by_text)
        self.assertNotIn("🚀 Получить клиентов", by_text)

    def test_owner_home_promotes_canonical_handoff_as_single_primary_action(self) -> None:
        from clientplatform.application.growth_cockpit import GrowthAction
        from handlers import clientplatform_goal_dashboard as goal_dashboard

        business_id = str(uuid4())
        token = goal_dashboard.control._uuid_token(business_id)
        action = GrowthAction(
            title="Ответить клиентам",
            reason="Есть обращение",
            action_key="sales_handoff",
            source="sales_handoff_queue",
        )
        buttons = _buttons(goal_dashboard._goal_keyboard(business_id, action))

        self.assertEqual(buttons[0].text, "🙋 Ответить клиентам")
        self.assertEqual(buttons[0].callback_data, f"cps:sh:{token}")
        self.assertEqual(buttons[1].text, "🧭 Что можно сделать")


    def test_owner_primary_action_routes_sales_plan_and_attribution_review(self) -> None:
        from clientplatform.application.growth_cockpit import GrowthAction
        from handlers import clientplatform_goal_dashboard as goal_dashboard

        business_id = str(uuid4())
        token = goal_dashboard.control._uuid_token(business_id)
        sales = GrowthAction(
            title="Продолжить работу",
            reason="Есть следующий шаг",
            action_key="sales_plan:plan-1",
            source="sales_action_plan",
        )
        lead_id = str(uuid4())
        manual = GrowthAction(
            title="Позвонить клиенту",
            reason="Сохранён ручной следующий шаг",
            action_key=f"sales_lead:{lead_id}",
            source="sales_lead",
            source_id=lead_id,
        )
        attribution = GrowthAction(
            title="Проверить источники",
            reason="Есть оплата без источника",
            action_key="attribution_review",
            source="revenue_attribution",
        )
        self.assertEqual(
            goal_dashboard._primary_action(business_id, sales),
            ("💬 Продолжить работу с клиентом", f"cps:sw:{token}"),
        )
        self.assertEqual(
            goal_dashboard._primary_action(business_id, manual),
            (
                "💬 Открыть клиента",
                f"cps:swv:{token}:{goal_dashboard.control._uuid_token(lead_id)}",
            ),
        )
        self.assertEqual(
            goal_dashboard._primary_action(business_id, attribution),
            ("💰 Проверить источники оплат", f"cpy:a:{token}:7"),
        )

    def test_owner_next_action_fails_closed_on_expected_read_errors(self) -> None:
        from handlers import clientplatform_goal_dashboard as goal_dashboard

        self.assertIsNone(goal_dashboard._without_advertising())
        for exc in (ValueError("bad"), OSError("down"), RuntimeError("down")):
            with self.subTest(exc=type(exc).__name__), patch.object(
                goal_dashboard, "get_growth_cockpit", side_effect=exc
            ):
                action = goal_dashboard._owner_next_action(object())
                self.assertEqual(action.action_key, "projection_unavailable")
                self.assertIn("проверьте работу вручную", action.reason.casefold())

    async def test_dashboard_renders_canonical_next_action_as_the_one_primary_button(self) -> None:
        from clientplatform.application.growth_cockpit import GrowthAction
        from handlers import clientplatform_goal_dashboard as goal_dashboard

        business_id = str(uuid4())
        action = GrowthAction(
            title="Продолжить работу с клиентом: Анна",
            reason="Для клиента уже существует следующий шаг.",
            action_key="sales_plan:plan-1",
            source="sales_action_plan",
        )
        message = SimpleNamespace(answer=AsyncMock())
        snapshot = (
            object(),
            SimpleNamespace(business=SimpleNamespace(name="Практика")),
            SimpleNamespace(activity_description="Помогаю клиентам"),
            [],
            [object()],
            [object()],
            [],
        )
        async def direct(function, *args, **kwargs):
            return function(*args, **kwargs)
        with (
            patch.object(goal_dashboard.one_click.simple, "_business_snapshot", new=AsyncMock(return_value=snapshot)),
            patch.object(goal_dashboard, "_owner_next_action", return_value=action),
            patch.object(goal_dashboard.asyncio, "to_thread", new=direct),
        ):
            await goal_dashboard.send_goal_dashboard(
                message, user_id=101, business_id=business_id
            )
        text = message.answer.await_args.args[0]
        buttons = _buttons(message.answer.await_args.kwargs["reply_markup"])
        self.assertIn(action.title, text)
        self.assertIn(action.reason, text)
        self.assertEqual(buttons[0].text, "💬 Продолжить работу с клиентом")
        self.assertEqual(len(buttons), 2)

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
