from __future__ import annotations

"""User-facing capability projection over canonical runtime and tenant facts."""

import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from clientplatform.application.ad_connections import (
    ad_connections_enabled,
    list_ad_connections,
    yandex_direct_provider_configured,
)
from clientplatform.application.control import business_connection_statuses
from clientplatform.domain.ad_connections import AdConnectionStatus
from clientplatform.domain.connections import ConnectionPlatform, ConnectionStatus
from clientplatform.domain.tenancy import TenantContext, TenantPermissionDenied
from config.settings import settings
from runtime.ingress_flags import max_webhook_enabled, vk_webhook_enabled
from runtime.telegram_transport import telegram_runtime_enabled
from services.messenger.setup import build_setup_status

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


class CapabilityAvailability(StrEnum):
    ACTIVE = "active"
    ATTENTION = "attention"
    CONFIGURING = "configuring"
    CONNECTABLE = "connectable"
    CONNECTED_UNAVAILABLE = "connected_unavailable"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class MessengerCapabilityProjection:
    platform: ConnectionPlatform
    availability: CapabilityAvailability
    connection_statuses: tuple[ConnectionStatus, ...]
    runtime_enabled: bool
    runtime_ready: bool
    setup_available: bool

    @property
    def active(self) -> bool:
        return ConnectionStatus.ACTIVE in self.connection_statuses

    @property
    def can_connect(self) -> bool:
        return (
            self.availability == CapabilityAvailability.CONNECTABLE
            and self.setup_available
        )


@dataclass(frozen=True, slots=True)
class AdvertisingCapabilityProjection:
    availability: CapabilityAvailability
    connection_statuses: tuple[AdConnectionStatus, ...]
    runtime_enabled: bool


@dataclass(frozen=True, slots=True)
class BusinessCapabilityProjection:
    messengers: tuple[MessengerCapabilityProjection, ...]
    yandex_direct: AdvertisingCapabilityProjection | None

    def messenger(self, platform: ConnectionPlatform | str) -> MessengerCapabilityProjection:
        normalized = (
            platform
            if isinstance(platform, ConnectionPlatform)
            else ConnectionPlatform(str(platform or "").strip().lower())
        )
        return next(item for item in self.messengers if item.platform == normalized)


def _omnichannel_setup_available() -> bool:
    enabled = (
        os.getenv("CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED") or ""
    ).strip().lower() in _TRUE_VALUES
    public_base = str(getattr(settings, "MESSENGER_PUBLIC_BASE_URL", "") or "").strip()
    return bool(enabled and public_base.startswith("https://"))


def _availability(
    *,
    statuses: tuple[ConnectionStatus, ...],
    runtime_enabled: bool,
    runtime_ready: bool,
    setup_available: bool,
) -> CapabilityAvailability:
    if ConnectionStatus.ACTIVE in statuses:
        if not runtime_enabled:
            return CapabilityAvailability.CONNECTED_UNAVAILABLE
        if not runtime_ready:
            return CapabilityAvailability.ATTENTION
        return CapabilityAvailability.ACTIVE
    if ConnectionStatus.ATTENTION in statuses:
        return CapabilityAvailability.ATTENTION
    if ConnectionStatus.PENDING in statuses:
        if runtime_enabled and runtime_ready:
            return CapabilityAvailability.CONFIGURING
        return CapabilityAvailability.CONNECTED_UNAVAILABLE
    if runtime_enabled and runtime_ready and setup_available:
        return CapabilityAvailability.CONNECTABLE
    return CapabilityAvailability.UNAVAILABLE


