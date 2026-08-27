from __future__ import annotations

import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from clientplatform.domain.ad_connections import AdConnectionError
from clientplatform.domain.promotions import PromotionError
from clientplatform.integrations.yandex_direct import YandexDirectError
from handlers import clientplatform_control as control
from handlers import clientplatform_goal_first_autopilot as goal
from handlers import clientplatform_goal_first_safety as safety
from handlers import clientplatform_one_click_experience as one_click


class FakeState:
    def __init__(self, data=None) -> None:
        self.data = dict(data or {})
        self.state = None
        self.clear_count = 0

    async def get_data(self):
        return dict(self.data)

    async def set_data(self, data):
        self.data = dict(data)

    async def update_data(self, **values):
        self.data.update(values)

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self.clear_count += 1
        self.data.clear()


async def direct(function, *args, **kwargs):
    return function(*args, **kwargs)


def outbound():
    return SimpleNamespace(answer=AsyncMock())


def callback(target=None, *, username="clientplatform_bot"):
    target = target or outbound()
    return SimpleNamespace(
        data="cpo:start:business-1",
        from_user=SimpleNamespace(id=101),
        message=target,
        answer=AsyncMock(),
        bot=SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(username=username))
        ),
    )


def base_data():
    return {
        "business_id": "business-1",
        "business_token": "business-1",
        "slot_id": "slot-1",
        "connection_id": "connection-1",
    }


def promotion():
    return SimpleNamespace(
        campaign=SimpleNamespace(id="promotion-1", source_token="source-token-1")
    )


def draft():
    return SimpleNamespace(
        campaign_name="ClientPlatform managed",
        job=SimpleNamespace(
            id="job-1",
            external_campaign_id="managed-7001",
            title="Консультация",
            text="Свободное время",
        ),
    )


class ManagedGoalFirstSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.original_prepare = getattr(goal, "_prepare_goal_result", None)
        self.original_choose = getattr(goal, "_choose_goal_region", None)
        self.original_one_click_prepare = getattr(one_click, "_prepare_draft", None)
        self.original_installed = getattr(
            one_click, "_managed_campaign_goal_first_installed", False
        )

    def tearDown(self) -> None:
        if self.original_prepare is not None:
            goal._prepare_goal_result = self.original_prepare
        if self.original_choose is not None:
            goal._choose_goal_region = self.original_choose
        if self.original_one_click_prepare is not None:
            one_click._prepare_draft = self.original_one_click_prepare
        one_click._managed_campaign_goal_first_installed = self.original_installed

    def _reinstall(self) -> None:
        one_click._managed_campaign_goal_first_installed = False
        safety._install_managed_campaign_goal_first()

    async def test_install_is_safe_when_dependency_module_is_missing_or_already_installed(self):
        original = sys.modules.pop("handlers.clientplatform_control")
        try:
            one_click._managed_campaign_goal_first_installed = False
            safety._install_managed_campaign_goal_first()
            self.assertFalse(one_click._managed_campaign_goal_first_installed)
        finally:
            sys.modules["handlers.clientplatform_control"] = original

        one_click._managed_campaign_goal_first_installed = True
        safety._install_managed_campaign_goal_first()
        self.assertTrue(one_click._managed_campaign_goal_first_installed)

    async def test_managed_prepare_fail_closed_for_promotion_source_and_yandex_errors(self):
        target = outbound()
        event = callback(target)
        self._reinstall()

        for failure_kind in ("promotion", "source", "managed"):
            with self.subTest(failure_kind=failure_kind):
                state = FakeState(base_data())
                target.answer.reset_mock()
                patches = [
                    patch.object(one_click.asyncio, "to_thread", new=direct),
                    patch.object(control, "_actor", new=AsyncMock(return_value="actor")),
                    patch.object(control, "_callback_message", return_value=target),
                    patch.object(one_click, "create_slot_promotion", return_value=promotion()),
                    patch.object(
                        one_click,
                        "create_managed_ad_publication_draft",
                        return_value=draft(),
                    ),
                    patch.object(
                        one_click.settings,
                        "MESSENGER_PUBLIC_BASE_URL",
                        "https://client.example.test",
                    ),
                ]
                if failure_kind == "promotion":
                    patches[3] = patch.object(
                        one_click,
                        "create_slot_promotion",
                        side_effect=PromotionError("promotion failed"),
                    )
                elif failure_kind == "source":
                    patches[5] = patch.object(
                        one_click.settings,
                        "MESSENGER_PUBLIC_BASE_URL",
                        "",
                    )
                else:
                    event = callback(target)
                    patches[4] = patch.object(
                        one_click,
                        "create_managed_ad_publication_draft",
                        side_effect=YandexDirectError("provider failed"),
                    )

                with (
                    patches[0],
                    patches[1],
                    patches[2],
                    patches[3],
                    patches[4],
                    patches[5],
                ):
                    await goal._prepare_goal_result(
                        event,
                        state,
                        data=base_data(),
                        region_ids=(47,),
                    )

                self.assertEqual(state.clear_count, 1)
                self.assertIn("Ничего не запущено", target.answer.await_args.args[0])
                event = callback(target)

    async def test_region_history_error_asks_region_and_saved_region_continues(self):
        target = outbound()
        event = callback(target)
        self._reinstall()

        state = FakeState(base_data())
        with (
            patch.object(one_click.asyncio, "to_thread", new=direct),
            patch.object(control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(control, "_callback_message", return_value=target),
            patch.object(
                one_click,
                "list_ad_publications",
                side_effect=AdConnectionError("history unavailable"),
            ),
        ):
            await goal._choose_goal_region(
                event,
                state,
                data=base_data(),
                campaign_id="ignored",
                campaign_name="ignored",
            )
        self.assertEqual(state.state, one_click.OneClickOwnerState.waiting_region)
        self.assertIn("Осталось только указать регион", target.answer.await_args.args[0])

        saved = SimpleNamespace(connection_id="connection-1", region_ids=(47,))
        prepare = AsyncMock()
        with (
            patch.object(one_click.asyncio, "to_thread", new=direct),
            patch.object(control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(one_click, "list_ad_publications", return_value=[saved]),
            patch.object(one_click, "_prepare_draft", new=prepare),
        ):
            await goal._choose_goal_region(
                event,
                FakeState(base_data()),
                data=base_data(),
                campaign_id="ignored",
                campaign_name="ignored",
            )
        prepare.assert_awaited_once()
        self.assertEqual(prepare.await_args.kwargs["region_ids"], (47,))

    async def test_managed_prepare_success_keeps_goal_first_ready_contract(self):
        target = outbound()
        event = callback(target)
        state = FakeState(base_data())
        self._reinstall()
        with (
            patch.object(one_click.asyncio, "to_thread", new=direct),
            patch.object(control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(control, "_callback_message", return_value=target),
            patch.object(one_click, "create_slot_promotion", return_value=promotion()),
            patch.object(
                one_click.settings,
                "MESSENGER_PUBLIC_BASE_URL",
                "https://client.example.test",
            ),
            patch.object(
                one_click,
                "create_managed_ad_publication_draft",
                return_value=draft(),
            ) as create_draft,
        ):
            await goal._prepare_goal_result(
                event,
                state,
                data=base_data(),
                region_ids=(47,),
            )
        create_draft.assert_called_once()
        self.assertNotIn("external_campaign_id", create_draft.call_args.kwargs)
        self.assertEqual(
            create_draft.call_args.kwargs["source_url"],
            "https://client.example.test/clientplatform/acquire?source=cpa_source-token-1",
        )
        self.assertEqual(state.state, goal.GoalFirstAutopilotState.ready)
        self.assertEqual(state.data["external_campaign_id"], "managed-7001")
        self.assertEqual(state.data["external_campaign_name"], "ClientPlatform managed")

    async def test_generation_confirmation_is_explicit_and_rejects_stale_draft(self):
        stale = callback()
        stale.data = "cpo:genask:business-1"
        stale_state = FakeState({"business_token": "other-business"})
        await goal.ask_generated_image_confirmation(stale, stale_state)
        stale.answer.assert_awaited_once_with(
            "Этот черновик уже устарел", show_alert=True
        )
        self.assertIsNone(stale_state.state)

        target = outbound()
        event = callback(target)
        event.data = "cpo:genask:business-1"
        state = FakeState({"business_token": "business-1"})
        with patch.object(control, "_callback_message", return_value=target):
            await goal.ask_generated_image_confirmation(event, state)
        self.assertEqual(
            state.state, goal.GoalFirstAutopilotState.confirming_generation
        )
        event.answer.assert_awaited_once_with()
        text = target.answer.await_args.args[0]
        self.assertIn("только после явного выбора", text)
        labels = [
            button.text
            for row in target.answer.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertEqual(
            labels,
            [
                "✅ Создать 1 картинку",
                "🎨 Выбрать из 3 концепций",
                "⬅️ Не создавать",
            ],
        )

    async def test_creative_studio_failure_and_three_concept_preview(self):
        event = callback()
        event.data = "cpo:genstudio:business-1"
        state = FakeState(
            {
                "business_token": "business-1",
                "business_id": "business-1",
                "job_id": "job-1",
            }
        )
        with (
            patch.object(control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(goal.asyncio, "to_thread", new=direct),
            patch.object(goal, "load_goal_visual_brand", return_value=None),
            patch.object(
                goal,
                "build_goal_image_variants",
                side_effect=ValueError("invalid"),
            ),
        ):
            await goal.open_generated_image_studio(event, state)
        event.answer.assert_awaited_once_with(
            "Не удалось подготовить варианты", show_alert=True
        )
        self.assertIsNone(state.state)

        target = outbound()
        event = callback(target)
        event.data = "cpo:genstudio:business-1"
        state = FakeState(
            {
                "business_token": "business-1",
                "business_id": "business-1",
                "job_id": "job-1",
                "creative_title": "Консультация",
                "creative_body": "Свободное время",
            }
        )
        variants = [
            SimpleNamespace(experiment_id="experiment-1"),
            SimpleNamespace(experiment_id="experiment-1"),
            SimpleNamespace(experiment_id="experiment-1"),
        ]
        with (
            patch.object(control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(control, "_callback_message", return_value=target),
            patch.object(goal.asyncio, "to_thread", new=direct),
            patch.object(goal, "load_goal_visual_brand", return_value=None),
            patch.object(goal, "build_goal_image_variants", return_value=variants),
            patch.object(goal, "goal_variant_labels", return_value=("A", "B", "C")),
        ):
            await goal.open_generated_image_studio(event, state)
        self.assertEqual(
            state.state, goal.GoalFirstAutopilotState.confirming_generation
        )
        self.assertEqual(state.data["creative_experiment_id"], "experiment-1")
        self.assertEqual(state.data["creative_variant_id"], "")
        labels = [
            button.text
            for row in target.answer.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertEqual(labels, ["A", "B", "C", "⬅️ Назад"])


class GoalFirstInteractionSafetyTests(unittest.TestCase):
    def _fake_safety(self) -> ModuleType:
        fake = ModuleType("fake_interaction_safety")
        fake._SENSITIVE_STATE_PREFIXES = ()
        fake._ONE_SHOT_PREFIXES = ()
        fake._REPEATABLE_NAVIGATION_PREFIXES = ()
        fake._state_local_callback_allowed = lambda state, callback: False
        fake._callback_can_escape_state = lambda state, callback: False
        return fake

    def test_goal_first_state_callbacks_and_escape_rules(self):
        fake = self._fake_safety()
        with patch.object(safety, "_install_managed_campaign_goal_first") as install_managed:
            safety.install_goal_first_safety(fake)
        install_managed.assert_called_once()

        allowed = fake._state_local_callback_allowed
        self.assertTrue(allowed("GoalFirstAutopilotState:ready", "cpo:launch:business-1"))
        self.assertFalse(allowed("GoalFirstAutopilotState:ready", "cpo:gen:business-1"))
        self.assertTrue(
            allowed("GoalFirstAutopilotState:customizing", "cpo:custom-image:business-1")
        )
        self.assertTrue(
            allowed("GoalFirstAutopilotState:confirming_generation", "cpo:gen:business-1")
        )
        self.assertTrue(
            allowed("GoalFirstAutopilotState:generation_pending", "cpo:gencheck:business-1")
        )
        self.assertTrue(
            allowed("GoalFirstAutopilotState:confirming_launch", "cpo:launch-confirm:business-1")
        )
        self.assertFalse(allowed("OtherState:ready", "cpo:launch:business-1"))

        escape = fake._callback_can_escape_state
        self.assertTrue(escape("GoalFirstAutopilotState:ready", "cpj:home:business-1"))
        self.assertTrue(escape("GoalFirstAutopilotState:ready", "cpo:start:business-1"))
        self.assertFalse(escape("OtherState:ready", "cpj:home:business-1"))

        self.assertIn("GoalFirstAutopilotState:", fake._SENSITIVE_STATE_PREFIXES)
        self.assertIn("cpo:launch:", fake._ONE_SHOT_PREFIXES)
        self.assertIn("cpo:gencheck:", fake._REPEATABLE_NAVIGATION_PREFIXES)

    def test_install_is_idempotent(self):
        fake = self._fake_safety()
        with patch.object(safety, "_install_managed_campaign_goal_first") as install_managed:
            safety.install_goal_first_safety(fake)
            safety.install_goal_first_safety(fake)
        install_managed.assert_called_once()


if __name__ == "__main__":
    unittest.main()
