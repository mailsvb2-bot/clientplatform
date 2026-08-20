from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum


ONE_TIME_REVIEW_AFTER = timedelta(days=30)
INACTIVE_REVIEW_AFTER = timedelta(days=90)


class RetentionCohort(StrEnum):
    ONE_TIME_CUSTOMER = "one_time_customer"
    INACTIVE_CUSTOMER = "inactive_customer"


class ReactivationAction(StrEnum):
    REVIEW_REPEAT_OFFER = "review_repeat_offer"
    REVIEW_REACTIVATION_OFFER = "review_reactivation_offer"


@dataclass(frozen=True, slots=True)
class RetentionEvidence:
    customer_id: str
    display_name: str | None
    paid_orders: int
    last_paid_at: datetime
    last_activity_at: datetime

    def __post_init__(self) -> None:
        customer_id = str(self.customer_id or "").strip()
        if not customer_id:
            raise ValueError("customer_id must not be empty")
        object.__setattr__(self, "customer_id", customer_id)
        if self.display_name is not None:
            name = str(self.display_name).strip()
            object.__setattr__(self, "display_name", name or None)
        if isinstance(self.paid_orders, bool) or int(self.paid_orders) < 1:
            raise ValueError("paid_orders must be a positive integer")
        object.__setattr__(self, "paid_orders", int(self.paid_orders))
        for field_name in ("last_paid_at", "last_activity_at"):
            value = getattr(self, field_name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{field_name} must be timezone-aware")
            object.__setattr__(self, field_name, value.astimezone(timezone.utc))
        if self.last_activity_at < self.last_paid_at:
            raise ValueError("last_activity_at cannot precede last_paid_at")


@dataclass(frozen=True, slots=True)
class RetentionCandidate:
    customer_id: str
    display_name: str | None
    cohort: RetentionCohort
    suggested_action: ReactivationAction
    paid_orders: int
    last_paid_at: datetime
    last_activity_at: datetime
    inactive_days: int


def classify_retention_evidence(
    evidence: RetentionEvidence,
    *,
    now: datetime,
) -> RetentionCandidate | None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    stamp = now.astimezone(timezone.utc)
    if evidence.last_activity_at > stamp:
        raise ValueError("last_activity_at cannot be in the future")
    inactive_for = stamp - evidence.last_activity_at
    inactive_days = max(0, inactive_for.days)
    if inactive_for >= INACTIVE_REVIEW_AFTER:
        return RetentionCandidate(
            customer_id=evidence.customer_id,
            display_name=evidence.display_name,
            cohort=RetentionCohort.INACTIVE_CUSTOMER,
            suggested_action=ReactivationAction.REVIEW_REACTIVATION_OFFER,
            paid_orders=evidence.paid_orders,
            last_paid_at=evidence.last_paid_at,
            last_activity_at=evidence.last_activity_at,
            inactive_days=inactive_days,
        )
    if evidence.paid_orders == 1 and inactive_for >= ONE_TIME_REVIEW_AFTER:
        return RetentionCandidate(
            customer_id=evidence.customer_id,
            display_name=evidence.display_name,
            cohort=RetentionCohort.ONE_TIME_CUSTOMER,
            suggested_action=ReactivationAction.REVIEW_REPEAT_OFFER,
            paid_orders=evidence.paid_orders,
            last_paid_at=evidence.last_paid_at,
            last_activity_at=evidence.last_activity_at,
            inactive_days=inactive_days,
        )
    return None


__all__ = [
    "INACTIVE_REVIEW_AFTER",
    "ONE_TIME_REVIEW_AFTER",
    "ReactivationAction",
    "RetentionCandidate",
    "RetentionCohort",
    "RetentionEvidence",
    "classify_retention_evidence",
]
