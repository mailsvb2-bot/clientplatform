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


def _callback(data: str, *, user_id: int = 72) -> CallbackQuery:
    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Slot add"),
        text="screen",
    )
    assert message.from_user is not None
    return CallbackQuery(
        id=f"slot-add-{user_id}",
        from_user=message.from_user,
        chat_instance="slot-add",
        message=message,
        data=data,
    )


def _state(*, user_id: int = 72) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id),
    )


@pytest.mark.asyncio
async def test_legacy_slotadd_clears_stale_replacement_state_before_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    button_surface_contract.install_button_surface_contract(interaction_safety)
    state = _state()
    await state.set_state("ClientPlatformControlState:booking_duration")
    await state.set_data(
        {
            "business_id": "old-business",
            "offering_id": "old-offering",
            "replacing_slot_id": "old-slot",
        }
    )
    observed: dict[str, Any] = {}

    async def answer_callback(
        _callback: CallbackQuery,
        text: str | None = None,
        **_kwargs: Any,
    ) -> None:
        observed.setdefault("answers", []).append(text)

    async def edit_reply_markup(
        _message: Message,
        *,
        reply_markup: Any = None,
        **_kwargs: Any,
    ) -> None:
        observed.setdefault("reply_markups", []).append(reply_markup)

    async def handler(_event: Any, data: dict[str, Any]) -> str:
        current = data["state"]
        observed["state"] = await current.get_state()
        observed["data"] = await current.get_data()
        return "handled"

    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)
    monkeypatch.setattr(Message, "edit_reply_markup", edit_reply_markup)
    callback = _callback(
        "cp:slotadd:abcdefghijklmnopqrstuv:zyxwvutsrqponmlkjihgfe"
    )
    middleware = interaction_safety.ClientPlatformInteractionSafetyMiddleware()

    result = await middleware(
        handler,
        callback,
        {"bot": type("Bot", (), {"id": 1})(), "state": state},
    )

    assert result == "handled"
    assert observed["state"] is None
    assert observed["data"] == {}
    assert observed["reply_markups"] == [None]
    assert interaction_safety._is_state_escape_callback(str(callback.data))
    assert not interaction_safety._is_repeatable_navigation(str(callback.data))
