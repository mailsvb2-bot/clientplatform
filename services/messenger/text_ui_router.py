from __future__ import annotations

from services.messenger.clientplatform_entry import (
    handle_clientplatform_entry,
    parse_clientplatform_entry_command,
)
from services.messenger.text_ui import MessengerReply


def _official_entry_text(text: str | None, *, event_type: str | None = None) -> str:
    raw = str(text or "").strip()
    if parse_clientplatform_entry_command(raw, event_type=event_type) is not None:
        return raw
    return "start"


def handle_incoming_text(
    user_id: int,
    *,
    platform: str,
    external_user_id: str | None = None,
    text: str | None = None,
    username: str | None = None,
    display_name: str | None = None,
    first_name: str | None = None,
    event_type: str | None = None,
    event_key: str | None = None,
) -> tuple[int, list[MessengerReply]]:
    """Route the compatibility text entry to the single ClientPlatform owner surface.

    Official ClientPlatform VK/MAX/Telegram control channels are owner/member
    entry points. Unknown free-form text therefore opens the canonical entry
    instead of falling through into an unrelated product journey.
    """

    return handle_clientplatform_entry(
        int(user_id),
        platform=platform,
        external_user_id=external_user_id,
        text=_official_entry_text(text, event_type=event_type),
        event_type=event_type,
        username=username,
        display_name=display_name,
        first_name=first_name,
        event_key=event_key,
    )


__all__ = ["MessengerReply", "handle_incoming_text"]
