from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from handlers import clientplatform_control as control
from handlers import clientplatform_entry as entry


class _Message:
    def __init__(self, *, text: str) -> None:
        self.text = text
        self.from_user = SimpleNamespace(
            id=501,
            username="client",
            full_name="Client User",
        )
        self.answers: list[str] = []

    async def answer(self, text: str, **_kwargs: Any) -> None:
        self.answers.append(text)


class _State:
    def __init__(self) -> None:
        self.clear_count = 0

    async def clear(self) -> None:
        self.clear_count += 1


async def _direct_to_thread(
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    return func(*args, **kwargs)


class ClientPlatformManagedBotEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_managed_bot_context_precedes_invite_payload(self) -> None:
        business_id = str(uuid4())
        customer_link = SimpleNamespace(
            business_id=business_id,
            business_name="Практика",
            customer_id=str(uuid4()),
        )
        claim = Mock(side_effect=AssertionError("foreign invite must not be claimed"))
        portal = AsyncMock()
        message = _Message(text="/start cpj_foreign-token")
        state = _State()
        with (
            patch.object(entry.asyncio, "to_thread", _direct_to_thread),
            patch.object(entry, "list_customer_businesses", return_value=[customer_link]),
            patch.object(entry, "claim_customer_invite", claim),
            patch.object(control, "_send_client_portal", portal),
        ):
            await entry.clientplatform_entry_start(
                message,
                state,
                managed_bot_business_id=business_id,
            )
        claim.assert_not_called()
        portal.assert_awaited_once_with(message, links=[customer_link])
        self.assertEqual(state.clear_count, 1)

    async def test_managed_bot_cannot_fall_back_to_owner_onboarding(self) -> None:
        business_id = str(uuid4())
        message = _Message(text="/start")
        state = _State()
        with (
            patch.object(entry.asyncio, "to_thread", _direct_to_thread),
            patch.object(entry, "list_customer_businesses", return_value=[]),
        ):
            await entry.clientplatform_entry_start(
                message,
                state,
                managed_bot_business_id=business_id,
            )
        self.assertEqual(state.clear_count, 1)
        self.assertTrue(message.answers)
        self.assertIn("Не удалось открыть кабинет", message.answers[-1])


if __name__ == "__main__":
    unittest.main()
