from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.dispatcher.event.bases import SkipHandler

from services.payments import gift


class _Message:
    def __init__(self, user_id: int = 42) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[tuple[str, object]] = []

    async def answer(self, text: str, reply_markup=None) -> None:
        self.answers.append((text, reply_markup))


@pytest.mark.asyncio
async def test_cancel_delegates_when_gift_flow_is_not_active(monkeypatch) -> None:
    monkeypatch.setattr(gift, "peek_pending", lambda _uid: SimpleNamespace(kind="share"))
    with pytest.raises(SkipHandler):
        await gift.gift_pick_cancel(_Message())


@pytest.mark.asyncio
async def test_cancel_clears_only_active_gift_flow(monkeypatch) -> None:
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(gift, "peek_pending", lambda _uid: SimpleNamespace(kind="gift_target"))
    monkeypatch.setattr(gift, "pop_pending", lambda uid: calls.append(("pending", uid)))
    monkeypatch.setattr(gift, "clear_target", lambda uid: calls.append(("target", uid)))
    monkeypatch.setattr(gift, "kb_main", lambda *, user_id: ("main", user_id))

    message = _Message()
    await gift.gift_pick_cancel(message)

    assert calls == [("pending", 42), ("target", 42)]
    assert len(message.answers) == 2
