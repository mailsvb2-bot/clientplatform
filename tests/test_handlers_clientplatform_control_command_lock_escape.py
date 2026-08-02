from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.types import Chat, Message, User

from handlers.clientplatform_interaction_safety import (
    ClientPlatformInteractionSafetyMiddleware,
    _message_command,
)


def telegram_message(*, text: str, user_id: int = 7) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Тест"),
        text=text,
    )


def test_message_command_normalizes_payload_and_bot_suffix() -> None:
    assert _message_command(telegram_message(text="/start")) == "/start"
    assert (
        _message_command(
            telegram_message(text="/START@clientplatform_bot cpj_token")
        )
        == "/start"
    )
    assert _message_command(telegram_message(text="Сантехник")) is None


@pytest.mark.asyncio
async def test_start_escapes_a_busy_interaction_lock() -> None:
    middleware = ClientPlatformInteractionSafetyMiddleware()
    message = telegram_message(text="/start")
    data = {"bot": SimpleNamespace(id=1)}
    lock = middleware._lock_for(bot_id=1, chat_id=7, user_id=7)
    await lock.acquire()
    handled: list[str] = []

    async def handler(event: Any, _data: dict[str, Any]) -> str:
        handled.append(str(event.text))
        return "opened"

    started = time.monotonic()
    try:
        result = await asyncio.wait_for(
            middleware(handler, message, data),
            timeout=1.0,
        )
    finally:
        lock.release()

    assert result == "opened"
    assert handled == ["/start"]
    assert time.monotonic() - started < 0.75


@pytest.mark.asyncio
async def test_all_control_commands_escape_a_busy_interaction_lock() -> None:
    for command in ("/start", "/admin", "/mybot", "/cancel"):
        middleware = ClientPlatformInteractionSafetyMiddleware()
        message = telegram_message(text=command)
        data = {"bot": SimpleNamespace(id=1)}
        lock = middleware._lock_for(bot_id=1, chat_id=7, user_id=7)
        await lock.acquire()

        async def handler(_event: Any, _data: dict[str, Any]) -> str:
            return command

        try:
            assert (
                await asyncio.wait_for(
                    middleware(handler, message, data),
                    timeout=1.0,
                )
                == command
            )
        finally:
            lock.release()


@pytest.mark.asyncio
async def test_ordinary_text_still_waits_for_the_user_lock() -> None:
    middleware = ClientPlatformInteractionSafetyMiddleware()
    message = telegram_message(text="Сантехник")
    data = {"bot": SimpleNamespace(id=1)}
    lock = middleware._lock_for(bot_id=1, chat_id=7, user_id=7)
    await lock.acquire()

    async def handler(_event: Any, _data: dict[str, Any]) -> str:
        return "unexpected"

    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                middleware(handler, message, data),
                timeout=0.05,
            )
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_control_command_uses_and_releases_an_available_lock() -> None:
    middleware = ClientPlatformInteractionSafetyMiddleware()
    message = telegram_message(text="/admin")
    data = {"bot": SimpleNamespace(id=1)}
    lock = middleware._lock_for(bot_id=1, chat_id=7, user_id=7)

    async def handler(_event: Any, _data: dict[str, Any]) -> str:
        assert lock.locked() is True
        return "admin"

    assert await middleware(handler, message, data) == "admin"
    assert lock.locked() is False
