from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from clientplatform.domain.ad_connections import AdConnectionStatus
from clientplatform.domain.bookings import BookingSlotStatus
from clientplatform.domain.tenancy import TenantPermissionDenied
from handlers import clientplatform_goal_driven_experience as goal


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


class FakeMessage:
    def __init__(self, text="") -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=101)
        self.bot = SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(username="clientplatform_bot"))
        )
        self.answers = []
        self.photos = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))

    async def answer_photo(self, photo, **kwargs):
        self.photos.append((photo, kwargs))


def callback(data: str, out: FakeMessage | None = None):
    target = out or FakeMessage()
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=101),
        bot=target.bot,
        message=target,
        answer=AsyncMock(),
    )


async def direct(function, *args, **kwargs):
    return function(*args, **kwargs)


def slot(slot_id="slot-1", status=BookingSlotStatus.OPEN):
    return SimpleNamespace(
        slot=SimpleNamespace(
            id=slot_id,
            status=status,
            starts_at="2026-08-20T09:00:00+00:00",
        ),
        offering_title="Консультация 60 минут",
        local_start="20.08.2026 12:00",
    )


def offering(offering_id="offering-1", title="Консультация"):
    return SimpleNamespace(id=offering_id, title=title)


def capability(capability_id="cap-1", key="services"):
    return SimpleNamespace(
        id=capability_id,
        connector_key=key,
        status=goal.CapabilityStatus.ACTIVE,
    )


def connection(connection_id="conn-1", login="owner"):
    return SimpleNamespace(
        id=connection_id,
        external_login=login,
        status=AdConnectionStatus.ACTIVE,
    )


def campaign(campaign_id="6001", name="Основная", state="ON", status="ACCEPTED"):
    return SimpleNamespace(
        campaign_id=campaign_id,
        name=name,
        state=state,
        status=status,
    )


def publication(
    job_id="job-1",
    *,
    connection_id="conn-1",
    campaign_id="6001",
    regions=(47,),
    updated_at="2026-08-11T12:00:00+00:00",
):
    return SimpleNamespace(
        id=job_id,
        business_id="business-1",
        connection_id=connection_id,
        external_campaign_id=campaign_id,
        region_ids=regions,
        title="Новая консультация",
        text="Есть свободное время",
        updated_at=updated_at,
        created_at=updated_at,
    )


def promotion():
    return SimpleNamespace(
        campaign=SimpleNamespace(
            id="promotion-1",
            source_token="source-token-1",
            creative=SimpleNamespace(
                headline="Консультация",
                primary_text="Есть свободное время",
                description="Запись онлайн",
            ),
        )
    )


def draft(job_id="job-1"):
    return SimpleNamespace(
        job=SimpleNamespace(
            id=job_id,
            title="Консультация",
            text="Есть свободное время",
        ),
        campaign_name="Основная",
    )


