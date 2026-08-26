from __future__ import annotations

from urllib.parse import quote

from clientplatform.application.messenger_switching import (
    resolve_staff_switch_destination,
)
from clientplatform.domain.connections import ConnectionPlatform
from services.messenger.bridge import issue_bridge_token
from services.messenger.links import build_bridge_payload


class StaffMessengerSwitchLinkService:
    """Materialize one-time tenant-specific staff switch links just before send."""

    def resolve_command_url(self, *, command: str, business_id: str) -> str | None:
        destination = resolve_staff_switch_destination(
            command=command,
            business_id=business_id,
        )
        if destination is None:
            return None
        token = issue_bridge_token(
            destination.user_id,
            target_platform=destination.platform.value,
        )
        payload = quote(build_bridge_payload(token), safe="")
        target = quote(destination.public_target, safe="")
        if destination.platform == ConnectionPlatform.TELEGRAM:
            return f"https://t.me/{target}?start={payload}"
        if destination.platform == ConnectionPlatform.VK:
            return f"https://vk.com/im?sel=-{destination.public_target}&start={payload}"
        return f"https://max.ru/{target}?start={payload}"


__all__ = ["StaffMessengerSwitchLinkService"]
