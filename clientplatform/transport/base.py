from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from clientplatform.domain.connections import (
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


@runtime_checkable
class TwoPhaseDispatchAdapter(DispatchAdapter, Protocol):
    """Adapter whose provider preparation is replay-safe before final write.

    ``prepare`` may resolve/download media and perform provider-side upload work,
    but it must not create the user-visible message. The worker can therefore do
    all replay-safe preparation first, durably mark a non-idempotent boundary,
    and call ``send_prepared`` only for the final provider message write.

    Raw provider credentials are deliberately not part of prepared state. The
    worker retains the resolved credential and passes it only to the final-write
    call, preventing an avoidable secret copy in transient prepared objects.

    ``release_prepared`` is best-effort transient cleanup. It must never create
    provider-visible work or change durable dispatch state.
    """

    async def prepare(self, item: ClaimedDispatch, credential: str) -> object:
        """Prepare transient provider state without creating the message."""

    async def send_prepared(self, prepared: object, credential: str) -> str:
        """Cross only the final provider message-write boundary."""

    async def release_prepared(self, prepared: object) -> None:
        """Release transient preparation state after send, failure or cancel."""


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
