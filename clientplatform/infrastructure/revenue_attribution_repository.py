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
    RevenueJourneySnapshot,
    RevenueJourneySourceResult,
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


_OUTCOME_SELECT = """
    SELECT id, outcome_type, occurred_at, customer_id, subject_ref,
           amount_minor, currency, source_type, source_id
    FROM business_outcome_events
"""

_SELECT = """
    SELECT id, business_id, outcome_event_id, outcome_type, customer_id,
           touch_id, attribution_identity_id, source, source_ref_type,
           source_ref_id, promotion_campaign_id, model_version,
           amount_minor, currency, occurred_at, created_at
    FROM revenue_attributions
"""


def _record_from_row(row: Any) -> RevenueAttributionRecord:
    customer_id = _value(row, "customer_id", 4)
    touch_id = _value(row, "touch_id", 5)
    attribution_identity_id = _value(row, "attribution_identity_id", 6)
    campaign_id = _value(row, "promotion_campaign_id", 10)
    return RevenueAttributionRecord(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        outcome_event_id=str(_value(row, "outcome_event_id", 2)),
        outcome_type=OutcomeType(str(_value(row, "outcome_type", 3))),
        customer_id=None if customer_id is None else str(customer_id),
        touch_id=None if touch_id is None else str(touch_id),
        attribution_identity_id=(
            None if attribution_identity_id is None else str(attribution_identity_id)
        ),
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


def _direct_signed_amount(outcome_type: OutcomeType, amount_minor: int) -> int:
    if outcome_type == OutcomeType.ORDER_PAID:
        if amount_minor < 0:
            raise RevenueAttributionInvariantViolation("order_paid amount must not be negative")
        return amount_minor
    if outcome_type in {OutcomeType.REFUND_RECORDED, OutcomeType.OUTCOME_REVERSAL}:
        return -abs(amount_minor)
    raise RevenueAttributionInvariantViolation("outcome is not a supported monetary fact")


class RevenueAttributionRepository:
    """Deterministic first-touch attribution for canonical monetary outcomes."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._attribution = AttributionRepository(conn)

    def _resolve_money(
        self,
        *,
        business_id: str,
        row: Any,
    ) -> tuple[int, str] | None:
        outcome_type = OutcomeType(str(_value(row, "outcome_type", 1)))
        if outcome_type.value not in _SUPPORTED_MONEY_TYPES:
            return None
        amount = _value(row, "amount_minor", 5)
        currency = _value(row, "currency", 6)
        if (amount is None) != (currency is None):
            raise RevenueAttributionInvariantViolation("monetary outcome has incomplete money")
        if amount is not None and currency is not None:
            return _direct_signed_amount(outcome_type, int(amount)), str(currency)
        if outcome_type != OutcomeType.OUTCOME_REVERSAL:
            return None
        source_type = str(_value(row, "source_type", 7) or "").strip()
        source_id = str(_value(row, "source_id", 8) or "").strip()
        if source_type != "outcome_event" or not source_id:
            return None
        referenced = self._conn.execute(
            _OUTCOME_SELECT + " WHERE business_id=? AND id=? LIMIT 1",
            (str(business_id), source_id),
        ).fetchone()
        if referenced is None:
            return None
        referenced_type = OutcomeType(str(_value(referenced, "outcome_type", 1)))
        referenced_amount = _value(referenced, "amount_minor", 5)
        referenced_currency = _value(referenced, "currency", 6)
        if (
            referenced_type.value not in _SUPPORTED_MONEY_TYPES
            or referenced_amount is None
            or referenced_currency is None
        ):
            return None
        referenced_signed = _direct_signed_amount(referenced_type, int(referenced_amount))
        return -referenced_signed, str(referenced_currency)

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
            _OUTCOME_SELECT + " WHERE business_id=? AND id=? LIMIT 1",
            (str(business_id), str(outcome_event_id)),
        ).fetchone()
        if row is None:
            raise RevenueAttributionInvariantViolation("monetary outcome does not belong to this business")
        outcome_type = OutcomeType(str(_value(row, "outcome_type", 1)))
        if outcome_type.value not in _SUPPORTED_MONEY_TYPES:
            raise RevenueAttributionInvariantViolation("outcome is not a supported monetary fact")
        resolved_money = self._resolve_money(business_id=str(business_id), row=row)
        if resolved_money is None:
            return None
        signed_amount, currency = resolved_money
        outcome_occurred_at = _parse_datetime(_value(row, "occurred_at", 2))

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
            if customer_trace is not None and customer_trace.touch.occurred_at > outcome_occurred_at:
                customer_trace = None
        booking_trace = None
        if subject_ref and subject_ref.startswith("booking_slot:"):
            booking_slot_id = subject_ref.removeprefix("booking_slot:").strip()
            if not booking_slot_id:
                raise RevenueAttributionInvariantViolation("booking subject_ref is malformed")
            booking_trace = self._attribution.get_booking_trace(
                business_id=str(business_id),
                booking_slot_id=booking_slot_id,
            )
            if booking_trace is not None and booking_trace.touch.occurred_at > outcome_occurred_at:
                booking_trace = None
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
            currency=currency,
            occurred_at=outcome_occurred_at,
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

    def _event_source(self, *, business_id: str, row: Any) -> AcquisitionSource:
        """Resolve one non-monetary outcome through the existing first-touch spine."""

        occurred_at = _parse_datetime(_value(row, "occurred_at", 2))
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
            if customer_trace is not None and customer_trace.touch.occurred_at > occurred_at:
                customer_trace = None

        booking_trace = None
        if subject_ref and subject_ref.startswith("booking_slot:"):
            booking_slot_id = subject_ref.removeprefix("booking_slot:").strip()
            if booking_slot_id:
                booking_trace = self._attribution.get_booking_trace(
                    business_id=str(business_id),
                    booking_slot_id=booking_slot_id,
                )
                if booking_trace is not None and booking_trace.touch.occurred_at > occurred_at:
                    booking_trace = None

        if (
            customer_trace is not None
            and booking_trace is not None
            and customer_trace.touch.id != booking_trace.touch.id
        ):
            raise RevenueAttributionInvariantViolation(
                "customer and booking attribution disagree in revenue journey"
            )
        trace = booking_trace or customer_trace
        if trace is None:
            return AcquisitionSource.UNKNOWN
        return trace.identity.source

    def journey_snapshot(
        self,
        *,
        business_id: str,
        occurred_from: datetime,
        occurred_to: datetime,
    ) -> RevenueJourneySnapshot:
        """Project source → booking → payment → reactivation without new durable state."""

        if (
            occurred_from.tzinfo is None
            or occurred_to.tzinfo is None
            or occurred_to <= occurred_from
        ):
            raise ValueError("revenue journey window must be timezone-aware and non-empty")

        records = self.reconcile_window(
            business_id=str(business_id),
            occurred_from=occurred_from,
            occurred_to=occurred_to,
        )
        records_by_event = {record.outcome_event_id: record for record in records}
        event_rows = self._conn.execute(
            _OUTCOME_SELECT + " WHERE business_id=? AND occurred_at>=? AND occurred_at<?",
            (
                str(business_id),
                _serialize_datetime(occurred_from),
                _serialize_datetime(occurred_to),
            ),
        ).fetchall()
        event_types = Counter(str(_value(row, "outcome_type", 1)) for row in event_rows)

        verified_totals: dict[str, int] = defaultdict(int)
        attributed_totals: dict[str, int] = defaultdict(int)
        source_revenue: dict[AcquisitionSource, dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        source_counts: dict[AcquisitionSource, Counter[str]] = defaultdict(Counter)
        source_paid_customers: dict[AcquisitionSource, set[str]] = defaultdict(set)
        source_reactivated_customers: dict[AcquisitionSource, set[str]] = defaultdict(set)
        all_paid_customers: set[str] = set()
        all_reactivated_customers: set[str] = set()
        monetary_outcomes = 0
        stage_source_unknown = False

        for record in records:
            attributed_totals[record.currency] += record.amount_minor

        for row in event_rows:
            event_id = str(_value(row, "id", 0))
            outcome_type = OutcomeType(str(_value(row, "outcome_type", 1)))
            customer_value = _value(row, "customer_id", 3)
            customer_id = None if customer_value is None else str(customer_value)

            if outcome_type in {
                OutcomeType.LEAD_CREATED,
                OutcomeType.BOOKING_CREATED,
                OutcomeType.BOOKING_COMPLETED,
                OutcomeType.CUSTOMER_REACTIVATED,
            }:
                source = self._event_source(business_id=str(business_id), row=row)
                if source == AcquisitionSource.UNKNOWN:
                    stage_source_unknown = True
                if outcome_type == OutcomeType.LEAD_CREATED:
                    source_counts[source]["leads"] += 1
                elif outcome_type == OutcomeType.BOOKING_CREATED:
                    source_counts[source]["bookings"] += 1
                elif outcome_type == OutcomeType.BOOKING_COMPLETED:
                    source_counts[source]["completed_bookings"] += 1
                elif customer_id is not None:
                    source_reactivated_customers[source].add(customer_id)
                    all_reactivated_customers.add(customer_id)

            resolved_money = self._resolve_money(business_id=str(business_id), row=row)
            if resolved_money is None:
                continue
            monetary_outcomes += 1
            signed_amount, currency = resolved_money
            verified_totals[currency] += signed_amount
            record = records_by_event.get(event_id)
            money_source = record.source if record is not None else AcquisitionSource.UNKNOWN
            source_revenue[money_source][currency] += signed_amount
            if (
                outcome_type == OutcomeType.ORDER_PAID
                and signed_amount > 0
                and customer_id is not None
            ):
                all_paid_customers.add(customer_id)
                source_paid_customers[money_source].add(customer_id)

        all_sources = set(source_counts) | set(source_revenue) | set(source_paid_customers)
        all_sources |= set(source_reactivated_customers)
        verified_currencies = set(verified_totals)
        rank_currency = next(iter(verified_currencies)) if len(verified_currencies) == 1 else None

        source_rows: list[RevenueJourneySourceResult] = []
        for source in all_sources:
            revenue = tuple(
                MoneyBreakdown(currency=currency, amount_minor=amount)
                for currency, amount in sorted(source_revenue[source].items())
            )
            source_rows.append(
                RevenueJourneySourceResult(
                    source=source,
                    leads=source_counts[source]["leads"],
                    bookings=source_counts[source]["bookings"],
                    completed_bookings=source_counts[source]["completed_bookings"],
                    paid_customers=len(source_paid_customers[source]),
                    reactivated_customers=len(source_reactivated_customers[source]),
                    revenue_by_currency=revenue,
                )
            )

        def source_rank(item: RevenueJourneySourceResult) -> tuple[object, ...]:
            revenue_amount = 0
            if rank_currency is not None:
                revenue_amount = next(
                    (
                        money.amount_minor
                        for money in item.revenue_by_currency
                        if money.currency == rank_currency
                    ),
                    0,
                )
            return (
                item.source == AcquisitionSource.UNKNOWN,
                -revenue_amount,
                -item.paid_customers,
                -item.completed_bookings,
                -item.bookings,
                -item.leads,
                item.source.value,
            )

        source_rows.sort(key=source_rank)
        unattributed_totals: dict[str, int] = {}
        for currency in sorted(set(verified_totals) | set(attributed_totals)):
            difference = verified_totals[currency] - attributed_totals[currency]
            if difference != 0:
                unattributed_totals[currency] = difference

        limitations: list[str] = []
        unattributed_count = monetary_outcomes - len(records)
        if unattributed_count:
            limitations.append("attribution_incomplete")
        if len(verified_totals) > 1:
            limitations.append("verified_revenue_mixed_currency")
        if stage_source_unknown:
            limitations.append("journey_source_incomplete")

        return RevenueJourneySnapshot(
            business_id=str(business_id),
            model_version=RevenueAttributionModel.FIRST_TOUCH_V1,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            leads=event_types[OutcomeType.LEAD_CREATED.value],
            qualified_leads=event_types[OutcomeType.LEAD_QUALIFIED.value],
            bookings=event_types[OutcomeType.BOOKING_CREATED.value],
            confirmed_bookings=event_types[OutcomeType.BOOKING_CONFIRMED.value],
            completed_bookings=event_types[OutcomeType.BOOKING_COMPLETED.value],
            paid_customers=len(all_paid_customers),
            reactivated_customers=len(all_reactivated_customers),
            monetary_outcomes=monetary_outcomes,
            attributed_monetary_outcomes=len(records),
            unattributed_monetary_outcomes=unattributed_count,
            verified_revenue_by_currency=tuple(
                MoneyBreakdown(currency=currency, amount_minor=amount)
                for currency, amount in sorted(verified_totals.items())
            ),
            attributed_revenue_by_currency=tuple(
                MoneyBreakdown(currency=currency, amount_minor=amount)
                for currency, amount in sorted(attributed_totals.items())
            ),
            unattributed_revenue_by_currency=tuple(
                MoneyBreakdown(currency=currency, amount_minor=amount)
                for currency, amount in sorted(unattributed_totals.items())
            ),
            sources=tuple(source_rows),
            limitations=tuple(limitations),
        )

    def snapshot(
        self,
        *,
        business_id: str,
        occurred_from: datetime,
        occurred_to: datetime,
        verified_spend: OutcomeMoney | None = None,
    ) -> UnitEconomicsSnapshot:
        if verified_spend is not None and verified_spend.amount_minor < 0:
            raise ValueError("verified_spend amount_minor must be non-negative")
        records = self.reconcile_window(
            business_id=business_id,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
        )
        event_rows = self._conn.execute(
            _OUTCOME_SELECT + " WHERE business_id=? AND occurred_at>=? AND occurred_at<?",
            (
                str(business_id),
                _serialize_datetime(occurred_from),
                _serialize_datetime(occurred_to),
            ),
        ).fetchall()
        event_types = Counter(str(_value(row, "outcome_type", 1)) for row in event_rows)
        paid_customers = {
            str(_value(row, "customer_id", 3))
            for row in event_rows
            if str(_value(row, "outcome_type", 1)) == OutcomeType.ORDER_PAID.value
            and _value(row, "amount_minor", 5) is not None
            and int(_value(row, "amount_minor", 5)) > 0
            and _value(row, "customer_id", 3) is not None
        }
        monetary_outcomes = sum(
            1
            for row in event_rows
            if self._resolve_money(business_id=str(business_id), row=row) is not None
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
