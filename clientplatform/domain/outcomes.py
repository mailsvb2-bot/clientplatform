from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping


class OutcomeType(StrEnum):
    LEAD_CREATED = "lead_created"
    LEAD_QUALIFIED = "lead_qualified"
    BOOKING_CREATED = "booking_created"
    BOOKING_CONFIRMED = "booking_confirmed"
    BOOKING_COMPLETED = "booking_completed"
    ORDER_PAID = "order_paid"
    CUSTOMER_REACTIVATED = "customer_reactivated"
    REFUND_RECORDED = "refund_recorded"
    OUTCOME_CORRECTION = "outcome_correction"
    OUTCOME_REVERSAL = "outcome_reversal"


class OutcomeIdempotencyConflict(ValueError):
    """An idempotency key was reused for a different business fact."""


@dataclass(frozen=True, slots=True)
class OutcomeSource:
    source_type: str
    source_id: str

    def __post_init__(self) -> None:
        for field_name in ("source_type", "source_id"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"{field_name} must not be empty")
            object.__setattr__(self, field_name, value)


@dataclass(frozen=True, slots=True)
class OutcomeMoney:
    """Money stored only as integer minor units with an explicit ISO currency."""

    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
            raise TypeError("amount_minor must be an integer")
        currency = str(self.currency or "").strip().upper()
        if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            raise ValueError("currency must be a three-letter ISO code")
        object.__setattr__(self, "currency", currency)


@dataclass(frozen=True, slots=True)
class BusinessOutcomeEvent:
    """Immutable canonical record of an externally meaningful business outcome."""

    id: str
    business_id: str
    outcome_type: OutcomeType
    occurred_at: datetime
    source: OutcomeSource
    customer_id: str | None
    subject_ref: str | None
    money: OutcomeMoney | None
    idempotency_key: str
    metadata: Mapping[str, Any]
    metadata_version: int
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("id", "business_id", "idempotency_key"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"{field_name} must not be empty")
            object.__setattr__(self, field_name, value)
        if self.customer_id is not None:
            customer_id = str(self.customer_id).strip()
            if not customer_id:
                raise ValueError("customer_id must not be blank")
            object.__setattr__(self, "customer_id", customer_id)
        if self.subject_ref is not None:
            subject_ref = str(self.subject_ref).strip()
            if not subject_ref:
                raise ValueError("subject_ref must not be blank")
            object.__setattr__(self, "subject_ref", subject_ref)
        if not isinstance(self.outcome_type, OutcomeType):
            object.__setattr__(self, "outcome_type", OutcomeType(str(self.outcome_type)))
        if not isinstance(self.source, OutcomeSource):
            raise TypeError("source must be OutcomeSource")
        if self.occurred_at.tzinfo is None or self.created_at.tzinfo is None:
            raise ValueError("outcome timestamps must be timezone-aware")
        if isinstance(self.metadata_version, bool) or int(self.metadata_version) < 1:
            raise ValueError("metadata_version must be a positive integer")
        object.__setattr__(self, "metadata_version", int(self.metadata_version))
        if self.outcome_type in {OutcomeType.ORDER_PAID, OutcomeType.REFUND_RECORDED} and self.money is None:
            raise ValueError(f"{self.outcome_type.value} requires money with currency")

    @property
    def source_type(self) -> str:
        return self.source.source_type

    @property
    def source_id(self) -> str:
        return self.source.source_id

    @property
    def amount_minor(self) -> int | None:
        return None if self.money is None else self.money.amount_minor

    @property
    def currency(self) -> str | None:
        return None if self.money is None else self.money.currency
