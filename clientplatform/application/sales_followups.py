from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from clientplatform.domain.sales_followup import SalesFollowup
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure import DispatchOutboxRepository
from clientplatform.infrastructure.sales_followup_repository import SalesFollowupRepository
from services.db import get_db, get_db_ro


@dataclass(frozen=True, slots=True)
class SalesFollowupMaintenanceResult:
    owner_reminders_created: int
    stopped_followups: int


def schedule_sales_followup(
    *,
    actor: TenantContext,
    lead_id: str,
    message_text: str,
    scheduled_at: datetime | str,
    request_key: str,
    now: datetime | str | None = None,
) -> SalesFollowup:
    """Persist one owner-approved follow-up in the canonical dispatch outbox."""

    with get_db() as conn:
        repository = SalesFollowupRepository(conn)
        followup = repository.schedule(
            actor=actor,
            lead_id=lead_id,
            message_text=message_text,
            scheduled_at=scheduled_at,
            request_key=request_key,
            now=now,
        )
        DispatchOutboxRepository(conn).materialize_sales_followup(
            actor=actor,
            followup_id=followup.id,
            now=None if now is None else str(now),
        )
        return repository.get(actor=actor, followup_id=followup.id)


def get_active_sales_followup(
    *,
    actor: TenantContext,
    lead_id: str,
) -> SalesFollowup | None:
    with get_db_ro() as conn:
        return SalesFollowupRepository(conn).active_for_lead(actor=actor, lead_id=lead_id)


def cancel_sales_followup(
    *,
    actor: TenantContext,
    lead_id: str,
) -> int:
    with get_db() as conn:
        return SalesFollowupRepository(conn).cancel_active(actor=actor, lead_id=lead_id)


def suppress_sales_followup_channel(
    *,
    actor: TenantContext,
    lead_id: str,
    reason: str = "opt_out",
) -> int:
    with get_db() as conn:
        return SalesFollowupRepository(conn).suppress_channel(
            actor=actor,
            lead_id=lead_id,
            reason=reason,
        )


def run_sales_followup_maintenance_batch(
    *,
    limit: int = 100,
    now: datetime | str | None = None,
) -> SalesFollowupMaintenanceResult:
    """Create replay-safe owner reminders for stale leads."""

    with get_db() as conn:
        repository = SalesFollowupRepository(conn)
        stopped = repository.stop_invalid_queued(now=now, limit=limit)
        marked = repository.mark_stale_owner_reminders(now=now, limit=limit)
        return SalesFollowupMaintenanceResult(
            owner_reminders_created=marked,
            stopped_followups=stopped,
        )


__all__ = [
    "SalesFollowupMaintenanceResult",
    "cancel_sales_followup",
    "get_active_sales_followup",
    "run_sales_followup_maintenance_batch",
    "schedule_sales_followup",
    "suppress_sales_followup_channel",
]
