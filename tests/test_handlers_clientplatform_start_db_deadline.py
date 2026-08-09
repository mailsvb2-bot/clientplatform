from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from handlers import clientplatform_entry as entry
from services.db import core as db_core


class _Message:
    def __init__(self) -> None:
        self.answers: list[str] = []

    async def answer(self, text: str, **_kwargs: Any) -> None:
        self.answers.append(text)


class _State:
    pass


@pytest.mark.asyncio
async def test_start_db_deadline_reaches_worker_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = _Message()
    observed: list[float | None] = []
    monkeypatch.setattr(entry.control, "_start_payload", lambda _message: "")

    async def dispatch(_message: Any, _state: Any, *, payload: str) -> bool:
        assert payload == ""
        observed.append(
            await asyncio.to_thread(db_core._DB_OPERATION_DEADLINE.get)
        )
        return True

    monkeypatch.setattr(entry, "_dispatch_clientplatform_start", dispatch)

    await entry.clientplatform_start(message, _State())

    assert len(observed) == 1
    assert observed[0] is not None
    assert float(observed[0]) > time.monotonic()
    assert message.answers == []


@pytest.mark.asyncio
async def test_internal_db_deadline_uses_safe_start_timeout_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = _Message()
    monkeypatch.setattr(entry.control, "_start_payload", lambda _message: "")

    async def dispatch(_message: Any, _state: Any, *, payload: str) -> bool:
        del payload
        raise db_core.DatabaseOperationDeadlineExceeded(
            "database_operation_deadline_exceeded"
        )

    monkeypatch.setattr(entry, "_dispatch_clientplatform_start", dispatch)

    await entry.clientplatform_start(message, _State())

    assert len(message.answers) == 1
    assert "не успел открыть кабинет" in message.answers[0]
    assert "database_operation_deadline_exceeded" not in message.answers[0]


def test_start_storage_deadline_leaves_response_margin() -> None:
    assert 0 < entry._START_STORAGE_DEADLINE_SECONDS < entry._START_TIMEOUT_SECONDS
    assert (
        entry._START_TIMEOUT_SECONDS - entry._START_STORAGE_DEADLINE_SECONDS
        >= 2.0
    )
