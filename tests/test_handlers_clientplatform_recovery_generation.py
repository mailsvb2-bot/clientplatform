from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User

from handlers import clientplatform_button_surface_contract as button_surface_contract
from handlers import clientplatform_interaction_safety as interaction_safety


class _Bot:
    id = 1


def _state(*, user_id: int) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id),
    )


def _message(text: str, *, user_id: int) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Recovery"),
        text=text,
    )


def _callback(data: str, *, user_id: int) -> CallbackQuery:
    message = _message("screen", user_id=user_id)
    assert message.from_user is not None
    return CallbackQuery(
        id=f"recovery-generation-{user_id}",
        from_user=message.from_user,
        chat_instance="recovery-generation",
        message=message,
        data=data,
    )


@pytest.mark.asyncio
async def test_recovery_generation_rejects_late_stale_callback_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    button_surface_contract.install_button_surface_contract(interaction_safety)
    monkeypatch.setattr(
        interaction_safety,
        "_CONTROL_COMMAND_LOCK_WAIT_SECONDS",
        0.01,
    )

    async def answer_callback(
        _callback: CallbackQuery,
        text: str | None = None,
        *,
        show_alert: bool = False,
        **_kwargs: Any,
    ) -> None:
        del text, show_alert

    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)

    user_id = 940060
    state = _state(user_id=user_id)
    await state.set_state("OldWizard:waiting")
    await state.set_data({"old": True})
    middleware = interaction_safety.ClientPlatformInteractionSafetyMiddleware()
    entered = asyncio.Event()
    release = asyncio.Event()
    stale_write_completed = False

    async def slow_callback_handler(_event: Any, data: dict[str, Any]) -> str:
        nonlocal stale_write_completed
        entered.set()
        await release.wait()
        await data["state"].update_data(stale=True)
        await data["state"].set_state("OldWizard:resurrected")
        stale_write_completed = True
        return "old-callback"

    async def recovery_handler(_event: Any, data: dict[str, Any]) -> str:
        await data["state"].clear()
        return "recovered"

    async def run_old_callback() -> Any:
        return await middleware(
            slow_callback_handler,
            _callback("cpj:home:abcdefghijklmnopqrstuv", user_id=user_id),
            {"bot": _Bot(), "state": state},
        )

    async def run_recovery() -> Any:
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        result = await asyncio.wait_for(
            middleware(
                recovery_handler,
                _message("/cancel", user_id=user_id),
                {"bot": _Bot(), "state": state},
            ),
            timeout=1.0,
        )
        assert result == "recovered"
        assert await state.get_state() is None
        assert await state.get_data() == {}
        release.set()
        return result

    old_result, recovery_result = await asyncio.wait_for(
        asyncio.gather(run_old_callback(), run_recovery()),
        timeout=2.0,
    )

    assert recovery_result == "recovered"
    assert old_result is None
    assert stale_write_completed is False
    assert await state.get_state() is None
    assert await state.get_data() == {}