def project_messenger_capabilities(
    connections: Iterable[object],
    *,
    setup_available: bool | None = None,
    runtime_enabled: dict[ConnectionPlatform, bool] | None = None,
    runtime_ready: dict[ConnectionPlatform, bool] | None = None,
) -> tuple[MessengerCapabilityProjection, ...]:
    by_platform: dict[ConnectionPlatform, list[ConnectionStatus]] = {
        ConnectionPlatform.TELEGRAM: [],
        ConnectionPlatform.VK: [],
        ConnectionPlatform.MAX: [],
    }
    for connection in connections:
        if isinstance(connection, tuple) and len(connection) == 2:
            platform_value, status_value = connection
        else:
            platform_value = getattr(connection, "platform")
            status_value = getattr(connection, "status")
        platform_raw = getattr(platform_value, "value", platform_value)
        platform = (
            platform_value
            if isinstance(platform_value, ConnectionPlatform)
            else ConnectionPlatform(str(platform_raw).strip().lower())
        )
        if platform not in by_platform:
            continue
        status_raw = getattr(status_value, "value", status_value)
        status = (
            status_value
            if isinstance(status_value, ConnectionStatus)
            else ConnectionStatus(str(status_raw).strip().lower())
        )
        by_platform[platform].append(status)

    resolved_setup = _omnichannel_setup_available() if setup_available is None else bool(setup_available)
    setup_status = build_setup_status() if runtime_ready is None else None
    enabled = (
        runtime_enabled
        if runtime_enabled is not None
        else {
            ConnectionPlatform.TELEGRAM: telegram_runtime_enabled(),
            ConnectionPlatform.VK: vk_webhook_enabled(),
            ConnectionPlatform.MAX: max_webhook_enabled(),
        }
    )
    ready = (
        runtime_ready
        if runtime_ready is not None
        else {
            ConnectionPlatform.TELEGRAM: bool(setup_status and setup_status.telegram_ok),
            ConnectionPlatform.VK: bool(setup_status and setup_status.vk_ok),
            ConnectionPlatform.MAX: bool(setup_status and setup_status.max_ok),
        }
    )

    result: list[MessengerCapabilityProjection] = []
    for platform in (
        ConnectionPlatform.TELEGRAM,
        ConnectionPlatform.VK,
        ConnectionPlatform.MAX,
    ):
        statuses = tuple(by_platform[platform])
        platform_enabled = bool(enabled.get(platform, False))
        platform_ready = bool(ready.get(platform, False))
        result.append(
            MessengerCapabilityProjection(
                platform=platform,
                availability=_availability(
                    statuses=statuses,
                    runtime_enabled=platform_enabled,
                    runtime_ready=platform_ready,
                    setup_available=resolved_setup,
                ),
                connection_statuses=statuses,
                runtime_enabled=platform_enabled,
                runtime_ready=platform_ready,
                setup_available=resolved_setup,
            )
        )
    return tuple(result)


def _project_yandex(actor: TenantContext) -> AdvertisingCapabilityProjection | None:
    runtime_enabled = bool(ad_connections_enabled() and yandex_direct_provider_configured())
    try:
        connections = list_ad_connections(actor=actor)
    except TenantPermissionDenied:
        return None
    except (RuntimeError, ValueError):
        return AdvertisingCapabilityProjection(
            availability=CapabilityAvailability.UNAVAILABLE,
            connection_statuses=(),
            runtime_enabled=False,
        )
    statuses = tuple(item.status for item in connections)
    if AdConnectionStatus.ACTIVE in statuses:
        availability = (
            CapabilityAvailability.ACTIVE
            if runtime_enabled
            else CapabilityAvailability.CONNECTED_UNAVAILABLE
        )
    elif AdConnectionStatus.ATTENTION in statuses:
        availability = CapabilityAvailability.ATTENTION
    elif AdConnectionStatus.PENDING in statuses:
        availability = CapabilityAvailability.CONFIGURING
    elif runtime_enabled:
        availability = CapabilityAvailability.CONNECTABLE
    else:
        availability = CapabilityAvailability.UNAVAILABLE
    return AdvertisingCapabilityProjection(
        availability=availability,
        connection_statuses=statuses,
        runtime_enabled=runtime_enabled,
    )


def get_business_capability_projection(
    *, actor: TenantContext,
    include_advertising: bool = True,
) -> BusinessCapabilityProjection:
    connection_statuses = business_connection_statuses(actor=actor)
    return BusinessCapabilityProjection(
        messengers=project_messenger_capabilities(connection_statuses),
        yandex_direct=_project_yandex(actor) if include_advertising else None,
    )


__all__ = [
    "AdvertisingCapabilityProjection",
    "BusinessCapabilityProjection",
    "CapabilityAvailability",
    "MessengerCapabilityProjection",
    "get_business_capability_projection",
    "project_messenger_capabilities",
]
