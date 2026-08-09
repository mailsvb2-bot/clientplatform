from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from handlers import clientplatform_entry as entry
from services.db import core as db_core


class _StatusMessage:
    def __init__(self) -> None:
        self.edits: list[str] = []
        self.deleted = False

    async def edit_text(self, text: str) -> None:
        self.edits.append(text)

    async def delete(self) -> None:
        self.deleted = True


class _Message:
    def __init__(self) -> None:
        self.answers: list[str] = []
        self.status = _StatusMessage()

    async def answer(self, text: str, **_kwargs: Any) -> _StatusMessage | None:
        self.answers.append(text)
        if text == "Открываю…":
            return self.status
        return None


class _State:
    pass


@pytest.mark.asyncio
async def test_start_db_deadline_reaches_worker_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = _Message()
    observed: list[float | None] = []
    monkeypatch.setattr(entry.control, "_user_id", lambda _message: 42)

    async def dispatch(
        _message: Any,
        _state: Any,
        *,
        user_id: int,
        managed_bot_business_id: str | None,
    ) -> None:
        assert user_id == 42
        assert managed_bot_business_id is None
        observed.append(await asyncio.to_thread(db_core._DB_OPERATION_DEADLINE.get))

    monkeypatch.setattr(entry, "_dispatch_clientplatform_start", dispatch)

    await entry.clientplatform_entry_start(message, _State())

    assert len(observed) == 1
    assert observed[0] is not None
    assert float(observed[0]) > time.monotonic()
    assert message.answers == ["Открываю…"]
    assert message.status.edits == []
    assert message.status.deleted is True


@pytest.mark.asyncio
async def test_internal_db_deadline_uses_safe_start_timeout_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = _Message()
    monkeypatch.setattr(entry.control, "_user_id", lambda _message: 42)

    async def dispatch(
        _message: Any,
        _state: Any,
        *,
        user_id: int,
        managed_bot_business_id: str | None,
    ) -> None:
        del user_id, managed_bot_business_id
        raise db_core.DatabaseOperationDeadlineExceeded(
            "database_operation_deadline_exceeded"
        )

    monkeypatch.setattr(entry, "_dispatch_clientplatform_start", dispatch)

    await entry.clientplatform_entry_start(message, _State())

    assert message.answers == ["Открываю…"]
    assert len(message.status.edits) == 1
    assert "отвечает дольше обычного" in message.status.edits[0]
    assert "database_operation_deadline_exceeded" not in message.status.edits[0]
    assert message.status.deleted is False


def test_start_storage_deadline_leaves_response_margin() -> None:
    assert 0 < entry._START_STORAGE_DEADLINE_SECONDS < entry._START_TIMEOUT_SECONDS
    assert (
        entry._START_TIMEOUT_SECONDS - entry._START_STORAGE_DEADLINE_SECONDS
        >= 2.0
    )
