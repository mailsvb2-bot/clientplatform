from __future__ import annotations

from datetime import datetime
from typing import Any

from clientplatform.infrastructure.event_dispatch_safety import (
    event_commercial_claim_can_cross_provider_boundary,
    is_commercial_event_dispatch,
    mark_event_commercial_non_replay_boundary,
    quarantine_stale_event_commercial_boundaries,
)
from clientplatform.infrastructure.safe_member_dispatch_outbox import (
    DispatchOutboxRepository as _SafeMemberDispatchOutboxRepository,
)
from clientplatform.infrastructure.unified_dispatch_outbox import ClaimedProviderDispatch


class DispatchOutboxRepository(_SafeMemberDispatchOutboxRepository):
    """Add consent-aware commercial event safety to the canonical provider outbox."""

    def event_message_claim_can_cross_provider_boundary(
        self,
        item: ClaimedProviderDispatch,
        *,
        now: str | None = None,
    ) -> bool:
        if is_commercial_event_dispatch(item):
            return event_commercial_claim_can_cross_provider_boundary(
                self._conn,
                item,
                now=now,
            )
        return super().event_message_claim_can_cross_provider_boundary(item, now=now)

    def mark_provider_non_replay_boundary(
        self,
        item: ClaimedProviderDispatch,
        *,
        now: str | None = None,
    ) -> bool:
        if is_commercial_event_dispatch(item):
            return mark_event_commercial_non_replay_boundary(
                self._conn,
                item,
                now=now,
            )
        return super().mark_provider_non_replay_boundary(item, now=now)

    def claim_due(
        self,
        *,
        limit: int = 10,
        lock_ttl_seconds: int = 900,
        now: datetime | None = None,
    ) -> list[Any]:
        quarantine_stale_event_commercial_boundaries(
            self._conn,
            lock_ttl_seconds=lock_ttl_seconds,
            now=now,
        )
        return super().claim_due(
            limit=limit,
            lock_ttl_seconds=lock_ttl_seconds,
            now=now,
        )


__all__ = ["DispatchOutboxRepository"]
