from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from clientplatform.domain.events import normalize_utc
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.event_repository import EventRepository
from services.db import get_db, get_db_ro


@dataclass(frozen=True, slots=True)
class CurrencyRevenue:
    currency: str
    amount_minor: int


@dataclass(frozen=True, slots=True)
class EventFunnel:
    registered: int
    join_clicked: int
    attendance_confirmed: int
    offer_clicked: int
    paid: int
    revenue: tuple[CurrencyRevenue, ...]

    @staticmethod
    def _rate(numerator: int, denominator: int) -> float:
        return 0.0 if denominator <= 0 else round(numerator / denominator, 4)

    @property
    def join_click_rate(self) -> float:
        return self._rate(self.join_clicked, self.registered)

    @property
    def confirmed_attendance_rate(self) -> float:
        return self._rate(self.attendance_confirmed, self.registered)

    @property
    def registration_to_paid_rate(self) -> float:
        return self._rate(self.paid, self.registered)


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def get_event_funnel_in_transaction(
    conn: Any,
    *,
    actor: TenantContext,
    event_id: str,
) -> EventFunnel:
    event = EventRepository(conn).get(actor=actor, event_id=event_id)
    row = conn.execute(
        """
        SELECT COUNT(*) AS registered,
               SUM(CASE WHEN first_join_click_at IS NOT NULL THEN 1 ELSE 0 END) AS join_clicked,
               SUM(CASE WHEN attendance_confirmed_at IS NOT NULL THEN 1 ELSE 0 END) AS attendance_confirmed,
               SUM(CASE WHEN offer_clicked_at IS NOT NULL THEN 1 ELSE 0 END) AS offer_clicked
        FROM clientplatform_event_registrations
        WHERE business_id=? AND event_id=? AND status='registered'
        """,
        (event.business_id, event.id),
    ).fetchone()
    paid_row = conn.execute(
        """
        SELECT COUNT(DISTINCT registration_id) AS paid
        FROM clientplatform_event_conversion_links
        WHERE business_id=? AND event_id=?
        """,
        (event.business_id, event.id),
    ).fetchone()
    revenue_rows = conn.execute(
        """
        SELECT currency,COALESCE(SUM(amount_minor),0) AS amount_minor
        FROM clientplatform_event_conversion_links
        WHERE business_id=? AND event_id=?
          AND amount_minor IS NOT NULL AND currency IS NOT NULL
        GROUP BY currency
        ORDER BY currency
        """,
        (event.business_id, event.id),
    ).fetchall()
    return EventFunnel(
        registered=int(_value(row, "registered", 0) or 0),
        join_clicked=int(_value(row, "join_clicked", 1) or 0),
        attendance_confirmed=int(_value(row, "attendance_confirmed", 2) or 0),
        offer_clicked=int(_value(row, "offer_clicked", 3) or 0),
        paid=int(_value(paid_row, "paid", 0) or 0),
        revenue=tuple(
            CurrencyRevenue(
                currency=str(_value(item, "currency", 0)),
                amount_minor=int(_value(item, "amount_minor", 1) or 0),
            )
            for item in revenue_rows
        ),
    )


def get_event_funnel(*, actor: TenantContext, event_id: str) -> EventFunnel:
    with get_db_ro() as conn:
        return get_event_funnel_in_transaction(conn, actor=actor, event_id=event_id)


def link_verified_payment_in_transaction(
    conn: Any,
    *,
    business_id: str,
    event_id: str,
    registration_id: str,
    payment_ref: str,
    amount_minor: int | None,
    currency: str | None,
    occurred_at: datetime | str,
) -> bool:
    """Link only an already provider-verified payment to one event registration.

    This helper does not verify payment callbacks itself. Call it only from the
    existing verified-payment boundary, never from an offer click or browser
    redirect.
    """

    business = normalize_uuid(business_id, field_name="business_id")
    event = normalize_uuid(event_id, field_name="event_id")
    registration = normalize_uuid(registration_id, field_name="registration_id")
    ref = str(payment_ref or "").strip()
    if not ref or len(ref) > 512:
        raise ValueError("payment_ref must be 1..512 characters")
    if amount_minor is not None:
        if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor < 0:
            raise ValueError("amount_minor must be a non-negative integer")
    normalized_currency = None
    if currency is not None:
        normalized_currency = str(currency).strip().upper()
        if len(normalized_currency) != 3 or not normalized_currency.isalpha():
            raise ValueError("currency must be a three-letter code")
    if amount_minor is not None and normalized_currency is None:
        raise ValueError("currency is required when amount is present")
    exists = conn.execute(
        """
        SELECT 1
        FROM clientplatform_event_registrations r
        JOIN clientplatform_events e
          ON e.id=r.event_id AND e.business_id=r.business_id
        WHERE r.id=? AND r.event_id=? AND r.business_id=?
        LIMIT 1
        """,
        (registration, event, business),
    ).fetchone()
    if exists is None:
        raise ValueError("event registration does not belong to the supplied business/event")
    timestamp = normalize_utc(occurred_at, field_name="occurred_at").isoformat()
    cursor = conn.execute(
        """
        INSERT INTO clientplatform_event_conversion_links(
            business_id,event_id,registration_id,payment_ref,
            amount_minor,currency,occurred_at
        ) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(business_id,event_id,registration_id,payment_ref) DO NOTHING
        """,
        (
            business,
            event,
            registration,
            ref,
            amount_minor,
            normalized_currency,
            timestamp,
        ),
    )
    return int(getattr(cursor, "rowcount", 0) or 0) == 1


def link_verified_payment(**kwargs: Any) -> bool:
    with get_db() as conn:
        return link_verified_payment_in_transaction(conn, **kwargs)


__all__ = [
    "CurrencyRevenue",
    "EventFunnel",
    "get_event_funnel",
    "get_event_funnel_in_transaction",
    "link_verified_payment",
    "link_verified_payment_in_transaction",
]
