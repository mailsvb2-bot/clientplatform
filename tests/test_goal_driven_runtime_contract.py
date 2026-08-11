from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from clientplatform.domain.bookings import BookingSlotStatus
from handlers import clientplatform_goal_driven_runtime_contract as runtime


class _Message:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.answers: list[tuple[str, object | None]] = []

    async def answer(self, text: str, reply_markup=None) -> None:
        self.answers.append((text, reply_markup))


class GoalDrivenRuntimeContractTests(unittest.IsolatedAsyncioTestCase):
    def test_event_target_routes_callback_shape_but_keeps_message(self) -> None:
        target = _Message()
        control = SimpleNamespace(_callback_message=Mock(return_value=target))
        callback_adapter = SimpleNamespace(
            data="cpo:start:business-1",
            message=target,
        )

        self.assertIs(
            runtime._event_target(callback_adapter, control_module=control),
            target,
        )
        control._callback_message.assert_called_once_with(callback_adapter)

        control._callback_message.reset_mock()
        message = _Message("hello")
        self.assertIs(runtime._event_target(message, control_module=control), message)
        control._callback_message.assert_not_called()

    async def test_install_preserves_status_and_minimal_result_actions(self) -> None:
        message = _Message()
        open_slot = SimpleNamespace(
            slot=SimpleNamespace(status=BookingSlotStatus.OPEN)
        )
        booked_slot = SimpleNamespace(
            slot=SimpleNamespace(status=BookingSlotStatus.BOOKED)
        )
        capability = SimpleNamespace(id="cap-1")
        snapshot = (
            "actor",
            SimpleNamespace(business=SimpleNamespace(name="Мой бизнес")),
            SimpleNamespace(activity_description="Помогаю клиентам"),
            [capability],
            [object(), object()],
            [object()],
            [open_slot],
        )
        simple = SimpleNamespace(
            _business_snapshot=AsyncMock(return_value=snapshot),
            send_simple_dashboard=None,
        )
        owner = SimpleNamespace(
            _all_offerings=AsyncMock(return_value=[object(), object(), object()]),
            send_owner_dashboard=None,
        )
        markup = object()
        goal = SimpleNamespace(
            _runtime_contract_installed=False,
            _home_keyboard=Mock(return_value=markup),
            send_goal_dashboard=None,
            _target=None,
        )
        control = SimpleNamespace(
            _callback_message=Mock(),
            list_booking_slots=Mock(return_value=[open_slot, booked_slot]),
            _send_dashboard=None,
        )

        runtime.install_goal_runtime_contract(
            goal_module=goal,
            owner_module=owner,
            simple_module=simple,
            control_module=control,
        )
        await goal.send_goal_dashboard(
            message,
            user_id=101,
            business_id="business-1",
        )

        text, reply_markup = message.answers[-1]
        self.assertIn("Мой бизнес", text)
        self.assertIn("Помогаю клиентам", text)
        self.assertIn("Услуг: 3", text)
        self.assertIn("свободных времён: 1", text)
        self.assertIn("записей клиентов: 1", text)
        self.assertIn("Материалов и программ: 1", text)
        self.assertIn("клиентов: 2", text)
        self.assertIn("нажмите одну кнопку", text)
        self.assertIs(reply_markup, markup)
        owner._all_offerings.assert_awaited_once_with("actor", [capability])
        control.list_booking_slots.assert_called_once_with(
            actor="actor",
            include_unavailable=True,
        )
        self.assertIs(owner.send_owner_dashboard, goal.send_goal_dashboard)
        self.assertIs(simple.send_simple_dashboard, goal.send_goal_dashboard)
        self.assertIs(control._send_dashboard, goal.send_goal_dashboard)

    async def test_dashboard_without_capabilities_uses_snapshot_without_extra_query(self) -> None:
        message = _Message()
        open_slot = SimpleNamespace(
            slot=SimpleNamespace(status=BookingSlotStatus.OPEN)
        )
        snapshot = (
            "actor",
            SimpleNamespace(business=SimpleNamespace(name="Новый бизнес")),
            SimpleNamespace(activity_description=""),
            [],
            [],
            [],
            [open_slot],
        )
        simple = SimpleNamespace(
            _business_snapshot=AsyncMock(return_value=snapshot),
            send_simple_dashboard=None,
        )
        owner = SimpleNamespace(
            _all_offerings=AsyncMock(return_value=[]),
            send_owner_dashboard=None,
        )
        goal = SimpleNamespace(
            _runtime_contract_installed=False,
            _home_keyboard=Mock(return_value="markup"),
            send_goal_dashboard=None,
            _target=None,
        )
        control = SimpleNamespace(
            _callback_message=Mock(),
            list_booking_slots=Mock(),
            _send_dashboard=None,
        )

        runtime.install_goal_runtime_contract(
            goal_module=goal,
            owner_module=owner,
            simple_module=simple,
            control_module=control,
        )
        await goal.send_goal_dashboard(
            message,
            user_id=101,
            business_id="business-1",
        )

        text, _ = message.answers[-1]
        self.assertIn("Услуг: 0", text)
        self.assertIn("свободных времён: 1", text)
        owner._all_offerings.assert_not_awaited()
        control.list_booking_slots.assert_not_called()


if __name__ == "__main__":
    unittest.main()
