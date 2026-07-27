from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.dispatcher.event.bases import SkipHandler

from services.payments.gift import gift_pick_cancel
from services.pending import clear_pending, peek_pending, set_pending


class _Message:
    def __init__(self, user_id: int) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[str] = []

    async def answer(self, text: str, **_kwargs) -> None:
        self.answers.append(text)


@pytest.mark.asyncio
async def test_gift_cancel_cannot_consume_share_state() -> None:
    user_id = 94001
    clear_pending(user_id)
    set_pending(user_id, "share", {})

    with pytest.raises(SkipHandler):
        await gift_pick_cancel(_Message(user_id))

    pending = peek_pending(user_id)
    assert pending is not None
    assert pending.kind == "share"


@pytest.mark.asyncio
async def test_gift_cancel_consumes_only_active_gift_state() -> None:
    user_id = 94002
    clear_pending(user_id)
    set_pending(user_id, "gift_target", {})

    message = _Message(user_id)
    await gift_pick_cancel(message)

    assert peek_pending(user_id) is None
    assert any("отменён" in answer for answer in message.answers)
