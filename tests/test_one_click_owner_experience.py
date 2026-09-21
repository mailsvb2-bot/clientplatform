from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from clientplatform.domain.ad_connections import AdConnectionStatus
from clientplatform.domain.bookings import BookingSlotStatus
from clientplatform.domain.tenancy import PlatformRole, TenantContext, TenantPermissionDenied
from handlers import clientplatform_goal_dashboard as dashboard
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


def tenant_actor(role: PlatformRole = PlatformRole.OWNER) -> TenantContext:
    return TenantContext(
        business_id="11111111-1111-4111-8111-111111111111",
        user_id=101,
        membership_id="22222222-2222-4222-8222-222222222222",
        role=role,
    )


def slot(*, slot_id="slot-1", start="2026-08-20T09:00:00+00:00"):
    return SimpleNamespace(
        slot=SimpleNamespace(
            id=slot_id,
            status=BookingSlotStatus.OPEN,
            starts_at=start,
            offering_id="offering-1",
            duration_minutes=60,
        ),
        offering_title="Консультация",
        local_start="20.08.2026 12:00",
    )


def offering(*, offering_id="offering-1", title="Консультация"):
    return SimpleNamespace(id=offering_id, title=title)


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
    def setUp(self) -> None:
        self.public_base = patch.object(
            one_click.settings,
            "MESSENGER_PUBLIC_BASE_URL",
            "https://client.example.test",
        )
        self.public_base.start()

    def tearDown(self) -> None:
        self.public_base.stop()

    def common_patches(self, out):
        return (
            patch.object(one_click.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(one_click.control, "_token_uuid", side_effect=lambda value: value),
            patch.object(one_click.control, "_uuid_token", side_effect=lambda value: value),
            patch.object(
                one_click.control,
                "_actor",
                new=AsyncMock(return_value=tenant_actor()),
            ),
            patch.object(one_click.control, "_callback_message", return_value=out),
        )

    async def test_dashboard_separates_acquisition_sales_and_secondary_actions(self) -> None:
        out = outbound_message()
        snapshot = (
            tenant_actor(),
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
            patch.object(one_click.control, "_uuid_token", side_effect=lambda value: value),
            patch.object(dashboard, "_owner_next_action", return_value=None),
        ):
            await goal.send_goal_dashboard(out, user_id=101, business_id="business-1")
        labels = [
            button.text
            for row in out.answer.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertEqual(
            labels,
            [
                "💬 Клиенты и обращения",
                "👥 Найти клиентов",
                "💰 Продажи",
                "📊 Результаты",
                "▦ Все возможности",
            ],
        )
        self.assertNotIn("🚀 Получить клиентов", labels)

    async def test_all_capabilities_menu_leads_with_real_cockpit_and_keeps_quick_actions(self) -> None:
        out = outbound_message()
        cb = callback("cpo:more:business-1", out)
        with (
            patch.object(one_click.control, "_actor", new=AsyncMock(return_value=tenant_actor())),
            patch.object(one_click.control, "_token_uuid", side_effect=lambda value: value),
            patch.object(one_click.control, "_callback_message", return_value=out),
            patch.object(
                one_click,
                "cockpit_web_app_url",
                return_value="https://app.example.test/clientplatform/cockpit",
            ),
        ):
            await one_click.open_more(cb)
        answer_text = out.answer.await_args.args[0]
        markup = out.answer.await_args.kwargs["reply_markup"]
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertEqual(
            labels,
            [
                "🏠 Открыть кабинет",
                "💰 Деньги и результат",
                "👥 Клиенты и продажи",
                "📅 Услуги и запись",
                "🎨 Создать картинку",
                "📈 Продвижение и контент",
                "⚙️ Настройки бизнеса",
                "⬅️ Назад",
            ],
        )
        self.assertEqual(
            markup.inline_keyboard[0][0].web_app.url,
            "https://app.example.test/clientplatform/cockpit",
        )
        self.assertIn("🏠 Кабинет ClientPlatform", answer_text)
        self.assertIn("только те быстрые действия", answer_text)
        self.assertNotIn("Если Вам нужно:", answer_text)
        self.assertNotIn("🧭 Что можно сделать", answer_text)
        self.assertNotIn("💬 Подключить мессенджеры", labels)
        self.assertNotIn("📣 Реклама и продвижение", labels)

    def test_all_capabilities_menu_falls_back_to_quick_actions_without_public_cockpit(self) -> None:
        with patch.object(one_click, "cockpit_web_app_url", return_value=None):
            markup = one_click._more_keyboard("business-1", tenant_actor())
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertNotIn("🏠 Открыть кабинет", labels)
        self.assertEqual(labels[0], "💰 Деньги и результат")

    async def test_start_explicitly_asks_which_service_to_advertise(self) -> None:
        out = outbound_message()
        cb = callback("cpo:start:business-1", out)
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patch.object(
                one_click,
                "_advertisable_offerings",
                new=AsyncMock(return_value=[offering()]),
            ),
        ):
            await one_click.get_clients_one_click(cb, FakeState())
        self.assertIn("Что именно Вы хотите рекламировать", out.answer.await_args.args[0])
        button = out.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.text, "🧰 Консультация")
        self.assertTrue(str(button.callback_data).startswith("cpo:offer:"))

    async def test_advertisable_offerings_excludes_programs_and_deduplicates(self) -> None:
        actor = tenant_actor()
        active = one_click.control.CapabilityStatus.ACTIVE
        capabilities = [
            SimpleNamespace(id="cap-service", connector_key="booking", status=active),
            SimpleNamespace(id="cap-program", connector_key="programs", status=active),
            SimpleNamespace(id="cap-second", connector_key="consulting", status=active),
        ]
        shared = offering()
        with (
            patch.object(one_click.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(
                one_click.control,
                "list_business_capabilities",
                return_value=capabilities,
            ),
            patch.object(
                one_click.control,
                "list_business_offerings",
                side_effect=([shared], [shared]),
            ) as list_offerings,
        ):
            result = await one_click._advertisable_offerings(actor)

        self.assertEqual(result, [shared])
        self.assertEqual(list_offerings.call_count, 2)
        called_capabilities = {
            call.kwargs["capability_id"]
            for call in list_offerings.call_args_list
        }
        self.assertEqual(called_capabilities, {"cap-service", "cap-second"})

    async def test_selected_service_without_open_time_opens_calendar_for_that_service(self) -> None:
        out = outbound_message()
        state = FakeState()
        cb = callback("cpo:offer:business-1:offering-1", out)
        send_picker = AsyncMock()
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patch.object(
                one_click,
                "_advertisable_offerings",
                new=AsyncMock(return_value=[offering()]),
            ),
            patch.object(one_click.control, "list_booking_slots", return_value=[]),
            patch.object(
                one_click.control,
                "get_business_profile",
                return_value=SimpleNamespace(timezone="Europe/Moscow"),
            ),
            patch.object(
                one_click.importlib,
                "import_module",
                return_value=SimpleNamespace(send_booking_date_picker=send_picker),
            ),
        ):
            await one_click.choose_one_click_offering(cb, state)

        self.assertEqual(state.data["offering_id"], "offering-1")
        send_picker.assert_awaited_once()
        self.assertIn("Рекламируем «Консультация»", send_picker.await_args.kwargs["heading"])

    async def test_selected_service_lists_only_its_open_times_and_allows_new_time(self) -> None:
        out = outbound_message()
        state = FakeState()
        cb = callback("cpo:offer:business-1:offering-1", out)
        chosen = slot(slot_id="slot-chosen")
        other = slot(slot_id="slot-other")
        other.slot.offering_id = "offering-2"
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patch.object(
                one_click,
                "_advertisable_offerings",
                new=AsyncMock(return_value=[offering()]),
            ),
            patch.object(
                one_click.control,
                "list_booking_slots",
                return_value=[other, chosen],
            ),
        ):
            await one_click.choose_one_click_offering(cb, state)

        text = out.answer.await_args.args[0]
        self.assertIn("Какую дату и время продвигать", text)
        callbacks = [
            str(button.callback_data)
            for row in out.answer.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertTrue(any(value.startswith("cpo:slot:") for value in callbacks))
        self.assertTrue(any(value.startswith("cpo:newtime:") for value in callbacks))
        self.assertFalse(any("slot-other" in value for value in callbacks))

    async def test_start_without_offerings_routes_to_service_setup(self) -> None:
        out = outbound_message()
        cb = callback("cpo:start:business-1", out)
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patch.object(
                one_click,
                "_advertisable_offerings",
                new=AsyncMock(return_value=[]),
            ),
        ):
            await one_click.get_clients_one_click(cb, FakeState())

        self.assertIn("Сначала добавьте услугу", out.answer.await_args.args[0])
        labels = [
            button.text
            for row in out.answer.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("🧰 Мои услуги", labels)

    async def test_stale_selected_service_fails_closed(self) -> None:
        out = outbound_message()
        cb = callback("cpo:offer:business-1:offering-1", out)
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patch.object(
                one_click,
                "_advertisable_offerings",
                new=AsyncMock(return_value=[]),
            ),
            patch.object(one_click.control, "list_booking_slots", return_value=[]),
        ):
            await one_click.choose_one_click_offering(cb, FakeState())

        cb.answer.assert_awaited_once_with("Эта услуга больше недоступна", show_alert=True)
        out.answer.assert_not_awaited()

    async def test_choose_new_ad_time_opens_calendar_for_selected_service(self) -> None:
        out = outbound_message()
        state = FakeState()
        cb = callback("cpo:newtime:business-1:offering-1", out)
        send_picker = AsyncMock()
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patch.object(
                one_click,
                "_advertisable_offerings",
                new=AsyncMock(return_value=[offering()]),
            ),
            patch.object(
                one_click.control,
                "get_business_profile",
                return_value=SimpleNamespace(timezone="Europe/Moscow"),
            ),
            patch.object(
                one_click.importlib,
                "import_module",
                return_value=SimpleNamespace(send_booking_date_picker=send_picker),
            ),
        ):
            await one_click.choose_new_ad_time(cb, state)

        self.assertEqual(state.data["business_id"], "business-1")
        self.assertEqual(state.data["offering_id"], "offering-1")
        send_picker.assert_awaited_once()
        self.assertIn("новую дату", send_picker.await_args.kwargs["heading"])

    async def test_stale_ad_slot_fails_closed(self) -> None:
        out = outbound_message()
        cb = callback("cpo:slot:business-1:slot-gone", out)
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patch.object(
                one_click,
                "_reload_slot",
                new=AsyncMock(return_value=None),
            ),
        ):
            await one_click.choose_exact_ad_slot(cb, FakeState())

        cb.answer.assert_awaited_once_with(
            "Это время уже недоступно. Выберите другое.",
            show_alert=True,
        )

    async def test_exact_ad_slot_enters_canonical_ad_flow(self) -> None:
        out = outbound_message()
        state = FakeState()
        cb = callback("cpo:slot:business-1:slot-1", out)
        selected = slot()
        start_ad = AsyncMock()
        patches = self.common_patches(out)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patch.object(
                one_click,
                "_reload_slot",
                new=AsyncMock(return_value=selected),
            ),
            patch.object(one_click, "_start_slot_ad", new=start_ad),
        ):
            await one_click.choose_exact_ad_slot(cb, state)

        self.assertIn("Готовлю рекламу", cb.answer.await_args.args[0])
        start_ad.assert_awaited_once()
        self.assertIs(start_ad.await_args.kwargs["slot"], selected)


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
            await one_click._start_slot_ad(
                cb,
                state,
                actor=tenant_actor(),
                business_id="business-1",
                token="business-1",
                slot=slot(),
            )
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
            await one_click._start_slot_ad(
                cb,
                FakeState(),
                actor=tenant_actor(),
                business_id="business-1",
                token="business-1",
                slot=slot(),
            )
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
            await one_click._start_slot_ad(
                cb,
                state,
                actor=tenant_actor(),
                business_id="business-1",
                token="business-1",
                slot=slot(),
            )
        create_draft.assert_called_once()
        self.assertNotIn("external_campaign_id", create_draft.call_args.kwargs)
        self.assertEqual(state.state, goal.GoalFirstAutopilotState.ready)
        self.assertEqual(state.data["promotion_campaign_id"], "promotion-1")
        self.assertEqual(state.data["job_id"], "job-2")
        self.assertEqual(state.data["external_campaign_id"], "managed-7001")
        self.assertEqual(state.data["external_campaign_name"], "ClientPlatform managed")
        text = out.answer.await_args.args[0]
        self.assertIn("Реклама подготовлена", text)
        markup = out.answer.await_args.kwargs["reply_markup"]
        labels = [button.text for row in markup.inline_keyboard for button in row]
        callbacks = {
            str(button.callback_data)
            for row in markup.inline_keyboard
            for button in row
        }
        self.assertIn("🖼 Создать картинку", labels)
        self.assertIn("🎬 Создать видео", labels)
        self.assertIn("cpo:genask:business-1", callbacks)
        self.assertIn("cpo:genvideoask:business-1", callbacks)

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
            await one_click._start_slot_ad(
                cb,
                state,
                actor=tenant_actor(),
                business_id="business-1",
                token="business-1",
                slot=slot(),
            )
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
            await one_click._start_slot_ad(
                cb,
                state,
                actor=tenant_actor(),
                business_id="business-1",
                token="business-1",
                slot=slot(),
            )
        self.assertEqual(state.state, one_click.OneClickOwnerState.selecting_connection)
        labels = [
            button.text
            for row in out.answer.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("Яндекс · one", labels)
        self.assertIn("Яндекс · two", labels)

    def test_settings_exposes_direction_edit_and_owner_business_removal(self) -> None:
        owner_rows, owner_help = one_click._settings_rows("business-1", tenant_actor())
        owner_buttons = {
            label: callback_data
            for row in owner_rows
            for label, callback_data in row
        }
        self.assertEqual(
            owner_buttons["✏️ Изменить направление"],
            "cp:editact:business-1",
        )
        self.assertEqual(
            owner_buttons["🗑 Удалить бизнес"],
            "cps:archive-prompt:business-1",
        )
        self.assertTrue(any("убрать тестовый" in line for line in owner_help))

        admin_rows, _ = one_click._settings_rows(
            "business-1",
            tenant_actor(PlatformRole.ADMINISTRATOR),
        )
        admin_labels = [label for row in admin_rows for label, _ in row]
        self.assertIn("✏️ Изменить направление", admin_labels)
        self.assertNotIn("🗑 Удалить бизнес", admin_labels)

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
                "🏠 Открыть кабинет",
                "💰 Деньги и результат",
                "👥 Клиенты и продажи",
                "📅 Услуги и запись",
                "🎨 Создать картинку",
                "📈 Продвижение и контент",
                "⚙️ Настройки бизнеса",
                "⬅️ Назад",
            ],
        )
        buttons = {
            button.text: button.callback_data
            for row in out.answer.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        }
        self.assertEqual(buttons["👥 Клиенты и продажи"], "cpo:clients:business-1")
        self.assertEqual(buttons["📈 Продвижение и контент"], "cpo:content:business-1")
        self.assertEqual(buttons["⚙️ Настройки бизнеса"], "cpo:settings:business-1")


if __name__ == "__main__":
    unittest.main()
