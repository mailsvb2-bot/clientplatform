from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from clientplatform.domain.ad_connections import AdConnectionStatus
from clientplatform.domain.bookings import BookingSlotStatus
from clientplatform.domain.tenancy import TenantPermissionDenied
from handlers import clientplatform_goal_first_autopilot as goal
from handlers import clientplatform_one_click_experience as one_click


class FakeState:
    def __init__(self, data=None) -> None:
        self.data = dict(data or {})
        self.state = None
        self.cleared = False

    async def get_data(self):
        return dict(self.data)

    async def set_data(self, data):
        self.data = dict(data)

    async def update_data(self, **values):
        self.data.update(values)

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self.cleared = True
        self.data.clear()


async def immediate_to_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


def outbound_message():
    return SimpleNamespace(answer=AsyncMock())


def callback(data: str, out):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
        bot=SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(username="clientplatform_bot"))
        ),
        message=out,
    )


def slot(*, slot_id="slot-1", start="2026-08-20T09:00:00+00:00"):
    return SimpleNamespace(
        slot=SimpleNamespace(
            id=slot_id,
            status=BookingSlotStatus.OPEN,
            starts_at=start,
        ),
        offering_title="Консультация",
        local_start="20.08.2026 12:00",
    )


def connection(*, connection_id="connection-1", login="owner"):
    return SimpleNamespace(
        id=connection_id,
        external_login=login,
        status=AdConnectionStatus.ACTIVE,
    )


def publication_job(*, connection_id="connection-1", regions=(47,)):
    return SimpleNamespace(
        connection_id=connection_id,
        external_campaign_id="historical-provider-id",
        region_ids=regions,
    )


def promotion():
    return SimpleNamespace(
        campaign=SimpleNamespace(
            id="promotion-1",
            source_token="source-token-0001",
            creative=SimpleNamespace(
                headline="Консультация",
                primary_text="Есть свободное время.",
                description="Запись онлайн",
            ),
        )
    )


def managed_draft():
    return SimpleNamespace(
        campaign_name="ClientPlatform managed",
        job=SimpleNamespace(
            id="job-2",
            external_campaign_id="managed-7001",
            region_ids=(47,),
            title="Консультация",
            text="Свободное время для записи",
        ),
    )