class GoalDrivenOwnerExperienceTests(unittest.IsolatedAsyncioTestCase):
    def patches(self, out=None):
        target = out or FakeMessage()
        return (
            patch.object(goal.asyncio, "to_thread", new=direct),
            patch.object(goal.control, "_token_uuid", side_effect=lambda value: value),
            patch.object(goal.control, "_uuid_token", side_effect=lambda value: value),
            patch.object(goal.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(goal.control, "_callback_message", return_value=target),
            patch.object(goal, "Message", FakeMessage),
        )

    async def test_dashboard_is_intent_first_and_hides_ad_implementation(self):
        out = FakeMessage()
        snapshot = (
            "actor",
            SimpleNamespace(business=SimpleNamespace(name="Мой бизнес")),
            SimpleNamespace(activity_description="Помогаю клиентам"),
            [], [], [], [slot()],
        )
        with (
            patch.object(goal.simple, "_business_snapshot", new=AsyncMock(return_value=snapshot)),
            patch.object(goal.control, "_uuid_token", side_effect=lambda value: value),
        ):
            await goal.send_goal_dashboard(out, user_id=101, business_id="business-1")
        text, kwargs = out.answers[-1]
        self.assertIn("Помогаю клиентам", text)
        self.assertIn("Если нужны новые клиенты — нажмите одну кнопку", text)
        self.assertNotIn("кампан", text.lower())
        self.assertNotIn("ID регион", text)
        labels = [button.text for row in kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertEqual(labels, ["🚀 Получить клиентов", "👥 Клиенты и запись", "⚙️ Ещё"])

    async def test_dashboard_without_slots_promises_only_needed_question(self):
        out = FakeMessage()
        snapshot = (
            "actor",
            SimpleNamespace(business=SimpleNamespace(name="Мой бизнес")),
            object(), [], [], [], [],
        )
        with (
            patch.object(goal.simple, "_business_snapshot", new=AsyncMock(return_value=snapshot)),
            patch.object(goal.control, "_uuid_token", side_effect=lambda value: value),
        ):
            await goal.send_goal_dashboard(out, user_id=101, business_id="business-1")
        self.assertIn("если понадобится, я сам попрошу его указать", out.answers[-1][0])

    async def test_helpers_choose_recent_safe_history_and_filter_program_capability(self):
        older = publication(connection_id="conn-1", campaign_id="6001", regions=(47,), updated_at="2026-08-10")
        newer = publication(connection_id="conn-2", campaign_id="7001", regions=(213,), updated_at="2026-08-11")
        active = [connection("conn-1"), connection("conn-2")]
        self.assertEqual(goal._pick_connection(active, [older, newer]).id, "conn-2")
        camps = [campaign("7001", "A"), campaign("8001", "B")]
        self.assertEqual(goal._pick_campaign(camps, [newer], connection_id="conn-2").campaign_id, "7001")
        self.assertEqual(goal._pick_regions([older, newer], connection_id="conn-2", campaign_id="7001"), (213,))
        self.assertIsNone(goal._pick_connection(active, []))
        self.assertIsNone(goal._pick_campaign(camps, [], connection_id="conn-2"))
        program = capability("program-cap", "programs")
        service = capability("service-cap", "services")
        with (
            patch.object(goal.asyncio, "to_thread", new=direct),
            patch.object(goal.control, "list_business_capabilities", return_value=[program, service]),
            patch.object(goal.control, "list_business_offerings", return_value=[offering()]) as list_offerings,
        ):
            found = await goal._find_offering("actor", "offering-1")
        self.assertEqual(found.id, "offering-1")
        list_offerings.assert_called_once_with(actor="actor", capability_id="service-cap")

    async def test_eligibility_duration_and_identity_helpers_fail_closed(self):
        self.assertEqual(len(goal._eligible_campaigns([campaign()])), 1)
        self.assertEqual(goal._eligible_campaigns([campaign(state="OFF")]), [])
        self.assertEqual(goal._eligible_campaigns([campaign(status="DRAFT")]), [])
        self.assertEqual(goal._duration_from_title("Консультация 60 минут"), 60)
        self.assertIsNone(goal._duration_from_title("Консультация"))
        self.assertIsNone(goal._duration_from_title("Консультация 999 минут"))
        message = FakeMessage()
        self.assertIs(goal._target(message), message)
        self.assertEqual(goal._user_id(message), 101)
        with self.assertRaises(ValueError):
            goal._user_id(SimpleNamespace(from_user=None))
        self.assertEqual(await goal._bot_username(message), "clientplatform_bot")

    async def test_one_click_with_saved_choices_queues_safe_draft_without_confirmation(self):
        out = FakeMessage()
        cb = callback("cpo:start:business-1", out)
        state = FakeState()
        recent = publication()
        queued = SimpleNamespace(id="job-1")
        p = self.patches(out)
        with (
            p[0], p[1], p[2], p[3], p[4], p[5],
            patch.object(goal, "ad_connections_enabled", return_value=True),
            patch.object(goal, "yandex_direct_provider_configured", return_value=True),
            patch.object(goal.control, "list_booking_slots", return_value=[slot()]),
            patch.object(goal, "list_ad_connections", return_value=[connection()]),
            patch.object(goal, "list_ad_publications", return_value=[recent]),
            patch.object(goal, "list_yandex_direct_campaigns", return_value=[campaign()]),
            patch.object(goal, "create_slot_promotion", return_value=promotion()),
            patch.object(goal, "create_ad_publication_draft", return_value=draft()) as create_draft,
            patch.object(goal, "confirm_ad_publication", return_value=queued) as confirm,
        ):
            await goal.get_clients_goal(cb, state)
        cb.answer.assert_awaited_once_with("Делаю всё сам…")
        create_draft.assert_called_once()
        confirm.assert_called_once_with(actor="actor", job_id="job-1")
        text, kwargs = out.answers[-1]
        self.assertIn("✅ Всё подготовил", text)
        self.assertIn("Показы не запущены", text)
        self.assertNotIn("Выберите кабинет", text)
        self.assertNotIn("Выберите кампанию", text)
        labels = [button.text for row in kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertIn("✨ Добавить красивую картинку · платно", labels)
        self.assertIn("📨 Отправить людям", labels)

    async def test_first_paid_run_asks_human_city_not_region_id(self):
        out = FakeMessage()
        cb = callback("cpo:start:business-1", out)
        state = FakeState()
        p = self.patches(out)
        with (
            p[0], p[1], p[2], p[3], p[4], p[5],
            patch.object(goal, "ad_connections_enabled", return_value=True),
            patch.object(goal, "yandex_direct_provider_configured", return_value=True),
            patch.object(goal.control, "list_booking_slots", return_value=[slot()]),
            patch.object(goal, "list_ad_connections", return_value=[connection()]),
            patch.object(goal, "list_ad_publications", return_value=[]),
            patch.object(goal, "list_yandex_direct_campaigns", return_value=[campaign()]),
        ):
            await goal.get_clients_goal(cb, state)
        self.assertEqual(state.state, goal.one_click.OneClickOwnerState.waiting_region)
        text, kwargs = out.answers[-1]
        self.assertIn("Где Вы хотите находить новых клиентов?", text)
        self.assertNotIn("ID", text)
        labels = [button.text for row in kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertIn("Нижний Новгород", labels)
        self.assertIn("Другой город", labels)

    async def test_multiple_ad_choices_never_expose_account_picker(self):
        out = FakeMessage()
        cb = callback("cpo:start:business-1", out)
        state = FakeState()
        p = self.patches(out)
        with (
            p[0], p[1], p[2], p[3], p[4], p[5],
            patch.object(goal, "ad_connections_enabled", return_value=True),
            patch.object(goal, "yandex_direct_provider_configured", return_value=True),
            patch.object(goal.control, "list_booking_slots", return_value=[slot()]),
            patch.object(goal, "list_ad_connections", return_value=[connection("one"), connection("two")]),
            patch.object(goal, "list_ad_publications", return_value=[]),
            patch.object(goal, "create_slot_promotion", return_value=promotion()),
        ):
            await goal.get_clients_goal(cb, state)
        text, kwargs = out.answers[-1]
        self.assertIn("безопасный вариант уже готов", text.lower())
        labels = [button.text for row in kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertFalse(any(label.startswith("Яндекс ·") for label in labels))
        self.assertIn("📨 Отправить людям", labels)
        self.assertIn("⚙️ Настроить платное продвижение", labels)

    async def test_missing_connection_returns_result_and_optional_one_time_enable(self):
        out = FakeMessage()
        cb = callback("cpo:start:business-1", out)
        state = FakeState()
        p = self.patches(out)
        oauth = SimpleNamespace(authorization_url="https://oauth.example")
        with (
            p[0], p[1], p[2], p[3], p[4], p[5],
            patch.object(goal, "ad_connections_enabled", return_value=True),
            patch.object(goal, "yandex_direct_provider_configured", return_value=True),
            patch.object(goal.control, "list_booking_slots", return_value=[slot()]),
            patch.object(goal, "list_ad_connections", return_value=[]),
            patch.object(goal, "list_ad_publications", return_value=[]),
            patch.object(goal, "start_yandex_direct_oauth", return_value=oauth),
            patch.object(goal, "create_slot_promotion", return_value=promotion()),
        ):
            await goal.get_clients_goal(cb, state)
        labels = [button.text for row in out.answers[-1][1]["reply_markup"].inline_keyboard for button in row]
        self.assertIn("📨 Отправить людям", labels)
        self.assertIn("⚡ Включить платное продвижение", labels)

    async def test_manager_without_ad_permissions_still_gets_ready_result(self):
        out = FakeMessage()
        cb = callback("cpo:start:business-1", out)
        state = FakeState()
        p = self.patches(out)
        with (
            p[0], p[1], p[2], p[3], p[4], p[5],
            patch.object(goal, "ad_connections_enabled", return_value=True),
            patch.object(goal, "yandex_direct_provider_configured", return_value=True),
            patch.object(goal.control, "list_booking_slots", return_value=[slot()]),
            patch.object(goal, "list_ad_connections", side_effect=TenantPermissionDenied("owner only")),
            patch.object(goal, "list_ad_publications", return_value=[]),
            patch.object(goal, "create_slot_promotion", return_value=promotion()),
        ):
            await goal.get_clients_goal(cb, state)
        self.assertIn("готовое объявление уже можно отправлять", out.answers[-1][0])

    async def test_missing_schedule_asks_only_business_questions(self):
        out = FakeMessage()
        cb = callback("cpo:start:business-1", out)
        state = FakeState()
        cap = capability()
        p = self.patches(out)
        with (
            p[0], p[1], p[2], p[3], p[4], p[5],
            patch.object(goal.control, "list_booking_slots", return_value=[]),
            patch.object(goal.control, "list_business_capabilities", return_value=[]),
            patch.object(goal.control, "enable_business_capability", return_value=cap) as enable,
            patch.object(goal.control, "list_business_offerings", return_value=[]),
        ):
            await goal.get_clients_goal(cb, state)
        enable.assert_called_once_with(actor="actor", connector_key="services")
        self.assertEqual(state.state, goal.GoalDrivenOwnerState.waiting_offering_title)
        self.assertIn("Как называется то, на что Вы хотите получить клиента?", out.answers[-1][0])

        out2 = FakeMessage()
        cb2 = callback("cpo:start:business-1", out2)
        state2 = FakeState()
        p2 = self.patches(out2)
        with (
            p2[0], p2[1], p2[2], p2[3], p2[4], p2[5],
            patch.object(goal.control, "list_booking_slots", return_value=[]),
            patch.object(goal.control, "list_business_capabilities", return_value=[capability()]),
            patch.object(goal.control, "list_business_offerings", return_value=[offering()]),
        ):
            await goal.get_clients_goal(cb2, state2)
        self.assertEqual(state2.state, goal.GoalDrivenOwnerState.waiting_booking_start)
        self.assertIn("Когда Вы можете принять нового клиента", out2.answers[-1][0])

    async def test_multiple_services_ask_business_choice_not_internal_setting(self):
        out = FakeMessage()
        cb = callback("cpo:start:business-1", out)
        state = FakeState()
        p = self.patches(out)
        with (
            p[0], p[1], p[2], p[3], p[4], p[5],
            patch.object(goal.control, "list_booking_slots", return_value=[]),
            patch.object(goal.control, "list_business_capabilities", return_value=[capability()]),
            patch.object(goal.control, "list_business_offerings", return_value=[offering("o1", "Консультация"), offering("o2", "Диагностика")]),
        ):
            await goal.get_clients_goal(cb, state)
        text, kwargs = out.answers[-1]
        self.assertEqual(text, "Для чего сейчас нужен новый клиент?")
        labels = [button.text for row in kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertIn("🎯 Консультация", labels)
        self.assertIn("🎯 Диагностика", labels)

    async def test_new_offering_time_and_duration_continue_automatically(self):
        state = FakeState(
            {
                "business_id": "business-1",
                "business_token": "business-1",
                "capability_id": "cap-1",
            }
        )
        message = FakeMessage("Консультация 60 минут")
        created = offering(title="Консультация 60 минут")
        p = self.patches(message)
        with (
            p[0], p[1], p[2], p[3], p[4], p[5],
            patch.object(goal.control, "create_business_offering", return_value=created),
        ):
            await goal.receive_goal_offering_title(message, state)
        self.assertEqual(state.state, goal.GoalDrivenOwnerState.waiting_booking_start)
        message.text = "20.08 12:00"
        p = self.patches(message)
        with (
            p[0], p[3], p[4], p[5],
            patch.object(goal.control, "create_booking_slot", return_value=slot()),
            patch.object(goal, "_continue_goal", new=AsyncMock()) as continue_goal,
        ):
            await goal.receive_goal_booking_start(message, state)
        continue_goal.assert_awaited_once()

        state = FakeState(
            {
                "business_id": "business-1",
                "business_token": "business-1",
                "offering_id": "offering-1",
                "offering_title": "Консультация",
                "booking_start": "20.08 12:00",
            }
        )
        message = FakeMessage("abc")
        await goal.receive_goal_booking_duration(message, state)
        self.assertIn("только число минут", message.answers[-1][0])
        message.text = "60"
        p = self.patches(message)
        with (
            p[0], p[3], p[4], p[5],
            patch.object(goal.control, "create_booking_slot", return_value=slot()),
            patch.object(goal, "_continue_goal", new=AsyncMock()) as continue_goal,
        ):
            await goal.receive_goal_booking_duration(message, state)
        continue_goal.assert_awaited_once()

    async def test_city_selection_finishes_without_provider_details(self):
        out = FakeMessage()
        cb = callback("cpo:region:47", out)
        state = FakeState(
            {
                "business_id": "business-1",
                "business_token": "business-1",
                "slot_id": "slot-1",
                "connection_id": "conn-1",
                "external_campaign_id": "6001",
                "external_campaign_name": "Основная",
            }
        )
        p = self.patches(out)
        with (
            p[0], p[1], p[2], p[3], p[4], p[5],
            patch.object(goal.control, "list_booking_slots", return_value=[slot()]),
            patch.object(goal, "_prepare_and_queue", new=AsyncMock()) as prepare,
        ):
            await goal.choose_goal_region(cb, state)
        prepare.assert_awaited_once()
        self.assertEqual(prepare.await_args.kwargs["region_ids"], (47,))

        message = FakeMessage("Екатеринбург")
        state = FakeState(
            {
                "business_id": "business-1",
                "business_token": "business-1",
                "slot_id": "slot-1",
                "connection_id": "conn-1",
                "external_campaign_id": "6001",
                "external_campaign_name": "Основная",
            }
        )
        p = self.patches(message)
        with (
            p[0], p[1], p[2], p[3], p[4], p[5],
            patch.object(goal.control, "list_booking_slots", return_value=[slot()]),
            patch.object(goal, "create_slot_promotion", return_value=promotion()),
        ):
            await goal.receive_goal_region(message, state)
        self.assertIn("не могу определить без риска ошибиться", message.answers[-1][0])
        self.assertNotIn("ID", message.answers[-1][0])

    async def test_visual_generation_is_explicit_idempotent_and_task_managed(self):
        out = FakeMessage()
        cb = callback("cpo:visual:business-1:job-1", out)
        p = self.patches(out)
        visual = SimpleNamespace(id="visual-1", status="succeeded", asset_ready=True)
        with (
            p[0], p[1], p[2], p[3], p[4], p[5],
            patch.object(goal, "list_ad_publications", return_value=[publication()]),
            patch.object(goal, "create_ad_visual", return_value=visual) as create_visual,
            patch.object(goal, "materialize_ad_visual", return_value="/tmp/image.png"),
            patch.object(goal, "FSInputFile", side_effect=lambda path: path),
        ):
            await goal.generate_goal_visual(cb)
        cb.answer.assert_awaited_once_with("Создаю картинку…")
        idem = create_visual.call_args.kwargs["idempotency_key"]
        self.assertTrue(idem.startswith("clientplatform:"))
        self.assertEqual(len(out.photos), 1)

        out = FakeMessage()
        cb = callback("cpo:visual:business-1:job-1", out)
        p = self.patches(out)
        queued = SimpleNamespace(id="visual-1", status="queued", asset_ready=False)
        tracked = Mock()
        with (
            p[0], p[1], p[2], p[3], p[4], p[5],
            patch.object(goal, "list_ad_publications", return_value=[publication()]),
            patch.object(goal, "create_ad_visual", return_value=queued),
            patch.object(goal, "_track_task", tracked),
        ):
            await goal.generate_goal_visual(cb)
        self.assertIn("Ничего больше нажимать не нужно", out.answers[-1][0])
        tracked.assert_called_once()
        tracked.call_args.args[0].close()

        coroutine = goal._finish_visual(FakeMessage(), scope_id="business-1", job_id="visual-1")
        manager = Mock()
        with patch.object(goal, "_BACKGROUND_TASKS", manager):
            goal._track_task(coroutine)
        manager.create.assert_called_once_with(
            coroutine,
            name="clientplatform-goal-visual-delivery",
        )
        coroutine.close()

    async def test_install_replaces_daily_dashboard_and_legacy_keyboard(self):
        owner = SimpleNamespace(
            _goal_driven_experience_installed=False,
            _owner_keyboard=lambda _business_id: None,
            send_owner_dashboard=lambda *args, **kwargs: None,
        )
        simple = SimpleNamespace(send_simple_dashboard=lambda *args, **kwargs: None)
        control = SimpleNamespace(_send_dashboard=lambda *args, **kwargs: None)
        one_click = SimpleNamespace(_home_keyboard=lambda _business_id: None)
        goal.install_goal_driven_experience(
            owner_module=owner,
            simple_module=simple,
            control_module=control,
            one_click_module=one_click,
        )
        self.assertIs(owner.send_owner_dashboard, goal.send_goal_dashboard)
        self.assertIs(simple.send_simple_dashboard, goal.send_goal_dashboard)
        self.assertIs(control._send_dashboard, goal.send_goal_dashboard)
        self.assertIs(owner._owner_keyboard, goal._home_keyboard)
        self.assertIs(one_click._home_keyboard, goal._home_keyboard)


if __name__ == "__main__":
    unittest.main()
