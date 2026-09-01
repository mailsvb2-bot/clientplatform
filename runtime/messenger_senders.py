from __future__ import annotations

import hashlib
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from runtime.messenger_max_sender import MaxBotSender
from runtime.messenger_telegram_sender import TelegramBotSender
from runtime.messenger_transport_errors import MessengerMediaNotReadyError, MessengerTransportError
from runtime.messenger_vk_sender import VkBotSender as _VkBotSender, _callback_keyboard_json

_delivery_scope: ContextVar[tuple[str, int]] = ContextVar(
    "messenger_provider_delivery_scope",
    default=("", 0),
)


@contextmanager
def provider_delivery_scope(delivery_key: str) -> Iterator[None]:
    """Give provider sends a stable identity for one durable outbox attempt."""

    token = _delivery_scope.set((str(delivery_key or "").strip(), 0))
    try:
        yield
    finally:
        _delivery_scope.reset(token)


def _next_vk_random_id() -> int:
    delivery_key, ordinal = _delivery_scope.get()
    if not delivery_key:
        return int(time.time_ns() % 2147483647) or 1
    ordinal += 1
    _delivery_scope.set((delivery_key, ordinal))
    digest = hashlib.blake2s(
        f"{delivery_key}:{ordinal}".encode("utf-8"),
        digest_size=4,
    ).digest()
    value = int.from_bytes(digest, "big") & 0x7FFFFFFF
    return value or 1


class VkBotSender(_VkBotSender):
    """VK sender with deterministic provider idempotency for durable delivery."""

    async def send_text(self, external_user_id: str, text: str, **kwargs: Any):
        random_id = kwargs.get("random_id")
        if random_id is None:
            random_id = _next_vk_random_id()

        params: dict[str, Any] = {
            "user_id": str(external_user_id),
            "random_id": int(random_id),
            "message": str(text or ""),
        }
        if kwargs.get("keyboard_json"):
            params["keyboard"] = _callback_keyboard_json(str(kwargs["keyboard_json"]))
        if kwargs.get("attachment"):
            params["attachment"] = kwargs["attachment"]
        data = await self._vk_method("messages.send", params)
        return data.get("response", data)


__all__ = [
    "MessengerTransportError",
    "MessengerMediaNotReadyError",
    "TelegramBotSender",
    "MaxBotSender",
    "VkBotSender",
    "provider_delivery_scope",
]
