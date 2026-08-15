from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.attribution import AcquisitionSource
from clientplatform.domain.outcomes import OutcomeMoney, OutcomeType
from clientplatform.domain.revenue_attribution import (
    MoneyBreakdown,
    RevenueAttributionInvariantViolation,
    RevenueAttributionModel,
    RevenueAttributionRecord,
    UnitEconomicsSnapshot,
)
from clientplatform.infrastructure.attribution_repository import AttributionRepository


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _serialize_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("revenue attribution timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_datetime(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


_SELECT = """
    SELECT id, business_id, outcome_event_id, outcome_type, customer_id,
           touch_id, attribution_identity_id, source, source_ref_type,
           source_ref_id, promotion_campaign_id, model_version,
           amount_minor, currency, occurred_at, created_at
    FROM revenue_attributions
"""


def _record_from_row(row: Any) -> RevenueAttributionRecord:
    customer_id = _value(row, "customer_id", 4)
    campaign_id = _value(row, "promotion_campaign_id", 10)
    return RevenueAttributionRecord(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        outcome_event_id=str(_value(row, "outcome_event_id", 2)),
        outcome_type=OutcomeType(str(_value(row, "outcome_type", 3))),
        customer_id=None if customer_id is None else str(customer_id),
        touch_id=str(_value(row, "touch_id", 5)),
        attribution_identity_id=str(_value(row, "attribution_identity_id", 6)),
        source=AcquisitionSource(str(_value(row, "source", 7))),
        source_ref_type=str(_value(row, "source_ref_type", 8)),
        source_ref_id=str(_value(row, "source_ref_id", 9)),
        promotion_campaign_id=None if campaign_id is None else str(campaign_id),
        model_version=RevenueAttributionModel(str(_value(row, "model_version", 11))),
        amount_minor=int(_value(row, "amount_minor", 12)),
        currency=str(_value(row, "currency", 13)),
        occurred_at=_parse_datetime(_value(row, "occurred_at", 14)),
        created_at=_parse_datetime(_value(row, "created_at", 15)),
    )


def _semantic(record: RevenueAttributionRecord) -> tuple[object, ...]:
    return (
        record.business_id,
        record.outcome_event_id,
        record.outcome_type.value,
        record.customer_id,
        record.touch_id,
        record.attribution_identity_id,
        record.source.value,
        record.source_ref_type,
        record.source_ref_id,
        record.promotion_campaign_id,
        record.model_version.value,
        record.amount_minor,
        record.currency,
        _serialize_datetime(record.occurred_at),
    )


_SUPPORTED_MONEY_TYPES = (
    OutcomeType.ORDER_PAID.value,
    OutcomeType.REFUND_RECORDED.value,
    OutcomeType.OUTCOME_REVERSAL.value,
)


class RevenueAttributionRepository:
    """Deterministic first-touch attribution for canonical monetary outcomes."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._attribution = AttributionRepository(conn)

    def get_for_outcome(
        self,
        *,
        business_id: str,
        outcome_event_id: str,
        model_version: RevenueAttributionModel = RevenueAttributionModel.FIRST_TOUCH_V1,
    ) -> RevenueAttributionRecord | None:
        row = self._conn.execute(
            _SELECT + " WHERE business_id=? AND outcome_event_id=? AND model_version=? LIMIT 1",
            (str(business_id), str(outcome_event_id), model_version.value),
        ).fetchone()
        return None if row is None else _record_from_row(row)

    def materialize_outcome(
        self,
        *,
        business_id: str,
        outcome_event_id: str,
        created_at: datetime | None = None,
    ) -> RevenueAttributionRecord | None:
        """Persist the canonical first-touch decision for one monetary outcome, if attributable."""

        row = self._conn.execute(
            """
            SELECT id, outcome_type, occurred_at, customer_id, subject_ref,
                   amount_minor, currency
            FROM business_outcome_events
            WHERE business_id=? AND id=?
            LIMIT 1
            """,
            (str(business_id), str(outcome_event_id)),
        ).fetchone()
        if row is None:
            raise RevenueAttributionInvariantViolation("monetary outcome does not belong to this business")
        outcome_type = OutcomeType(str(_value(row, "outcome_type", 1)))
        amount = _value(row, "amount_minor", 5)
        currency = _value(row, "currency", 6)
        if outcome_type.value not in _SUPPORTED_MONEY_TYPES or amount is None or currency is None:
            raise RevenueAttributionInvariantViolation("outcome is not a supported monetary fact")
        raw_amount = int(amount)
        if outcome_type == OutcomeType.ORDER_PAID and raw_amount < 0:
            raise RevenueAttributionInvariantViolation("order_paid amount must not be negative")
        signed_amount = raw_amount
        if outcome_type in {OutcomeType.REFUND_RECORDED, OutcomeType.OUTCOME_REVERSAL}:
            signed_amount = -abs(raw_amount)

        customer_value = _value(row, "customer_id", 3)
        customer_id = None if customer_value is None else str(customer_value)
        subject_value = _value(row, "subject_ref", 4)
        subject_ref = None if subject_value is None else str(subject_value)

        customer_trace = None
        if customer_id is not None:
            customer_trace = self._attribution.get_customer_trace(
                business_id=str(business_id),
                customer_id=customer_id,
            )
        booking_trace = None
        if subject_ref and subject_ref.startswith("booking_slot:"):
            booking_slot_id = subject_ref.removeprefix("booking_slot:").strip()
            if not booking_slot_id:
                raise RevenueAttributionInvariantViolation("booking subject_ref is malformed")
            booking_trace = self._attribution.get_booking_trace(
                business_id=str(business_id),
                booking_slot_id=booking_slot_id,
            )
        trace = booking_trace or customer_trace
        if trace is None:
            return None
        if customer_trace is not None and booking_trace is not None and customer_trace.touch.id != booking_trace.touch.id:
            raise RevenueAttributionInvariantViolation("customer and booking attribution disagree")
        if customer_id is not None and trace.touch.customer_id != customer_id:
            raise RevenueAttributionInvariantViolation("monetary outcome customer does not match acquisition touch")

        stamp = created_at or datetime.now(timezone.utc)
        record = RevenueAttributionRecord(
            id=str(uuid4()),
            business_id=str(business_id),
            outcome_event_id=str(_value(row, "id", 0)),
            outcome_type=outcome_type,
            customer_id=customer_id,
            touch_id=trace.touch.id,
            attribution_identity_id=trace.identity.id,
            source=trace.identity.source,
            source_ref_type=trace.identity.source_ref_type,
            source_ref_id=trace.identity.source_ref_id,
            promotion_campaign_id=trace.identity.promotion_campaign_id,
            model_version=RevenueAttributionModel.FIRST_TOUCH_V1,
            amount_minor=signed_amount,
            currency=str(currency),
            occurred_at=_parse_datetime(_value(row, "occurred_at", 2)),
            created_at=stamp,
        )
        self._conn.execute(
            """
            INSERT OR IGNORE INTO revenue_attributions(
                id, business_id, outcome_event_id, outcome_type, customer_id,
                touch_id, attribution_identity_id, source, source_ref_type,
                source_ref_id, promotion_campaign_id, model_version,
                amount_minor, currency, occurred_at, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.business_id,
                record.outcome_event_id,
                record.outcome_type.value,
                record.customer_id,
                record.touch_id,
                record.attribution_identity_id,
                record.source.value,
                record.source_ref_type,
                record.source_ref_id,
                record.promotion_campaign_id,
                record.model_version.value,
                record.amount_minor,
                record.currency,
                _serialize_datetime(record.occurred_at),
                _serialize_datetime(record.created_at),
            ),
        )
        accepted = self.get_for_outcome(
            business_id=record.business_id,
            outcome_event_id=record.outcome_event_id,
        )
        if accepted is None:
            raise RuntimeError("revenue attribution was not persisted")
        if _semantic(accepted) != _semantic(record):
            raise RevenueAttributionInvariantViolation(
                "monetary outcome already has a different first-touch attribution"
            )
        return accepted

    def reconcile_window(
        self,
        *,
        business_id: str,
        occurred_from: datetime,
        occurred_to: datetime,
    ) -> list[RevenueAttributionRecord]:
        if occurred_from.tzinfo is None or occurred_to.tzinfo is None or occurred_to <= occurred_from:
            raise ValueError("revenue attribution window must be timezone-aware and non-empty")
        rows = self._conn.execute(
            """
            SELECT id
            FROM business_outcome_events
            WHERE business_id=? AND occurred_at>=? AND occurred_at<?
              AND outcome_type IN ('order_paid','refund_recorded','outcome_reversal')
              AND amount_minor IS NOT NULL AND currency IS NOT NULL
            ORDER BY occurred_at, id
            """,
            (
                str(business_id),
                _serialize_datetime(occurred_from),
                _serialize_datetime(occurred_to),
            ),
        ).fetchall()
        accepted: list[RevenueAttributionRecord] = []
        for row in rows:
            record = self.materialize_outcome(
                business_id=str(business_id),
                outcome_event_id=str(_value(row, "id", 0)),
            )
            if record is not None:
                accepted.append(record)
        return accepted

    def snapshot(
        self,
        *,
        business_id: str,
        occurred_from: datetime,
        occurred_to: datetime,
        verified_spend: OutcomeMoney | None = None,
    ) -> UnitEconomicsSnapshot:
        records = self.reconcile_window(
            business_id=business_id,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
        )
        event_rows = self._conn.execute(
            """
            SELECT outcome_type, customer_id
            FROM business_outcome_events
            WHERE business_id=? AND occurred_at>=? AND occurred_at<?
            """,
            (
                str(business_id),
                _serialize_datetime(occurred_from),
                _serialize_datetime(occurred_to),
            ),
        ).fetchall()
        event_types = Counter(str(_value(row, "outcome_type", 0)) for row in event_rows)
        paid_customers = {
            record.customer_id
            for record in records
            if record.outcome_type == OutcomeType.ORDER_PAID
            and record.amount_minor > 0
            and record.customer_id is not None
        }
        monetary_outcomes = sum(
            1
            for row in event_rows
            if str(_value(row, "outcome_type", 0)) in _SUPPORTED_MONEY_TYPES
        )
        revenue_totals: dict[str, int] = defaultdict(int)
        source_counts: Counter[AcquisitionSource] = Counter()
        for record in records:
            revenue_totals[record.currency] += record.amount_minor
            source_counts[record.source] += 1
        breakdown = tuple(
            MoneyBreakdown(currency=currency, amount_minor=amount)
            for currency, amount in sorted(revenue_totals.items())
        )
        attributed_revenue = breakdown[0] if len(breakdown) == 1 else None
        limitations: list[str] = []
        if len(records) != monetary_outcomes:
            limitations.append("attribution_incomplete")
        if len(breakdown) > 1:
            limitations.append("revenue_mixed_currency")
        if verified_spend is None:
            limitations.append("spend_unavailable")

        cpl = booking_cost = cac = roas = None
        if verified_spend is not None:
            if event_types[OutcomeType.LEAD_CREATED.value] > 0:
                cpl = verified_spend.amount_minor // event_types[OutcomeType.LEAD_CREATED.value]
            if event_types[OutcomeType.BOOKING_CREATED.value] > 0:
                booking_cost = verified_spend.amount_minor // event_types[OutcomeType.BOOKING_CREATED.value]
            if paid_customers:
                cac = verified_spend.amount_minor // len(paid_customers)
            if attributed_revenue is None:
                limitations.append("roas_revenue_unavailable")
            elif attributed_revenue.currency != verified_spend.currency:
                limitations.append("spend_currency_mismatch")
            elif verified_spend.amount_minor > 0:
                roas = attributed_revenue.amount_minor * 10_000 // verified_spend.amount_minor

        return UnitEconomicsSnapshot(
            business_id=str(business_id),
            model_version=RevenueAttributionModel.FIRST_TOUCH_V1,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            leads=event_types[OutcomeType.LEAD_CREATED.value],
            qualified_leads=event_types[OutcomeType.LEAD_QUALIFIED.value],
            bookings=event_types[OutcomeType.BOOKING_CREATED.value],
            paid_customers=len(paid_customers),
            monetary_outcomes=monetary_outcomes,
            attributed_monetary_outcomes=len(records),
            unattributed_monetary_outcomes=monetary_outcomes - len(records),
            revenue_by_currency=breakdown,
            spend=verified_spend,
            cpl_minor=cpl,
            cost_per_booking_minor=booking_cost,
            cac_minor=cac,
            roas_basis_points=roas,
            limitations=tuple(dict.fromkeys(limitations)),
            source_breakdown=dict(source_counts),
        )
