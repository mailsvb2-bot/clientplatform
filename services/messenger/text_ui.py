from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MessengerReply:
    """Channel-neutral reply emitted by the canonical ClientPlatform entry flow."""

    kind: str = "text"
    text: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def handle_incoming_text(*args: Any, **kwargs: Any):
    """Compatibility import path for the canonical ClientPlatform messenger entry."""

    from services.messenger.text_ui_router import handle_incoming_text as _handle

    return _handle(*args, **kwargs)


__all__ = ["MessengerReply", "handle_incoming_text"]
