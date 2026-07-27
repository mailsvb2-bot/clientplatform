from __future__ import annotations

from types import SimpleNamespace

import pytest

from handlers.flow import settings_core
from services.pending import clear_pending, peek_pending, set_pending


class _Message:
    def __init__(self, user_id: int, text: str) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.text = text
        self.answers: list[str] = []

    async def answer(self, text: str, **_kwargs) -> None:
        self.answers.append(text)


@pytest.mark.asyncio
async def test_invalid_time_keeps_settings_state_active() -> None:
    user_id = 95001
    clear_pending(user_id)
    set_pending(user_id, "set_time", {"slot": "work"})

    message = _Message(user_id, "not-a-time")
    await settings_core.settings_time_input(message)

    pending = peek_pending(user_id)
    assert pending is not None
    assert pending.kind == "set_time"
    assert any("HH:MM" in answer for answer in message.answers)


@pytest.mark.asyncio
async def test_invalid_quiet_hours_keeps_settings_state_active() -> None:
    user_id = 95002
    clear_pending(user_id)
    set_pending(user_id, "set_quiet_hours", {})

    message = _Message(user_id, "wrong")
    await settings_core.settings_time_input(message)

    pending = peek_pending(user_id)
    assert pending is not None
    assert pending.kind == "set_quiet_hours"
    assert any("HH:MM-HH:MM" in answer for answer in message.answers)
