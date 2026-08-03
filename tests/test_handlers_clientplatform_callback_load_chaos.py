from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User

from handlers import clientplatform_interaction_safety as safety

# This suite intentionally uses real aiogram CallbackQuery objects and runs
# in the full runtime contour on every pull-request head.


class FakeState:
    async def get_state(self) -> None:
        return None


def _callback(index: int, *, data: str | None = None) -> CallbackQuery:
    user = User(id=910001, is_bot=False, first_name="Tester")
    message = Message(
        message_id=1000 + index,
        date=datetime.now(timezone.utc),
        chat=Chat(id=910001, type="private"),
        from_user=user,
        text="admin",
    )
    return CallbackQuery(
        id=f"callback-{index}",
        from_user=user,
        chat_instance="load-test",
        message=message,
        data=data or f"cpa:invalid-token:section-{index}",
    )


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


@pytest.mark.asyncio
async def test_one_hundred_real_callback_objects_remain_low_latency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    middleware = safety.ClientPlatformInteractionSafetyMiddleware()
    acknowledgements = 0

    async def answer_callback(*_args: Any, **_kwargs: Any) -> None:
        nonlocal acknowledgements
        acknowledgements += 1

    async def handler(_event: Any, _data: dict[str, Any]) -> str:
        await asyncio.sleep(0)
        return "ok"

    monkeypatch.setattr(safety, "_answer_callback", answer_callback)
    durations: list[float] = []
    for index in range(100):
        started = time.perf_counter()
        result = await middleware(
            handler,
            _callback(index),
            {
                "bot": SimpleNamespace(id=8534548177),
                "state": FakeState(),
            },
        )
        durations.append((time.perf_counter() - started) * 1000)
        assert result == "ok"

    assert acknowledgements == 100
    assert _p95(durations) < 100
    assert max(durations) < 250


@pytest.mark.asyncio
async def test_callback_is_acknowledged_before_waiting_for_busy_user_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    middleware = safety.ClientPlatformInteractionSafetyMiddleware()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_acknowledged = asyncio.Event()
    handler_order: list[str] = []

    async def answer_callback(callback: CallbackQuery, *_args: Any, **_kwargs: Any) -> None:
        if callback.data and callback.data.endswith("second"):
            second_acknowledged.set()

    async def handler(event: CallbackQuery, _data: dict[str, Any]) -> None:
        label = str(event.data).rsplit(":", 1)[-1]
        handler_order.append(f"start:{label}")
        if label == "first":
            first_entered.set()
            await release_first.wait()
        handler_order.append(f"finish:{label}")

    monkeypatch.setattr(safety, "_answer_callback", answer_callback)
    data = {
        "bot": SimpleNamespace(id=8534548177),
        "state": FakeState(),
    }

    async with asyncio.TaskGroup() as group:
        group.create_task(
            middleware(
                handler,
                _callback(1, data="cpa:invalid-token:first"),
                data,
            )
        )
        await asyncio.wait_for(first_entered.wait(), timeout=1)
        group.create_task(
            middleware(
                handler,
                _callback(2, data="cpa:invalid-token:second"),
                data,
            )
        )
        await asyncio.wait_for(second_acknowledged.wait(), timeout=0.1)
        assert "start:second" not in handler_order
        release_first.set()

    assert handler_order == [
        "start:first",
        "finish:first",
        "start:second",
        "finish:second",
    ]


@pytest.mark.asyncio
async def test_duplicate_tap_is_deduplicated_without_second_handler_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    middleware = safety.ClientPlatformInteractionSafetyMiddleware()
    handled = 0
    messages: list[str | None] = []

    async def answer_callback(
        _callback: CallbackQuery,
        text: str | None = None,
        **_kwargs: Any,
    ) -> None:
        messages.append(text)

    async def handler(_event: Any, _data: dict[str, Any]) -> None:
        nonlocal handled
        handled += 1

    monkeypatch.setattr(safety, "_answer_callback", answer_callback)
    data = {
        "bot": SimpleNamespace(id=8534548177),
        "state": FakeState(),
    }
    callback_data = "cpa:invalid-token:duplicate"
    await middleware(handler, _callback(1, data=callback_data), data)
    await middleware(handler, _callback(2, data=callback_data), data)

    assert handled == 1
    assert messages[-1] == "Действие уже выполняется."
