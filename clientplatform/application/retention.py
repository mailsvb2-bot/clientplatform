from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from clientplatform.domain.retention import ReactivationAction, RetentionCandidate, RetentionCohort
from clientplatform.domain.sales import ContactBasis, SalesInvariantViolation, SalesLead, SalesLeadStage
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.retention_repository import RetentionRepository
from clientplatform.infrastructure.sales_repository import SalesRepository
from services.db import get_db, get_db_ro


class RetentionCandidateUnavailable(ValueError):
    """The reviewed customer is no longer in the expected retention cohort."""


@dataclass(frozen=True, slots=True)
class PreparedReactivation:
    candidate: RetentionCandidate
    lead: SalesLead
    route_platform: str | None


def _stamp(value: datetime | None) -> datetime:
    stamp = value or datetime.now(timezone.utc)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return stamp.astimezone(timezone.utc).replace(microsecond=0)


def _next_action(candidate: RetentionCandidate) -> str:
    if candidate.suggested_action == ReactivationAction.REVIEW_REPEAT_OFFER:
        return "Подготовить повторное предложение для клиента"
    return "Подготовить предложение для возврата клиента"


def list_retention_candidates(
    *,
    actor: TenantContext,
    now: datetime | None = None,
    limit: int = 100,
) -> list[RetentionCandidate]:
    """Read deterministic U-010 candidates without sending or mutating customer state."""

    stamp = _stamp(now)
    with get_db_ro() as conn:
        return RetentionRepository(conn).list_candidates(actor=actor, now=stamp, limit=limit)


def prepare_reactivation_sales_lead(
    *,
    actor: TenantContext,
    customer_id: str,
    expected_cohort: RetentionCohort | str,
    now: datetime | None = None,
) -> PreparedReactivation:
    """Materialize owner-approved reactivation work into the canonical sales contour.

    This operation never queues or sends a customer message. External messaging
    remains exclusively behind the U-009 follow-up/outbox approval boundary.
    """

    stamp = _stamp(now)
    expected = (
        expected_cohort
        if isinstance(expected_cohort, RetentionCohort)
        else RetentionCohort(str(expected_cohort))
    )
    timestamp = stamp.isoformat(timespec="seconds")
    with get_db() as conn:
        retention = RetentionRepository(conn)
        candidate = retention.get_candidate(actor=actor, customer_id=customer_id, now=stamp)
        if candidate is None or candidate.cohort != expected:
            raise RetentionCandidateUnavailable("retention candidate changed; refresh before approving")
        route = retention.preferred_reactivation_channel(actor=actor, customer_id=candidate.customer_id)
        source_kind = route or "manual"
        cycle = candidate.last_activity_at.isoformat(timespec="seconds")
        sales = SalesRepository(conn)
        lead = sales.create_or_refresh_lead(
            actor=actor,
            opportunity_key=f"reactivation:{candidate.customer_id}:{cycle}",
            customer_id=candidate.customer_id,
            source_kind=source_kind,
            source_ref=f"reactivation:{candidate.cohort.value}",
            contact_basis=ContactBasis.EXISTING_CUSTOMER,
            now=timestamp,
        )
        if lead.stage == SalesLeadStage.WON:
            raise SalesInvariantViolation("reactivation cycle is already won")
        if lead.stage == SalesLeadStage.LOST:
            lead = sales.set_stage(
                actor=actor,
                lead_id=lead.id,
                stage=SalesLeadStage.NEW,
                now=timestamp,
            )
        lead = sales.set_next_action(
            actor=actor,
            lead_id=lead.id,
            next_action=_next_action(candidate),
            now=timestamp,
        )
        sales.record_event(
            actor=actor,
            lead_id=lead.id,
            event_type="reactivation_review_approved",
            dedupe_key=f"reactivation-approved:{cycle}",
            payload={
                "cohort": candidate.cohort.value,
                "suggested_action": candidate.suggested_action.value,
                "inactive_days": candidate.inactive_days,
                "route_platform": route,
            },
            now=timestamp,
        )
        return PreparedReactivation(candidate=candidate, lead=lead, route_platform=route)


__all__ = [
    "PreparedReactivation",
    "RetentionCandidateUnavailable",
    "list_retention_candidates",
    "prepare_reactivation_sales_lead",
]
