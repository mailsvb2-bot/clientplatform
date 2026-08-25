from __future__ import annotations

import re
from dataclasses import dataclass

from clientplatform.application.tenancy import resolve_tenant_context
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.tenancy import TenantContext
from services.db import get_db_ro


_COMMAND_RE = re.compile(
    r"cpm:switch:(telegram|vk|max):([1-9][0-9]{0,18})"
)
_TELEGRAM_USERNAME_RE = re.compile(r"[A-Za-z0-9_]{5,32}")
_MAX_USERNAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,80}")


class MessengerSwitchRejected(RuntimeError):
    """A staff messenger switch target is unavailable or ambiguous."""


@dataclass(frozen=True, slots=True)
class StaffMessengerSwitchDestination:
    user_id: int
    business_id: str
    platform: ConnectionPlatform
    public_target: str


def _managed_bot_target(
    *, business_id: str, platform: ConnectionPlatform
) -> str | None:
    with get_db_ro() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT mb.username
            FROM managed_bots mb
            JOIN connections c
              ON c.id=mb.connection_id AND c.business_id=mb.business_id
             AND c.platform=mb.platform
            WHERE mb.business_id=? AND mb.platform=?
              AND mb.status='active' AND c.status='active'
              AND mb.username IS NOT NULL AND mb.username!=''
            """,
            (business_id, platform.value),
        ).fetchall()
    values = [
        str(row["username"] if hasattr(row, "keys") else row[0]).strip()
        for row in rows
    ]
    values = [value for value in values if value]
    if len(values) != 1:
        return None
    value = values[0]
    pattern = (
        _TELEGRAM_USERNAME_RE
        if platform == ConnectionPlatform.TELEGRAM
        else _MAX_USERNAME_RE
    )
    return value if pattern.fullmatch(value) else None


def _vk_target(*, business_id: str) -> str | None:
    with get_db_ro() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT external_account_id
            FROM connections
            WHERE business_id=? AND platform='vk' AND status='active'
              AND connection_type='vk_community'
            """,
            (business_id,),
        ).fetchall()
    values = [
        str(row["external_account_id"] if hasattr(row, "keys") else row[0]).strip()
        for row in rows
    ]
    values = [value for value in values if value.isdigit() and int(value) > 0]
    return values[0] if len(values) == 1 else None


def _public_target(*, business_id: str, platform: ConnectionPlatform) -> str | None:
    if platform == ConnectionPlatform.VK:
        return _vk_target(business_id=business_id)
    return _managed_bot_target(business_id=business_id, platform=platform)


def available_staff_messenger_switches(
    actor: TenantContext,
) -> tuple[ConnectionPlatform, ...]:
    current = resolve_tenant_context(
        user_id=actor.user_id,
        business_id=actor.business_id,
    )
    available: list[ConnectionPlatform] = []
    for platform in (
        ConnectionPlatform.TELEGRAM,
        ConnectionPlatform.VK,
        ConnectionPlatform.MAX,
    ):
        if _public_target(business_id=current.business_id, platform=platform):
            available.append(platform)
    return tuple(available)


def build_staff_switch_command(
    actor: TenantContext,
    platform: ConnectionPlatform | str,
) -> str:
    current = resolve_tenant_context(
        user_id=actor.user_id,
        business_id=actor.business_id,
    )
    selected = (
        platform
        if isinstance(platform, ConnectionPlatform)
        else ConnectionPlatform(str(platform or "").strip().lower())
    )
    return f"cpm:switch:{selected.value}:{current.user_id}"


def parse_staff_switch_command(command: str) -> tuple[ConnectionPlatform, int] | None:
    match = _COMMAND_RE.fullmatch(str(command or "").strip())
    if match is None:
        return None
    return ConnectionPlatform(match.group(1)), int(match.group(2))


def resolve_staff_switch_destination(
    *, command: str, business_id: str
) -> StaffMessengerSwitchDestination | None:
    parsed = parse_staff_switch_command(command)
    if parsed is None:
        return None
    platform, user_id = parsed
    actor = resolve_tenant_context(user_id=user_id, business_id=business_id)
    target = _public_target(business_id=actor.business_id, platform=platform)
    if target is None:
        raise MessengerSwitchRejected(
            "messenger switch target is unavailable or ambiguous"
        )
    return StaffMessengerSwitchDestination(
        user_id=actor.user_id,
        business_id=actor.business_id,
        platform=platform,
        public_target=target,
    )


__all__ = [
    "MessengerSwitchRejected",
    "StaffMessengerSwitchDestination",
    "available_staff_messenger_switches",
    "build_staff_switch_command",
    "parse_staff_switch_command",
    "resolve_staff_switch_destination",
]
