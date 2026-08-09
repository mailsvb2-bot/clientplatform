from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User

from handlers import clientplatform_interaction_safety as safety


class _Bot:
    id = 1


def _callback(data: str, *, user_id: int = 871) -> CallbackQuery:
    user = User(id=user_id, is_bot=False, first_name="Interaction")
    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=user,
        text="screen",
    )
    return CallbackQuery(
        id=f"interaction-{user_id}-{data}",
        from_user=user,
        chat_instance="interaction-ownership",
        message=message,
        data=data,
    )


@pytest.mark.asyncio
async def test_foreign_callback_namespace_bypasses_clientplatform_dedup_and_locking() -> None:
    middleware = safety.ClientPlatformInteractionSafetyMiddleware()
    calls: list[str] = []

    async def handler(event: Any, _data: dict[str, Any]) -> str:
        calls.append(str(event.data))
        return "foreign"

    callback = _callback("payments:confirm:42")
    data = {"bot": _Bot()}

    assert await middleware(handler, callback, data) == "foreign"
    assert await middleware(handler, callback, data) == "foreign"
    assert calls == ["payments:confirm:42", "payments:confirm:42"]
    assert middleware._locks == {}
    assert middleware._lock_users == {}
    assert middleware._recent_actions == {}


@pytest.mark.asyncio
async def test_mutating_callback_handler_owns_first_semantic_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    middleware = safety.ClientPlatformInteractionSafetyMiddleware()
    answers: list[tuple[str | None, bool]] = []

    async def answer_callback(
        _callback: CallbackQuery,
        text: str | None = None,
        *,
        show_alert: bool = False,
        **_kwargs: Any,
    ) -> None:
        answers.append((text, show_alert))

    async def handler(event: CallbackQuery, _data: dict[str, Any]) -> str:
        await event.answer("Профиль пока нельзя завершить", show_alert=True)
        return "validated"

    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)
    callback = _callback("cp:finish:abcdefghijklmnopqrstuv")

    assert await middleware(handler, callback, {"bot": _Bot()}) == "validated"
    assert answers[0] == ("Профиль пока нельзя завершить", True)
    assert answers[-1] == (None, False)
    assert middleware._locks == {}
    assert middleware._lock_users == {}


@pytest.mark.asyncio
async def test_repeatable_navigation_serializes_and_evicts_principal_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    middleware = safety.ClientPlatformInteractionSafetyMiddleware()
    active = 0
    max_active = 0
    answers: list[str | None] = []

    async def answer_callback(
        _callback: CallbackQuery,
        text: str | None = None,
        **_kwargs: Any,
    ) -> None:
        answers.append(text)

    async def handler(_event: Any, _data: dict[str, Any]) -> str:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "handled"

    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)
    data = {"bot": SimpleNamespace(id=1)}

    results = await asyncio.gather(
        middleware(handler, _callback("cpp:stats:one"), data),
        middleware(handler, _callback("cpp:stats:two"), data),
    )

    assert results == ["handled", "handled"]
    assert max_active == 1
    assert answers == [None, None]
    assert middleware._locks == {}
    assert middleware._lock_users == {}
