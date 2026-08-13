from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from clientplatform.domain.bookings import BookingSlotStatus
from clientplatform.domain.tenancy import TenantPermissionDenied
from handlers import clientplatform_goal_schedule as schedule


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
        self.state = None


class FakeMessage:
    def __init__(self, text="") -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=101)
        self.bot = SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(username="clientplatform_bot"))
        )
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


def callback(data: str, out=None):
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


def capability(capability_id="cap-1", key="services"):
    return SimpleNamespace(
        id=capability_id,
        connector_key=key,
        status=schedule.CapabilityStatus.ACTIVE,
    )


def offering(offering_id="offering-1", title="Консультация"):
    return SimpleNamespace(id=offering_id, title=title)


def slot(status=BookingSlotStatus.OPEN):
    return SimpleNamespace(
        slot=SimpleNamespace(
            id="slot-1",
            status=status,
            starts_at="2026-08-20T09:00:00+00:00",
        ),
        offering_title="Консультация",
        local_start="20.08.2026 12:00",
    )


class GoalScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_delegates_to_one_click_when_open_slot_exists(self) -> None:
        cb = callback("cpo:start:business-1")
        state = FakeState()
        with (
            patch.object(schedule.control, "_token_uuid", side_effect=lambda value: value),
            patch.object(schedule.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(schedule.control, "list_booking_slots", return_value=[slot()]),
            patch.object(schedule.one_click, "get_clients_one_click", new=AsyncMock()) as delegate,
            patch.object(schedule.asyncio, "to_thread", new=direct),
        ):
            await schedule.get_clients_goal(cb, state)
        delegate.assert_awaited_once_with(cb, state)
        cb.answer.assert_not_awaited()

    async def test_start_without_slot_enters_business_schedule_flow(self) -> None:
        out = FakeMessage()
        cb = callback("cpo:start:business-1", out)
        state = FakeState()
        with (
            patch.object(schedule.control, "_token_uuid", side_effect=lambda value: value),
            patch.object(schedule.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(schedule.control, "list_booking_slots", return_value=[]),
            patch.object(schedule, "_begin_missing_schedule", new=AsyncMock()) as begin,
            patch.object(schedule.asyncio, "to_thread", new=direct),
        ):
            await schedule.get_clients_goal(cb, state)
        cb.answer.assert_awaited_once_with("Готовлю всё сам…")
        self.assertEqual(state.clear_count, 1)
        begin.assert_awaited_once()

    async def test_missing_capability_is_enabled_and_then_service_name_is_requested(self) -> None:
        out = FakeMessage()
        cb = callback("cpo:start:business-1", out)
        state = FakeState()
        created_capability = capability()
        with (
            patch.object(schedule.control, "_callback_message", return_value=out),
            patch.object(schedule.control, "list_business_capabilities", return_value=[]),
            patch.object(schedule.control, "enable_business_capability", return_value=created_capability),
            patch.object(schedule.control, "list_business_offerings", return_value=[]),
            patch.object(schedule.asyncio, "to_thread", new=direct),
        ):
            await schedule._begin_missing_schedule(
                cb,
                state,
                actor="actor",
                business_id="business-1",
                business_token="business-1",
            )
        self.assertEqual(state.state, schedule.GoalScheduleState.waiting_offering_title)
        self.assertEqual(state.data["capability_id"], "cap-1")
        self.assertIn("Как называется услуга", out.answers[-1][0])

    async def test_missing_capability_enable_failure_fails_closed(self) -> None:
        out = FakeMessage()
        cb = callback("cpo:start:business-1", out)
        with (
            patch.object(schedule.control, "_callback_message", return_value=out),
            patch.object(schedule.control, "list_business_capabilities", return_value=[]),
            patch.object(
                schedule.control,
                "enable_business_capability",
                side_effect=TenantPermissionDenied("owner only"),
            ),
            patch.object(schedule.asyncio, "to_thread", new=direct),
        ):
            await schedule._begin_missing_schedule(
                cb,
                FakeState(),
                actor="actor",
                business_id="business-1",
                business_token="business-1",
            )
        self.assertIn("Ничего опасного не изменено", out.answers[-1][0])

    async def test_single_offering_asks_only_for_time(self) -> None:
        out = FakeMessage()
        cb = callback("cpo:start:business-1", out)
        state = FakeState()
        with (
            patch.object(schedule.control, "_callback_message", return_value=out),
            patch.object(schedule.control, "list_business_capabilities", return_value=[capability()]),
            patch.object(schedule.control, "list_business_offerings", return_value=[offering()]),
            patch.object(schedule.asyncio, "to_thread", new=direct),
        ):
            await schedule._begin_missing_schedule(
                cb,
                state,
                actor="actor",
                business_id="business-1",
                business_token="business-1",
            )
        self.assertEqual(state.state, schedule.GoalScheduleState.waiting_booking_start)
        self.assertEqual(state.data["offering_id"], "offering-1")
        self.assertIn("Когда Вы можете принять", out.answers[-1][0])

    async def test_many_offerings_are_paginated_and_every_late_service_is_reachable(self) -> None:
        out = FakeMessage()
        offerings = [offering(f"off-{index}", f"Услуга {index:02d}") for index in range(18)]
        with patch.object(schedule.control, "_uuid_token", side_effect=lambda value: value):
            await schedule._show_offering_page(
                out,
                business_token="business-1",
                offerings=offerings,
                page=0,
            )
            await schedule._show_offering_page(
                out,
                business_token="business-1",
                offerings=offerings,
                page=2,
            )
        first_text, first_kwargs = out.answers[0]
        last_text, last_kwargs = out.answers[1]
        self.assertIn("страница 1/3", first_text)
        self.assertIn("страница 3/3", last_text)
        first_labels = [
            button.text
            for row in first_kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        last_labels = [
            button.text
            for row in last_kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("Дальше ➡️", first_labels)
        self.assertIn("🎯 Услуга 16", last_labels)
        self.assertIn("🎯 Услуга 17", last_labels)
        self.assertIn("⬅️ Назад", last_labels)

    async def test_offering_page_callback_reloads_current_list_and_rejects_bad_page(self) -> None:
        out = FakeMessage()
        cb = callback("cpo:offers:business-1:1", out)
        offerings = [offering(f"off-{index}", f"Услуга {index:02d}") for index in range(10)]
        with (
            patch.object(schedule.control, "_token_uuid", side_effect=lambda value: value),
            patch.object(schedule.control, "_uuid_token", side_effect=lambda value: value),
            patch.object(schedule.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(schedule.control, "_callback_message", return_value=out),
            patch.object(schedule, "_selectable_offerings", new=AsyncMock(return_value=offerings)),
        ):
            await schedule.change_goal_offering_page(cb)
        cb.answer.assert_awaited_once_with()
        self.assertIn("страница 2/2", out.answers[-1][0])

        bad = callback("cpo:offers:business-1:-1", FakeMessage())
        with patch.object(schedule.control, "_token_uuid", side_effect=lambda value: value):
            await schedule.change_goal_offering_page(bad)
        bad.answer.assert_awaited_once_with(
            "Список изменился. Нажмите «🚀 Найти новых клиентов» ещё раз.",
            show_alert=True,
        )

    async def test_offering_selection_reloads_by_id_and_stale_choice_fails_closed(self) -> None:
        out = FakeMessage()
        cb = callback("cpo:offer:business-1:offering-1", out)
        state = FakeState()
        selected = offering()
        with (
            patch.object(schedule.control, "_token_uuid", side_effect=lambda value: value),
            patch.object(schedule.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(schedule.control, "_callback_message", return_value=out),
            patch.object(schedule, "_find_offering", new=AsyncMock(return_value=selected)),
        ):
            await schedule.choose_goal_offering(cb, state)
        self.assertEqual(state.state, schedule.GoalScheduleState.waiting_booking_start)
        cb.answer.assert_awaited_once_with()

        stale = callback("cpo:offer:business-1:missing", FakeMessage())
        with (
            patch.object(schedule.control, "_token_uuid", side_effect=lambda value: value),
            patch.object(schedule.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(schedule, "_find_offering", new=AsyncMock(return_value=None)),
        ):
            await schedule.choose_goal_offering(stale, FakeState())
        stale.answer.assert_awaited_once_with(
            "Не получилось выбрать услугу. Начните ещё раз.",
            show_alert=True,
        )

    async def test_duration_is_inferred_only_from_safe_range(self) -> None:
        self.assertEqual(schedule._duration_from_title("Консультация 60 минут"), 60)
        self.assertEqual(schedule._duration_from_title("Сессия 5 мин"), 5)
        self.assertIsNone(schedule._duration_from_title("Консультация"))
        self.assertIsNone(schedule._duration_from_title("Сессия 999 минут"))

    async def test_new_service_validation_and_creation(self) -> None:
        empty = FakeMessage("   ")
        await schedule.receive_goal_offering_title(empty, FakeState())
        self.assertIn("короткое название", empty.answers[-1][0])

        stale = FakeMessage("Консультация")
        stale_state = FakeState({})
        await schedule.receive_goal_offering_title(stale, stale_state)
        self.assertEqual(stale_state.clear_count, 1)
        self.assertIn("шаг уже устарел", stale.answers[-1][0])

        message = FakeMessage("Консультация 60 минут")
        state = FakeState(
            {
                "business_id": "business-1",
                "business_token": "business-1",
                "capability_id": "cap-1",
            }
        )
        created = offering(title="Консультация 60 минут")
        with (
            patch.object(schedule.control, "_user_id", return_value=101),
            patch.object(schedule.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(schedule.control, "create_business_offering", return_value=created),
            patch.object(schedule.asyncio, "to_thread", new=direct),
        ):
            await schedule.receive_goal_offering_title(message, state)
        self.assertEqual(state.state, schedule.GoalScheduleState.waiting_booking_start)
        self.assertEqual(state.data["offering_id"], "offering-1")

    async def test_new_service_permission_error_is_recoverable(self) -> None:
        message = FakeMessage("Консультация")
        state = FakeState(
            {
                "business_id": "business-1",
                "business_token": "business-1",
                "capability_id": "cap-1",
            }
        )
        with (
            patch.object(schedule.control, "_user_id", return_value=101),
            patch.object(
                schedule.control,
                "_actor",
                new=AsyncMock(side_effect=TenantPermissionDenied("no")),
            ),
        ):
            await schedule.receive_goal_offering_title(message, state)
        self.assertIn("Не получилось сохранить название", message.answers[-1][0])

    async def test_booking_start_infers_duration_and_resumes_creation(self) -> None:
        message = FakeMessage("20.08.2026 12:00")
        state = FakeState(
            {
                "business_id": "business-1",
                "business_token": "business-1",
                "offering_id": "offering-1",
                "offering_title": "Консультация 60 минут",
            }
        )
        with patch.object(schedule, "_create_slot_and_resume", new=AsyncMock()) as create:
            await schedule.receive_goal_booking_start(message, state)
        create.assert_awaited_once()
        self.assertEqual(create.await_args.kwargs["duration"], 60)
        self.assertEqual(create.await_args.kwargs["data"]["booking_start"], "20.08.2026 12:00")

    async def test_booking_start_without_duration_asks_one_more_business_question(self) -> None:
        message = FakeMessage("20.08.2026 12:00")
        state = FakeState(
            {
                "business_id": "business-1",
                "business_token": "business-1",
                "offering_id": "offering-1",
                "offering_title": "Консультация",
            }
        )
        await schedule.receive_goal_booking_start(message, state)
        self.assertEqual(state.state, schedule.GoalScheduleState.waiting_booking_duration)
        self.assertIn("Сколько минут", message.answers[-1][0])

    async def test_empty_booking_start_and_invalid_duration_are_rejected(self) -> None:
        empty = FakeMessage("   ")
        await schedule.receive_goal_booking_start(empty, FakeState())
        self.assertIn("Напишите дату и время", empty.answers[-1][0])

        invalid = FakeMessage("4")
        state = FakeState({"booking_start": "20.08.2026 12:00"})
        await schedule.receive_goal_booking_duration(invalid, state)
        self.assertIn("только число минут", invalid.answers[-1][0])

    async def test_valid_duration_creates_slot(self) -> None:
        message = FakeMessage("90")
        state = FakeState({"booking_start": "20.08.2026 12:00"})
        with patch.object(schedule, "_create_slot_and_resume", new=AsyncMock()) as create:
            await schedule.receive_goal_booking_duration(message, state)
        create.assert_awaited_once_with(message, state, data=state.data, duration=90)

    async def test_slot_creation_failure_keeps_user_on_time_question(self) -> None:
        message = FakeMessage()
        state = FakeState(
            {
                "business_id": "business-1",
                "business_token": "business-1",
                "offering_id": "offering-1",
                "booking_start": "20.08.2026 12:00",
            }
        )
        with (
            patch.object(schedule.control, "_user_id", return_value=101),
            patch.object(schedule.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(schedule.control, "create_booking_slot", side_effect=ValueError("bad")),
            patch.object(schedule.asyncio, "to_thread", new=direct),
        ):
            await schedule._create_slot_and_resume(message, state, data=state.data, duration=60)
        self.assertEqual(state.state, schedule.GoalScheduleState.waiting_booking_start)
        self.assertIn("не получилось сохранить", message.answers[-1][0])

    async def test_slot_creation_success_resumes_canonical_one_click_automatically(self) -> None:
        message = FakeMessage()
        state = FakeState(
            {
                "business_id": "business-1",
                "business_token": "business-1",
                "offering_id": "offering-1",
                "booking_start": "20.08.2026 12:00",
            }
        )
        with (
            patch.object(schedule.control, "_user_id", return_value=101),
            patch.object(schedule.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(schedule.control, "create_booking_slot", return_value=slot()),
            patch.object(schedule.one_click, "get_clients_one_click", new=AsyncMock()) as resume,
            patch.object(schedule.asyncio, "to_thread", new=direct),
        ):
            await schedule._create_slot_and_resume(message, state, data=state.data, duration=60)
        self.assertIn("Продолжаю готовить", message.answers[-1][0])
        resume.assert_awaited_once()
        adapter = resume.await_args.args[0]
        self.assertEqual(adapter.data, "cpo:start:business-1")
        self.assertEqual(adapter.from_user.id, 101)
        self.assertIs(adapter.bot, message.bot)
        await adapter.answer("ignored")

    async def test_slot_creation_with_stale_state_fails_without_side_effect(self) -> None:
        message = FakeMessage()
        state = FakeState({})
        with patch.object(schedule.control, "create_booking_slot", new=Mock()) as create:
            await schedule._create_slot_and_resume(message, state, data={}, duration=60)
        create.assert_not_called()
        self.assertEqual(state.clear_count, 1)
        self.assertIn("шаг уже устарел", message.answers[-1][0])


if __name__ == "__main__":
    unittest.main()
