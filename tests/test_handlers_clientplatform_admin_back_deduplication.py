from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User

from handlers import clientplatform_button_surface_contract as button_surface_contract
from handlers import clientplatform_interaction_safety as interaction_safety


def _callback(data: str, *, user_id: int = 71) -> CallbackQuery:
    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Admin navigation"),
        text="screen",
    )
    assert message.from_user is not None
    return CallbackQuery(
        id=f"admin-back-{user_id}-{data}",
        from_user=message.from_user,
        chat_instance="admin-back",
        message=message,
        data=data,
    )


def _state(*, user_id: int = 71) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id),
    )


def test_admin_stack_back_is_not_repeatable_navigation() -> None:
    button_surface_contract.install_button_surface_contract(interaction_safety)

    token = "abcdefghijklmnopqrstuv"
    assert not interaction_safety._is_repeatable_navigation(f"cpa:{token}:back")
    assert interaction_safety._is_state_escape_callback(f"cpa:{token}:back")
    assert interaction_safety._is_repeatable_navigation(f"cpa:{token}:menu")
    assert interaction_safety._is_repeatable_navigation(f"cpa:{token}:leave")


@pytest.mark.asyncio
async def test_admin_stack_back_rapid_double_tap_is_deduplicated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    button_surface_contract.install_button_surface_contract(interaction_safety)
    callback_answers: list[str | None] = []
    handled = 0

    async def answer_callback(
        _callback: CallbackQuery,
        text: str | None = None,
        **_kwargs: Any,
    ) -> None:
        callback_answers.append(text)

    async def handler(_event: Any, _data: dict[str, Any]) -> str:
        nonlocal handled
        handled += 1
        return "handled"

    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)
    middleware = interaction_safety.ClientPlatformInteractionSafetyMiddleware()
    callback = _callback("cpa:abcdefghijklmnopqrstuv:back")
    data = {"bot": type("Bot", (), {"id": 1})(), "state": _state()}

    assert await middleware(handler, callback, data) == "handled"
    assert await middleware(handler, callback, data) is None
    assert handled == 1
    assert callback_answers[-1] == "Действие уже выполняется."
