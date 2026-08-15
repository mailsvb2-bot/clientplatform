from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Mapping

from clientplatform.domain.attribution import AcquisitionSource, AttributionModelVersion
from clientplatform.domain.outcomes import OutcomeMoney, OutcomeType


class RevenueAttributionInvariantViolation(ValueError):
    """Revenue attribution would violate a deterministic business invariant."""


class RevenueAttributionModel(StrEnum):
    """Versioned money allocation model built on the canonical acquisition spine."""

    FIRST_TOUCH_V1 = "first_touch_v1"


@dataclass(frozen=True, slots=True)
class RevenueAttributionRecord:
    """Durable decision assigning one monetary outcome to one first acquisition touch."""

    id: str
    business_id: str
    outcome_event_id: str
    outcome_type: OutcomeType
    customer_id: str | None
    touch_id: str | None
    attribution_identity_id: str | None
    source: AcquisitionSource
    source_ref_type: str
    source_ref_id: str
    promotion_campaign_id: str | None
    model_version: RevenueAttributionModel
    amount_minor: int
    currency: str
    occurred_at: datetime
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "id",
            "business_id",
            "outcome_event_id",
            "source_ref_type",
            "source_ref_id",
        ):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"{field_name} must not be empty")
            object.__setattr__(self, field_name, value)
        for field_name in ("touch_id", "attribution_identity_id"):
            raw_value = getattr(self, field_name)
            if raw_value is None:
                continue
            value = str(raw_value).strip()
            if not value:
                raise ValueError(f"{field_name} must not be blank")
            object.__setattr__(self, field_name, value)
        if not isinstance(self.outcome_type, OutcomeType):
            object.__setattr__(self, "outcome_type", OutcomeType(str(self.outcome_type)))
        if self.outcome_type not in {
            OutcomeType.ORDER_PAID,
            OutcomeType.REFUND_RECORDED,
            OutcomeType.OUTCOME_REVERSAL,
        }:
            raise ValueError("revenue attribution requires a monetary revenue/refund/reversal outcome")
        if not isinstance(self.source, AcquisitionSource):
            object.__setattr__(self, "source", AcquisitionSource(str(self.source)))
        if not isinstance(self.model_version, RevenueAttributionModel):
            object.__setattr__(
                self,
                "model_version",
                RevenueAttributionModel(str(self.model_version)),
            )
        if self.model_version.value != AttributionModelVersion.FIRST_TOUCH_V1.value:
            raise ValueError("revenue attribution model must match the acquisition attribution model")
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
            raise TypeError("amount_minor must be an integer")
        currency = str(self.currency or "").strip().upper()
        if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            raise ValueError("currency must be a three-letter ISO code")
        object.__setattr__(self, "currency", currency)
        if self.customer_id is not None:
            customer_id = str(self.customer_id).strip()
            if not customer_id:
                raise ValueError("customer_id must not be blank")
            object.__setattr__(self, "customer_id", customer_id)
        if self.promotion_campaign_id is not None:
            campaign_id = str(self.promotion_campaign_id).strip()
            if not campaign_id:
                raise ValueError("promotion_campaign_id must not be blank")
            object.__setattr__(self, "promotion_campaign_id", campaign_id)
        if self.occurred_at.tzinfo is None or self.created_at.tzinfo is None:
            raise ValueError("revenue attribution timestamps must be timezone-aware")

    @property
    def money(self) -> OutcomeMoney:
        return OutcomeMoney(amount_minor=self.amount_minor, currency=self.currency)


@dataclass(frozen=True, slots=True)
class MoneyBreakdown:
    """One currency-safe total. Different currencies are never merged."""

    currency: str
    amount_minor: int

    def __post_init__(self) -> None:
        OutcomeMoney(amount_minor=self.amount_minor, currency=self.currency)
        object.__setattr__(self, "currency", self.currency.upper())


@dataclass(frozen=True, slots=True)
class UnitEconomicsSnapshot:
    """Explainable business-result snapshot derived from canonical facts only."""

    business_id: str
    model_version: RevenueAttributionModel
    occurred_from: datetime
    occurred_to: datetime
    leads: int
    qualified_leads: int
    bookings: int
    paid_customers: int
    monetary_outcomes: int
    attributed_monetary_outcomes: int
    unattributed_monetary_outcomes: int
    revenue_by_currency: tuple[MoneyBreakdown, ...]
    spend: OutcomeMoney | None
    cpl_minor: int | None
    cost_per_booking_minor: int | None
    cac_minor: int | None
    roas_basis_points: int | None
    limitations: tuple[str, ...]
    source_breakdown: Mapping[AcquisitionSource, int]

    def __post_init__(self) -> None:
        if self.occurred_from.tzinfo is None or self.occurred_to.tzinfo is None:
            raise ValueError("unit economics timestamps must be timezone-aware")
        if self.occurred_to <= self.occurred_from:
            raise ValueError("occurred_to must be after occurred_from")
        for field_name in (
            "leads",
            "qualified_leads",
            "bookings",
            "paid_customers",
            "monetary_outcomes",
            "attributed_monetary_outcomes",
            "unattributed_monetary_outcomes",
        ):
            value = int(getattr(self, field_name))
            if value < 0:
                raise ValueError(f"{field_name} must be non-negative")
            object.__setattr__(self, field_name, value)
        if self.attributed_monetary_outcomes + self.unattributed_monetary_outcomes != self.monetary_outcomes:
            raise ValueError("monetary attribution counts must reconcile")

    @property
    def attribution_complete(self) -> bool:
        return self.unattributed_monetary_outcomes == 0

    @property
    def attributed_revenue(self) -> OutcomeMoney | None:
        if len(self.revenue_by_currency) != 1:
            return None
        item = self.revenue_by_currency[0]
        return OutcomeMoney(amount_minor=item.amount_minor, currency=item.currency)
