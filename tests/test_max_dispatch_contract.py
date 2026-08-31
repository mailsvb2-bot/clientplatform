from __future__ import annotations

import asyncio
from typing import Any

import pytest

from services.messenger import reply_dispatcher
from services.messenger.reply_dispatcher import _canonical_payment_text
from services.messenger.text_ui import MessengerReply, PAYMENT_UNAVAILABLE_TEXT


def test_max_dispatch_keeps_stateful_gift_text() -> None:
    text = "\U0001f381 Surface\n\nRecipient: user\n\nready"
    assert _canonical_payment_text("max", 1001, "mx1001", text) == text


class _FakeSender:
    def __init__(self) -> None:
        self.text_calls: list[tuple[str, str, dict[str, Any]]] = []

    async def send_text(self, external_user_id: str, text: str, **kwargs: Any) -> dict[str, Any]:
        self.text_calls.append((str(external_user_id), str(text), dict(kwargs)))
        return {"ok": True}


@pytest.mark.parametrize("platform", ["vk", "max"])
def test_disabled_payment_reply_reaches_sender_without_checkout_rebuild(monkeypatch, platform: str) -> None:
    sender = _FakeSender()
    monkeypatch.setenv("PAYMENT_HTTP_ENABLED", "0")
    monkeypatch.delenv("PAYMENT_CHECKOUT_SIGNING_KEY", raising=False)
    monkeypatch.delenv("CHECKOUT_SIGNING_KEY", raising=False)
    monkeypatch.setattr(reply_dispatcher, "MaxBotSender", lambda: sender if platform == "max" else _FakeSender())
    monkeypatch.setattr(reply_dispatcher, "VkBotSender", lambda: sender if platform == "vk" else _FakeSender())
    monkeypatch.setattr(
        reply_dispatcher,
        "package_payment_text",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("checkout UI must not rebuild")),
    )

    asyncio.run(
        reply_dispatcher.send_reply_bundle(
            platform,
            f"{platform}-disabled-payment",
            1002,
            [MessengerReply(text=PAYMENT_UNAVAILABLE_TEXT)],
        )
    )

    assert sender.text_calls
    assert sender.text_calls[-1][1] == PAYMENT_UNAVAILABLE_TEXT
    assert not PAYMENT_UNAVAILABLE_TEXT.lstrip().startswith("💳")
