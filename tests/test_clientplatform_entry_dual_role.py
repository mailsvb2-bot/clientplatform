from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import handlers

entry, control = handlers._load_clientplatform_modules()


class FakeMessage:
    def __init__(self, *, telegram_user_id: int, text: str = "/start") -> None:
        self.from_user = SimpleNamespace(
            id=telegram_user_id,
            username=f"user_{telegram_user_id}",
            full_name=f"User {telegram_user_id}",
        )
        self.text = text
        self.answers: list[tuple[str, object | None]] = []

    async def answer(self, text: str, *, reply_markup=None, **_kwargs) -> None:
        self.answers.append((text, reply_markup))


class FakeState:
    def __init__(self) -> None:
        self.clear_count = 0
        self.state = None
        self.data: dict[str, object] = {}

    async def clear(self) -> None:
        self.clear_count += 1
        self.state = None
        self.data = {}

    async def set_state(self, state) -> None:
        self.state = state

    async def update_data(self, **values) -> None:
        self.data.update(values)


class ClientPlatformEntryCompositionTests(unittest.TestCase):
    def test_lazy_handler_export_uses_entry_router_idempotently(self) -> None:
        first_entry, first_control = handlers._load_clientplatform_modules()
        subrouter_count = len(first_entry.router.sub_routers)
        second_entry, second_control = handlers._load_clientplatform_modules()

        self.assertIs(first_entry, second_entry)
        self.assertIs(first_control, second_control)
        self.assertIs(first_control.router, first_entry.router)
        self.assertTrue(first_control._dual_role_entry_composed)
        self.assertEqual(len(second_entry.router.sub_routers), subrouter_count)


class ClientPlatformDualRoleEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_customer_sees_both_workspaces(self) -> None:
        owner_business_id = str(uuid4())
        customer_business_id = str(uuid4())
        access = SimpleNamespace(
            business=SimpleNamespace(
                id=owner_business_id,
                name="Моя практика",
            )
        )
        link = SimpleNamespace(
            business_id=customer_business_id,
            business_name="Другой специалист",
        )
        message = FakeMessage(telegram_user_id=700)
        state = FakeState()

        with (
            patch.object(entry, "list_accessible_businesses", return_value=[access]),
            patch.object(entry, "list_customer_businesses", return_value=[link]),
            patch.object(control, "_resume_business", new=AsyncMock()) as resume,
            patch.object(control, "_send_client_portal", new=AsyncMock()) as portal,
        ):
            await entry.clientplatform_entry_start(message, state)

        self.assertEqual(state.clear_count, 1)
        self.assertEqual(len(message.answers), 1)
        text, markup = message.answers[0]
        self.assertIn("два рабочих пространства", text)
        self.assertIsNotNone(markup)
        callbacks = [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        ]
        labels = [
            button.text
            for row in markup.inline_keyboard
            for button in row
        ]
        self.assertEqual(
            callbacks,
            ["cp:entry:businesses", "cp:entry:clients"],
        )
        self.assertEqual(
            labels,
            ["Мои бизнесы", "Мои специалисты и программы"],
        )
        resume.assert_not_awaited()
        portal.assert_not_awaited()

    async def test_owner_only_still_enters_owner_workspace_directly(self) -> None:
        business_id = str(uuid4())
        access = SimpleNamespace(
            business=SimpleNamespace(id=business_id, name="Моя практика")
        )
        message = FakeMessage(telegram_user_id=701)
        state = FakeState()
        resume = AsyncMock()

        with (
            patch.object(entry, "list_accessible_businesses", return_value=[access]),
            patch.object(entry, "list_customer_businesses", return_value=[]),
            patch.object(control, "_resume_business", new=resume),
        ):
            await entry.clientplatform_entry_start(message, state)

        resume.assert_awaited_once_with(
            message,
            user_id=701,
            business_id=business_id,
            state=state,
        )
        self.assertEqual(message.answers, [])

    async def test_customer_only_still_enters_customer_portal_directly(self) -> None:
        link = SimpleNamespace(
            business_id=str(uuid4()),
            business_name="Другой специалист",
        )
        message = FakeMessage(telegram_user_id=702)
        state = FakeState()
        portal = AsyncMock()

        with (
            patch.object(entry, "list_accessible_businesses", return_value=[]),
            patch.object(entry, "list_customer_businesses", return_value=[link]),
            patch.object(control, "_send_client_portal", new=portal),
        ):
            await entry.clientplatform_entry_start(message, state)

        self.assertEqual(state.clear_count, 1)
        portal.assert_awaited_once_with(message, links=[link])


if __name__ == "__main__":
    unittest.main()