class OneClickOwnerExperienceTests(unittest.IsolatedAsyncioTestCase):
    def common_patches(self, out):
        return (
            patch.object(one_click.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(one_click.control, "_token_uuid", side_effect=lambda value: value),
            patch.object(one_click.control, "_uuid_token", side_effect=lambda value: value),
            patch.object(
                one_click.control,
                "_actor",
                new=AsyncMock(return_value="actor"),
            ),
            patch.object(one_click.control, "_callback_message", return_value=out),
        )

    async def test_dashboard_separates_acquisition_sales_and_secondary_actions(self) -> None:
        out = outbound_message()
        snapshot = (
            "actor",
            SimpleNamespace(business=SimpleNamespace(name="Мой бизнес")),
            SimpleNamespace(activity_description="Помогаю клиентам решать задачи"),
            [],
            [],
            [],
            [slot()],
        )
        with (
            patch.object(
                one_click.simple,
                "_business_snapshot",
                new=AsyncMock(return_value=snapshot),
            ),
            patch.object(one_click.control, "list_booking_slots", return_value=[slot()]),
            patch.object(one_click.control, "_uuid_token", side_effect=lambda value: value),
            patch.object(
                goal.send_goal_dashboard.__globals__["owner"],
                "_all_offerings",
                new=AsyncMock(return_value=[]),
            ),
        ):
            await goal.send_goal_dashboard(out, user_id=101, business_id="business-1")
        labels = [
            button.text
            for row in out.answer.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("🚀 Найти новых клиентов", labels)
        self.assertIn("💬 Обращения и продажи", labels)
        self.assertNotIn("🚀 Получить клиентов", labels)

    async def test_no_open_slot_reduces_flow_to_one_required_next_action(self) -> None:
        out = outbound_message()
        cb = callback("cpo:start:business-1", out)
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patch.object(one_click.control, "list_booking_slots", return_value=[]),
        ):
            await one_click.get_clients_one_click(cb, FakeState())
        self.assertIn("Сначала нужно одно свободное время", out.answer.await_args.args[0])
        self.assertEqual(
            out.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0].text,
            "➕ Открыть время",
        )

    async def test_existing_provider_campaign_is_not_a_selection_step(self) -> None:
        out = outbound_message()
        cb = callback("cpo:start:business-1", out)
        state = FakeState()
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patch.object(one_click, "ad_connections_enabled", return_value=True),
            patch.object(one_click, "yandex_direct_provider_configured", return_value=True),
            patch.object(one_click, "list_ad_connections", return_value=[connection()]),
            patch.object(one_click.control, "list_booking_slots", return_value=[slot()]),
            patch.object(one_click, "list_ad_publications", return_value=[]),
        ):
            await one_click.get_clients_one_click(cb, state)
        self.assertEqual(state.state, one_click.OneClickOwnerState.waiting_region)
        self.assertNotIn("external_campaign_id", state.data)
        self.assertFalse(hasattr(one_click.OneClickOwnerState, "selecting_campaign"))

    async def test_manager_without_ad_token_access_still_gets_promotion_result(self) -> None:
        out = outbound_message()
        cb = callback("cpo:start:business-1", out)
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patch.object(one_click, "ad_connections_enabled", return_value=True),
            patch.object(one_click, "yandex_direct_provider_configured", return_value=True),
            patch.object(
                one_click,
                "list_ad_connections",
                side_effect=TenantPermissionDenied("owner only"),
            ),
            patch.object(one_click.control, "list_booking_slots", return_value=[slot()]),
            patch.object(one_click, "list_ad_publications", return_value=[]),
            patch.object(one_click, "create_slot_promotion", return_value=promotion()),
        ):
            await one_click.get_clients_one_click(cb, FakeState())
        text = out.answer.await_args.args[0]
        self.assertIn("Уже можно привлекать клиентов", text)
        self.assertIn("нет доступа к личному рекламному кабинету", text)

    async def test_previous_safe_region_makes_managed_preparation_one_click(self) -> None:
        out = outbound_message()
        cb = callback("cpo:start:business-1", out)
        state = FakeState()
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patch.object(one_click, "ad_connections_enabled", return_value=True),
            patch.object(one_click, "yandex_direct_provider_configured", return_value=True),
            patch.object(one_click, "list_ad_connections", return_value=[connection()]),
            patch.object(one_click.control, "list_booking_slots", return_value=[slot()]),
            patch.object(
                one_click,
                "list_ad_publications",
                return_value=[publication_job()],
            ),
            patch.object(one_click, "create_slot_promotion", return_value=promotion()),
            patch.object(
                one_click,
                "create_managed_ad_publication_draft",
                return_value=managed_draft(),
            ) as create_draft,
        ):
            await one_click.get_clients_one_click(cb, state)
        create_draft.assert_called_once()
        self.assertNotIn("external_campaign_id", create_draft.call_args.kwargs)
        self.assertEqual(state.state, goal.GoalFirstAutopilotState.ready)
        self.assertEqual(state.data["promotion_campaign_id"], "promotion-1")
        self.assertEqual(state.data["job_id"], "job-2")
        self.assertEqual(state.data["external_campaign_id"], "managed-7001")
        self.assertEqual(state.data["external_campaign_name"], "ClientPlatform managed")
        self.assertIn("Реклама подготовлена", out.answer.await_args.args[0])

    async def test_first_direct_run_asks_only_for_missing_region(self) -> None:
        out = outbound_message()
        cb = callback("cpo:start:business-1", out)
        state = FakeState()
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patch.object(one_click, "ad_connections_enabled", return_value=True),
            patch.object(one_click, "yandex_direct_provider_configured", return_value=True),
            patch.object(one_click, "list_ad_connections", return_value=[connection()]),
            patch.object(one_click.control, "list_booking_slots", return_value=[slot()]),
            patch.object(one_click, "list_ad_publications", return_value=[]),
        ):
            await one_click.get_clients_one_click(cb, state)
        self.assertEqual(state.state, one_click.OneClickOwnerState.waiting_region)
        text = out.answer.await_args.args[0]
        self.assertIn("Осталось только указать регион", text)
        self.assertIn("создаст и привяжет сам", text)

    async def test_manual_campaign_selection_contract_is_absent(self) -> None:
        self.assertFalse(hasattr(one_click.OneClickOwnerState, "selecting_campaign"))
        self.assertFalse(hasattr(one_click, "choose_one_click_campaign"))
        self.assertFalse(hasattr(one_click, "_choose_campaign"))
        self.assertFalse(hasattr(one_click, "_eligible"))

    async def test_multiple_accounts_are_not_guessed_without_history(self) -> None:
        out = outbound_message()
        cb = callback("cpo:start:business-1", out)
        state = FakeState()
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patch.object(one_click, "ad_connections_enabled", return_value=True),
            patch.object(one_click, "yandex_direct_provider_configured", return_value=True),
            patch.object(
                one_click,
                "list_ad_connections",
                return_value=[
                    connection(connection_id="connection-1", login="one"),
                    connection(connection_id="connection-2", login="two"),
                ],
            ),
            patch.object(one_click.control, "list_booking_slots", return_value=[slot()]),
            patch.object(one_click, "list_ad_publications", return_value=[]),
        ):
            await one_click.get_clients_one_click(cb, state)
        self.assertEqual(state.state, one_click.OneClickOwnerState.selecting_connection)
        labels = [
            button.text
            for row in out.answer.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("Яндекс · one", labels)
        self.assertIn("Яндекс · two", labels)

    async def test_more_menu_hides_advanced_actions_from_home(self) -> None:
        out = outbound_message()
        cb = callback("cpo:more:business-1", out)
        patches = self.common_patches(out)
        with patches[1], patches[3], patches[4]:
            await one_click.open_more(cb)
        labels = [
            button.text
            for row in out.answer.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertEqual(
            labels,
            [
                "🧰 Услуги и расписание",
                "📣 Реклама и продвижение",
                "🤝 Партнёрства",
                "⚙️ Настройки",
                "🏠 В кабинет",
            ],
        )


if __name__ == "__main__":
    unittest.main()
