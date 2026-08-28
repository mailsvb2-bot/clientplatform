from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from clientplatform.domain.outcomes import (
    BusinessOutcomeEvent,
    OutcomeMoney,
    OutcomeSource,
    OutcomeType,
)
from clientplatform.domain.retention import ReactivationAction, RetentionCandidate, RetentionCohort
from clientplatform.domain.sales import ContactBasis, SalesInvariantViolation, SalesLead, SalesLeadStage
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.outcome_repository import OutcomeRepository
from clientplatform.infrastructure.retention_repository import RetentionRepository
from clientplatform.infrastructure.revenue_attribution_repository import RevenueAttributionRepository
from clientplatform.infrastructure.sales_repository import SalesRepository
from services.db import get_db, get_db_ro


class RetentionCandidateUnavailable(ValueError):
    """The reviewed customer is no longer in the expected retention cohort."""


@dataclass(frozen=True, slots=True)
class ReactivationOpportunity:
    """Read-only retention candidate paired with a currently permitted channel route."""

    candidate: RetentionCandidate
    route_platform: str | None


@dataclass(frozen=True, slots=True)
class PreparedReactivation:
    candidate: RetentionCandidate
    lead: SalesLead
    route_platform: str | None


@dataclass(frozen=True, slots=True)
class RecordedReactivation:
    """Canonical proof that one approved reactivation cycle produced repeat revenue."""

    lead: SalesLead
    payment_outcome: BusinessOutcomeEvent
    reactivation_outcome: BusinessOutcomeEvent


def _stamp(value: datetime | None) -> datetime:
    stamp = value or datetime.now(timezone.utc)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return stamp.astimezone(timezone.utc).replace(microsecond=0)


def _next_action(candidate: RetentionCandidate) -> str:
    if candidate.suggested_action == ReactivationAction.REVIEW_REPEAT_OFFER:
        return "Подготовить повторное предложение для клиента"
    return "Подготовить предложение для возврата клиента"


def _reactivation_cohort(lead: SalesLead) -> RetentionCohort:
    source_ref = str(lead.source_ref or "").strip()
    expected_prefix = f"reactivation:"
    opportunity_prefix = f"reactivation:{lead.customer_id}:"
    if (
        lead.contact_basis != ContactBasis.EXISTING_CUSTOMER
        or not source_ref.startswith(expected_prefix)
        or not lead.opportunity_key.startswith(opportunity_prefix)
    ):
        raise SalesInvariantViolation("sales lead is not a canonical reactivation cycle")
    try:
        return RetentionCohort(source_ref.removeprefix(expected_prefix))
    except ValueError as exc:
        raise SalesInvariantViolation("reactivation lead has an unknown cohort") from exc


def _validate_existing_payment(
    event: BusinessOutcomeEvent,
    *,
    lead: SalesLead,
    money: OutcomeMoney,
) -> None:
    if (
        event.outcome_type != OutcomeType.ORDER_PAID
        or event.customer_id != lead.customer_id
        or event.source_type != "sales_reactivation"
        or event.source_id != lead.id
        or event.subject_ref != f"sales_lead:{lead.id}"
        or event.money != money
    ):
        raise SalesInvariantViolation("reactivation payment evidence conflicts with the requested result")


def _validate_existing_reactivation(
    event: BusinessOutcomeEvent,
    *,
    lead: SalesLead,
    payment: BusinessOutcomeEvent,
    money: OutcomeMoney,
) -> None:
    if (
        event.outcome_type != OutcomeType.CUSTOMER_REACTIVATED
        or event.customer_id != lead.customer_id
        or event.source_type != "outcome_event"
        or event.source_id != payment.id
        or event.subject_ref != f"sales_lead:{lead.id}"
        or event.money != money
    ):
        raise SalesInvariantViolation("reactivation outcome evidence conflicts with the requested result")


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


def list_reactivation_opportunities(
    *,
    actor: TenantContext,
    now: datetime | None = None,
    limit: int = 100,
) -> list[ReactivationOpportunity]:
    """Pair deterministic retention cohorts with the existing safe channel route.

    A missing route is preserved as explicit evidence instead of being guessed.
    This projection does not create a sales lead and never queues a message.
    """

    stamp = _stamp(now)
    with get_db_ro() as conn:
        repository = RetentionRepository(conn)
        candidates = repository.list_candidates(actor=actor, now=stamp, limit=limit)
        routes = repository.preferred_reactivation_channels(
            actor=actor,
            customer_ids=tuple(candidate.customer_id for candidate in candidates),
        )
        return [
            ReactivationOpportunity(
                candidate=candidate,
                route_platform=routes.get(candidate.customer_id),
            )
            for candidate in candidates
        ]


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


