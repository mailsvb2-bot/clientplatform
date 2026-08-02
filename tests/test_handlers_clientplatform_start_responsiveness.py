from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from handlers import clientplatform_entry as entry


class FakeStatusMessage:
    def __init__(self) -> None:
        self.edits: list[str] = []
        self.deleted = False

    async def edit_text(self, text: str) -> None:
        self.edits.append(text)

    async def delete(self) -> None:
        self.deleted = True


class FakeMessage:
    def __init__(self, user_id: int = 101) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.text = "/start"
        self.answers: list[str] = []
        self.status = FakeStatusMessage()

    async def answer(self, text: str, **_kwargs: Any) -> FakeStatusMessage:
        self.answers.append(text)
        return self.status


class FakeState:
    pass


@pytest.mark.asyncio
async def test_start_acknowledges_before_any_storage_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = FakeMessage()
    observed: list[str] = []

    async def fake_dispatch(
        received_message: FakeMessage,
        _state: FakeState,
        *,
        user_id: int,
        managed_bot_business_id: str | None,
    ) -> None:
        assert received_message is message
        assert user_id == 101
        assert managed_bot_business_id is None
        observed.extend(received_message.answers)

    monkeypatch.setattr(entry, "_dispatch_clientplatform_start", fake_dispatch)

    await entry.clientplatform_entry_start(message, FakeState())

    assert observed == ["Открываю…"]
    assert message.status.deleted is True
    assert message.status.edits == []


@pytest.mark.asyncio
async def test_start_timeout_is_visible_instead_of_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = FakeMessage()

    async def stalled_dispatch(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.sleep(1)

    monkeypatch.setattr(entry, "_dispatch_clientplatform_start", stalled_dispatch)
    monkeypatch.setattr(entry, "_START_TIMEOUT_SECONDS", 0.01)

    await entry.clientplatform_entry_start(message, FakeState())

    assert message.answers == ["Открываю…"]
    assert message.status.deleted is False
    assert "дольше обычного" in message.status.edits[-1]


@pytest.mark.asyncio
async def test_start_failure_is_visible_and_does_not_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = FakeMessage()

    async def failed_dispatch(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(entry, "_dispatch_clientplatform_start", failed_dispatch)

    await entry.clientplatform_entry_start(message, FakeState())

    assert message.status.deleted is False
    assert "Не удалось открыть ClientPlatform" in message.status.edits[-1]
    assert "database unavailable" not in message.status.edits[-1]


@pytest.mark.asyncio
async def test_start_timeout_cancels_the_waiting_coroutine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = FakeMessage()
    cancelled = asyncio.Event()

    async def stalled_dispatch(*_args: Any, **_kwargs: Any) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(entry, "_dispatch_clientplatform_start", stalled_dispatch)
    monkeypatch.setattr(entry, "_START_TIMEOUT_SECONDS", 0.01)

    await entry.clientplatform_entry_start(message, FakeState())

    assert cancelled.is_set()
