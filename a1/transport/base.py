from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from a1.domain.connections import (
    ClaimedDispatch,
    ConnectionPlatform,
    DispatchInvariantViolation,
    normalize_connection_platform,
)


class CredentialProvider(Protocol):
    """Resolve a secret-manager reference at send time.

    Implementations must never persist or log the returned secret.
    """

    def resolve(self, reference: str) -> str:
        """Return the raw provider credential for one protected reference."""


class DispatchAdapter(Protocol):
    """Transport-specific sender used by the generic dispatch worker."""

    platform: ConnectionPlatform

    async def send(self, item: ClaimedDispatch, credential: str) -> str:
        """Send one dispatch and return the provider message identifier."""


class AdapterRegistry:
    """Immutable lookup of one adapter per supported platform."""

    def __init__(self, adapters: Iterable[DispatchAdapter]):
        mapped: dict[ConnectionPlatform, DispatchAdapter] = {}
        for adapter in adapters:
            platform = normalize_connection_platform(adapter.platform)
            if platform in mapped:
                raise DispatchInvariantViolation(
                    f"duplicate dispatch adapter for platform {platform.value}"
                )
            mapped[platform] = adapter
        self._adapters = mapped

    def get(
        self,
        platform: ConnectionPlatform | str,
    ) -> DispatchAdapter:
        normalized = normalize_connection_platform(platform)
        adapter = self._adapters.get(normalized)
        if adapter is None:
            raise DispatchInvariantViolation(
                f"dispatch adapter is not configured for {normalized.value}"
            )
        return adapter

    @property
    def platforms(self) -> tuple[ConnectionPlatform, ...]:
        return tuple(sorted(self._adapters, key=lambda item: item.value))
