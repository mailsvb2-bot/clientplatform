from __future__ import annotations

from dataclasses import asdict, dataclass

from clientplatform.application.capability_parity import (
    CapabilityAvailability,
    get_business_capability_projection,
)
from clientplatform.application.cockpit import resolve_cockpit_context
from clientplatform.application.native_messenger_setup import issue_native_messenger_setup
from clientplatform.application.tenancy import resolve_tenant_context
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.tenancy import TenantAccessDenied, TenantContext
from clientplatform.infrastructure.native_messenger_setup_repository import IssuedNativeMessengerSetup

_SCHEMA_VERSION = "2026-09-06.v1"
_PLATFORM_LABELS = {
    ConnectionPlatform.TELEGRAM: "Telegram",
    ConnectionPlatform.VK: "ВКонтакте",
    ConnectionPlatform.MAX: "MAX",
}
_STATE_LABELS = {
    CapabilityAvailability.ACTIVE: "Работает",
    CapabilityAvailability.ATTENTION: "Требует внимания",
    CapabilityAvailability.CONFIGURING: "Настраивается",
    CapabilityAvailability.CONNECTABLE: "Можно подключить",
    CapabilityAvailability.CONNECTED_UNAVAILABLE: "Подключён, но сейчас выключен",
    CapabilityAvailability.UNAVAILABLE: "Сейчас недоступен",
}


@dataclass(frozen=True, slots=True)
class CockpitConnectionItem:
    platform: str
    title: str
    availability: str
    state_label: str
    active: bool
    can_connect: bool
    runtime_ready: bool


@dataclass(frozen=True, slots=True)
class CockpitConnectionsSnapshot:
    schema_version: str
    business_id: str
    business_name: str
    items: tuple[CockpitConnectionItem, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def build_cockpit_connections(*, actor: TenantContext, business_name: str) -> CockpitConnectionsSnapshot:
    current = resolve_tenant_context(user_id=actor.user_id, business_id=actor.business_id)
    current.assert_can_manage_business()
    projection = get_business_capability_projection(actor=current, include_advertising=False)
    items = tuple(
        CockpitConnectionItem(
            platform=item.platform.value,
            title=_PLATFORM_LABELS[item.platform],
            availability=item.availability.value,
            state_label=_STATE_LABELS[item.availability],
            active=item.active,
            can_connect=item.can_connect,
            runtime_ready=item.runtime_ready,
        )
        for item in projection.messengers
    )
    return CockpitConnectionsSnapshot(
        schema_version=_SCHEMA_VERSION,
        business_id=current.business_id,
        business_name=str(business_name),
        items=items,
    )


def _resolve_actor(*, telegram_user_id: int, requested_business_id: str | None) -> tuple[TenantContext, str]:
    context = resolve_cockpit_context(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    if context.onboarding_required or context.business_id is None or context.business_name is None:
        raise TenantAccessDenied("active business membership was not found")
    actor = resolve_tenant_context(user_id=context.user_id, business_id=context.business_id)
    return actor, context.business_name


def resolve_cockpit_connections(
    *, telegram_user_id: int, requested_business_id: str | None = None
) -> CockpitConnectionsSnapshot:
    actor, business_name = _resolve_actor(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    return build_cockpit_connections(actor=actor, business_name=business_name)


def issue_cockpit_messenger_setup(
    *, telegram_user_id: int, requested_business_id: str | None, platform: str
) -> IssuedNativeMessengerSetup:
    actor, _business_name = _resolve_actor(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    actor.assert_can_manage_business()
    selected = ConnectionPlatform(str(platform or "").strip().lower())
    if selected not in {ConnectionPlatform.TELEGRAM, ConnectionPlatform.VK, ConnectionPlatform.MAX}:
        raise ValueError("unsupported messenger platform")
    projection = get_business_capability_projection(actor=actor, include_advertising=False)
    if not projection.messenger(selected).can_connect:
        raise RuntimeError("messenger connection is not currently connectable")
    return issue_native_messenger_setup(actor=actor, platform=selected, ttl_seconds=600)


__all__ = [
    "CockpitConnectionItem",
    "CockpitConnectionsSnapshot",
    "build_cockpit_connections",
    "issue_cockpit_messenger_setup",
    "resolve_cockpit_connections",
]
