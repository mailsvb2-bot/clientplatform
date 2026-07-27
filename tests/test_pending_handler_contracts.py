from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.dispatcher.event.bases import SkipHandler

from handlers.flow import settings_core
from handlers import weather as weather_handler
from services.payments.gift import gift_pick_cancel
from services.pending import clear_pending, peek_pending, set_pending


class _Message:
    def __init__(self, user_id: int, text: str = "", *, location=None) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.text = text
        self.location = location
        self.answers: list[str] = []

    async def answer(self, text: str, **_kwargs):
        self.answers.append(text)
        return None


@pytest.mark.asyncio
async def test_gift_cancel_skips_when_another_flow_owns_the_message() -> None:
    user_id = 94001
    clear_pending(user_id)
    set_pending(user_id, "share", {})

    with pytest.raises(SkipHandler):
        await gift_pick_cancel(_Message(user_id, "❌ Отмена"))

    assert peek_pending(user_id) is not None
    assert peek_pending(user_id).kind == "share"


@pytest.mark.asyncio
async def test_invalid_settings_time_keeps_pending_state() -> None:
    user_id = 94002
    clear_pending(user_id)
    set_pending(user_id, "set_time", {"slot": "work"})

    message = _Message(user_id, "not-a-time")
    await settings_core.settings_time_input(message)

    assert peek_pending(user_id) is not None
    assert peek_pending(user_id).kind == "set_time"
    assert any("HH:MM" in answer for answer in message.answers)


@pytest.mark.asyncio
async def test_failed_city_resolution_keeps_pending_state(monkeypatch) -> None:
    user_id = 94003
    clear_pending(user_id)
    set_pending(user_id, "weather_city", {})

    monkeypatch.setattr(weather_handler, "set_city", lambda *_args: (False, "Город не найден"))
    message = _Message(user_id, "Missing City")
    await weather_handler.weather_city_input(message)

    assert peek_pending(user_id) is not None
    assert peek_pending(user_id).kind == "weather_city"
    assert any("Город не найден" in answer for answer in message.answers)


@pytest.mark.asyncio
async def test_unscoped_location_is_not_consumed_by_weather() -> None:
    user_id = 94004
    clear_pending(user_id)
    message = _Message(
        user_id,
        location=SimpleNamespace(latitude=52.3676, longitude=4.9041),
    )

    with pytest.raises(SkipHandler):
        await weather_handler.weather_location(message)
