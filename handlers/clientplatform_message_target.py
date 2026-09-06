from __future__ import annotations

from typing import Any, Protocol


class ClientPlatformMessageTarget(Protocol):
    """Minimal canonical Telegram message surface used by reusable presenters."""

    async def answer(self, text: str, **kwargs: Any) -> Any:
        ...


__all__ = ["ClientPlatformMessageTarget"]