def record_reactivation_result(
    *,
    actor: TenantContext,
    lead_id: str,
    amount_minor: int,
    currency: str,
    now: datetime | None = None,
) -> RecordedReactivation:
    """Record owner-confirmed repeat payment and the resulting reactivation atomically.

    The payment is a canonical ``order_paid`` fact. ``customer_reactivated`` links
    that money to the exact approved reactivation sales cycle without replacing
    acquisition attribution. Replays with the same money are idempotent; a
    conflicting replay fails closed.
    """

    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor <= 0:
        raise ValueError("amount_minor must be a positive integer")
    money = OutcomeMoney(amount_minor=amount_minor, currency=currency)
    stamp = _stamp(now)
    timestamp = stamp.isoformat(timespec="seconds")

    with get_db() as conn:
        sales = SalesRepository(conn)
        lead = sales.get_lead(actor=actor, lead_id=lead_id)
        cohort = _reactivation_cohort(lead)
        outcomes = OutcomeRepository(conn)
        payment_key = f"reactivation-order-paid:{lead.id}"
        reactivation_key = f"customer-reactivated:{lead.id}"

        payment = outcomes.get_by_idempotency_key(
            business_id=lead.business_id,
            idempotency_key=payment_key,
        )
        reactivation = outcomes.get_by_idempotency_key(
            business_id=lead.business_id,
            idempotency_key=reactivation_key,
        )
        if reactivation is not None and payment is None:
            raise SalesInvariantViolation("reactivation outcome exists without its payment evidence")
        if lead.stage == SalesLeadStage.WON and (payment is None or reactivation is None):
            raise SalesInvariantViolation("won reactivation lead has no canonical reactivation outcome")
        if lead.stage == SalesLeadStage.LOST:
            raise SalesInvariantViolation("lost reactivation lead must be reopened before recording return")

        if payment is None:
            payment = outcomes.append(
                BusinessOutcomeEvent(
                    id=str(uuid4()),
                    business_id=lead.business_id,
                    outcome_type=OutcomeType.ORDER_PAID,
                    occurred_at=stamp,
                    source=OutcomeSource(source_type="sales_reactivation", source_id=lead.id),
                    customer_id=lead.customer_id,
                    subject_ref=f"sales_lead:{lead.id}",
                    money=money,
                    idempotency_key=payment_key,
                    metadata={
                        "reactivation_lead_id": lead.id,
                        "cohort": cohort.value,
                    },
                    metadata_version=1,
                    created_at=stamp,
                )
            )
        else:
            _validate_existing_payment(payment, lead=lead, money=money)

        # Keep the existing acquisition revenue spine current when the original
        # customer has a canonical first touch. Missing attribution is an honest
        # no-op; the repeat payment remains a durable outcome either way.
        RevenueAttributionRepository(conn).materialize_outcome(
            business_id=lead.business_id,
            outcome_event_id=payment.id,
            created_at=stamp,
        )

        if reactivation is None:
            reactivation = outcomes.append(
                BusinessOutcomeEvent(
                    id=str(uuid4()),
                    business_id=lead.business_id,
                    outcome_type=OutcomeType.CUSTOMER_REACTIVATED,
                    occurred_at=payment.occurred_at,
                    source=OutcomeSource(source_type="outcome_event", source_id=payment.id),
                    customer_id=lead.customer_id,
                    subject_ref=f"sales_lead:{lead.id}",
                    money=money,
                    idempotency_key=reactivation_key,
                    metadata={
                        "reactivation_lead_id": lead.id,
                        "payment_outcome_event_id": payment.id,
                        "cohort": cohort.value,
                    },
                    metadata_version=1,
                    created_at=stamp,
                )
            )
        else:
            _validate_existing_reactivation(
                reactivation,
                lead=lead,
                payment=payment,
                money=money,
            )

        lead = sales.set_stage(
            actor=actor,
            lead_id=lead.id,
            stage=SalesLeadStage.WON,
            reason="customer_reactivated",
            now=timestamp,
        )
        sales.record_event(
            actor=actor,
            lead_id=lead.id,
            event_type="reactivation_outcome_recorded",
            dedupe_key=f"reactivation-outcome:{reactivation.id}",
            payload={
                "cohort": cohort.value,
                "payment_outcome_event_id": payment.id,
                "reactivation_outcome_event_id": reactivation.id,
                "amount_minor": money.amount_minor,
                "currency": money.currency,
            },
            now=timestamp,
        )
        return RecordedReactivation(
            lead=lead,
            payment_outcome=payment,
            reactivation_outcome=reactivation,
        )


__all__ = [
    "PreparedReactivation",
    "ReactivationOpportunity",
    "RecordedReactivation",
    "RetentionCandidateUnavailable",
    "list_reactivation_opportunities",
    "list_retention_candidates",
    "prepare_reactivation_sales_lead",
    "record_reactivation_result",
]
