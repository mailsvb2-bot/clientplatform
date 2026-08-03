from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from handlers import clientplatform_control as control
from handlers import clientplatform_entry as entry


class FakeUser:
    def __init__(self, user_id: int = 101) -> None:
        self.id = user_id
        self.username = "dual_role"
        self.full_name = "Dual Role"


class FakeMessage:
    def __init__(self, *, user_id: int = 101, text: str = "/start") -> None:
        self.from_user = FakeUser(user_id)
        self.text = text
        self.answers: list[tuple[str, dict[str, Any]]] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))


class FakeCallback:
    def __init__(self, data: str, *, user_id: int = 101) -> None:
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage(user_id=user_id)
        self.answers: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        self.answers.append((args, kwargs))


class FakeState:
    def __init__(self) -> None:
        self.states: list[Any] = []
        self.clear_count = 0

    async def set_state(self, value: Any) -> None:
        self.states.append(value)

    async def clear(self) -> None:
        self.clear_count += 1


async def direct_to_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


def access(name: str) -> Any:
    return SimpleNamespace(business=SimpleNamespace(id=str(uuid4()), name=name))


def link(name: str) -> Any:
    return SimpleNamespace(
        business_id=str(uuid4()),
        business_name=name,
        customer_id=str(uuid4()),
    )


class ClientPlatformDualRoleEntryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.message_type_patch = patch.object(control, "Message", FakeMessage)
        self.thread_patch = patch.object(entry.asyncio, "to_thread", direct_to_thread)
        self.message_type_patch.start()
        self.thread_patch.start()
        self.addCleanup(self.message_type_patch.stop)
        self.addCleanup(self.thread_patch.stop)

    def test_direct_entry_module_import_is_safe(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import importlib; "
                    "entry = importlib.import_module('handlers.clientplatform_entry'); "
                    "control = importlib.import_module('handlers.clientplatform_control'); "
                    "assert control.router is entry.router; "
                    "assert getattr(control, '_dual_role_entry_composed', False)"
                ),
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    async def test_dual_role_start_shows_explicit_workspace_choice(self) -> None:
        with (
            patch.object(entry, "list_accessible_businesses", return_value=[access("Мой бизнес")]),
            patch.object(entry, "list_customer_businesses", return_value=[link("Мой специалист")]),
        ):
            message = FakeMessage()
            state = FakeState()
            await entry.clientplatform_entry_start(message, state)

        self.assertEqual(state.clear_count, 1)
        text, kwargs = message.answers[-1]
        self.assertIn("два рабочих пространства", text)
        labels = [button.text for row in kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertEqual(labels, ["Мои бизнесы", "Мои специалисты и программы"])

    async def test_role_choice_callbacks_recheck_live_access(self) -> None:
        business = access("Практика")
        customer_link = link("Специалист")
        resumed: list[str] = []
        portals: list[list[object]] = []

        async def fake_resume(_message: Any, **kwargs: Any) -> None:
            resumed.append(kwargs["business_id"])

        async def fake_portal(_message: Any, *, links: list[object]) -> None:
            portals.append(links)

        with (
            patch.object(entry, "list_accessible_businesses", return_value=[business]),
            patch.object(entry, "list_customer_businesses", return_value=[customer_link]),
            patch.object(control, "_resume_business", fake_resume),
            patch.object(control, "_send_client_portal", fake_portal),
        ):
            await entry.open_business_workspace(FakeCallback("cp:entry:businesses"), FakeState())
            await entry.open_customer_workspace(FakeCallback("cp:entry:clients"), FakeState())

        self.assertEqual(resumed, [business.business.id])
        self.assertEqual(portals, [[customer_link]])

    async def test_single_role_and_new_user_paths_remain_intact(self) -> None:
        customer_link = link("Специалист")
        portals: list[list[object]] = []

        async def fake_portal(_message: Any, *, links: list[object]) -> None:
            portals.append(links)

        with (
            patch.object(entry, "list_accessible_businesses", return_value=[]),
            patch.object(entry, "list_customer_businesses", return_value=[customer_link]),
            patch.object(control, "_send_client_portal", fake_portal),
        ):
            client_state = FakeState()
            await entry.clientplatform_entry_start(FakeMessage(), client_state)

        self.assertEqual(client_state.clear_count, 1)
        self.assertEqual(portals, [[customer_link]])

        with (
            patch.object(entry, "list_accessible_businesses", return_value=[]),
            patch.object(entry, "list_customer_businesses", return_value=[]),
        ):
            new_state = FakeState()
            new_message = FakeMessage()
            await entry.clientplatform_entry_start(new_message, new_state)

        self.assertEqual(new_state.states, [])
        self.assertIn("цифровой помощник", new_message.answers[-1][0])
        button = new_message.answers[-1][1]["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.text, "🚀 Запустить мой бизнес")
        self.assertEqual(button.callback_data, "cps:start")

    async def test_owner_only_paths_preserve_single_and_multiple_business_behavior(self) -> None:
        first = access("Первый")
        second = access("Второй")
        with (
            patch.object(entry, "list_accessible_businesses", return_value=[first, second]),
            patch.object(entry, "list_customer_businesses", return_value=[]),
        ):
            multiple_message = FakeMessage()
            multiple_state = FakeState()
            await entry.clientplatform_entry_start(multiple_message, multiple_state)
        self.assertEqual(multiple_state.clear_count, 1)
        self.assertIn("Выберите бизнес", multiple_message.answers[-1][0])

        resumed: list[str] = []

        async def fake_resume(_message: Any, **kwargs: Any) -> None:
            resumed.append(kwargs["business_id"])

        with (
            patch.object(entry, "list_accessible_businesses", return_value=[first]),
            patch.object(entry, "list_customer_businesses", return_value=[]),
            patch.object(control, "_resume_business", fake_resume),
        ):
            await entry.clientplatform_entry_start(FakeMessage(), FakeState())
        self.assertEqual(resumed, [first.business.id])

    async def test_invite_payload_remains_supported(self) -> None:
        business_id = str(uuid4())
        claim = SimpleNamespace(
            business_id=business_id,
            business_name="Практика",
            already_connected=False,
        )
        with patch.object(entry, "claim_customer_invite", return_value=claim):
            message = FakeMessage(text="/start cpj_secret-token")
            state = FakeState()
            await entry.clientplatform_entry_start(message, state)

        self.assertEqual(state.clear_count, 1)
        self.assertIn("Подключение завершено", message.answers[-1][0])
        callbacks = [
            button.callback_data
            for row in message.answers[-1][1]["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertTrue(any(value.startswith("cp:cprograms:") for value in callbacks))

    async def test_stale_role_callbacks_fail_closed(self) -> None:
        business_callback = FakeCallback("cp:entry:businesses")
        client_callback = FakeCallback("cp:entry:clients")
        with (
            patch.object(entry, "list_accessible_businesses", return_value=[]),
            patch.object(entry, "list_customer_businesses", return_value=[]),
        ):
            await entry.open_business_workspace(business_callback, FakeState())
            await entry.open_customer_workspace(client_callback, FakeState())

        self.assertIn("Активных бизнесов", business_callback.message.answers[-1][0])
        self.assertIn("Активных подключений", client_callback.message.answers[-1][0])

    async def test_entry_error_delegates_to_existing_fail_closed_handler(self) -> None:
        delegated = AsyncMock(return_value=True)
        event = object()
        with patch.object(control, "clientplatform_control_error", delegated):
            handled = await entry.clientplatform_entry_error(event)
        self.assertTrue(handled)
        delegated.assert_awaited_once_with(event)


if __name__ == "__main__":
    unittest.main()
