from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class PromotionAttribution:
    leads: dict[str, frozenset[str]]
    bookings: dict[str, frozenset[str]]
    won: dict[str, frozenset[str]]


def promotion_event_window(
    date_from: str,
    date_to: str,
    *,
    zone: ZoneInfo,
) -> tuple[str, str]:
    try:
        start_date = date.fromisoformat(date_from)
        end_date = date.fromisoformat(date_to)
    except ValueError as exc:
        raise ValueError("analytics dates must use YYYY-MM-DD") from exc
    if start_date > end_date:
        raise ValueError("analytics date_from must not be after date_to")
    start = datetime.combine(start_date, time.min, tzinfo=zone).astimezone(timezone.utc)
    end = datetime.combine(
        end_date + timedelta(days=1),
        time.min,
        tzinfo=zone,
    ).astimezone(timezone.utc)
    return (
        start.isoformat(timespec="seconds"),
        end.isoformat(timespec="seconds"),
    )


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def load_promotion_attribution(
    conn: Any,
    *,
    business_id: str,
    promotion_campaign_ids: set[str],
    event_from: str,
    event_until: str,
) -> PromotionAttribution:
    """Load exact local promotion outcomes without inventing cross-campaign credit.

    `won` remains intentionally stricter than a customer-stage join: the same
    customer needs a booked event for the same campaign, a sales lead for the
    campaign offering, and a persisted conversation transition to `won` after
    that booking inside the reporting window.
    """

    if not promotion_campaign_ids:
        return PromotionAttribution(leads={}, bookings={}, won={})
    event_rows = conn.execute(
        """
        SELECT campaign_id, event_type, customer_id
        FROM promotion_events
        WHERE business_id=?
          AND event_type IN ('opened','booked')
          AND occurred_at>=?
          AND occurred_at<?
        """,
        (business_id, event_from, event_until),
    ).fetchall()
    won_rows = conn.execute(
        """
        SELECT pe.campaign_id, pe.customer_id, se.payload_json
        FROM promotion_events pe
        JOIN promotion_campaigns pc
          ON pc.id=pe.campaign_id AND pc.business_id=pe.business_id
        JOIN clientplatform_sales_leads sl
          ON sl.business_id=pe.business_id
         AND sl.customer_id=pe.customer_id
         AND sl.offering_id=pc.offering_id
        JOIN clientplatform_sales_events se
          ON se.business_id=sl.business_id
         AND se.lead_id=sl.id
         AND se.event_type='conversation_transition'
        WHERE pe.business_id=?
          AND pe.event_type='booked'
          AND pe.occurred_at>=?
          AND pe.occurred_at<?
          AND se.occurred_at>=pe.occurred_at
          AND se.occurred_at<?
        """,
        (business_id, event_from, event_until, event_until),
    ).fetchall()

    leads: dict[str, set[str]] = defaultdict(set)
    bookings: dict[str, set[str]] = defaultdict(set)
    won: dict[str, set[str]] = defaultdict(set)
    for row in event_rows:
        campaign_id = str(_value(row, "campaign_id", 0))
        if campaign_id not in promotion_campaign_ids:
            continue
        event_type = str(_value(row, "event_type", 1))
        customer_id = str(_value(row, "customer_id", 2))
        if event_type == "opened":
            leads[campaign_id].add(customer_id)
        elif event_type == "booked":
            bookings[campaign_id].add(customer_id)
    for row in won_rows:
        campaign_id = str(_value(row, "campaign_id", 0))
        if campaign_id not in promotion_campaign_ids:
            continue
        try:
            payload = json.loads(str(_value(row, "payload_json", 2) or "{}"))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("to") != "won":
            continue
        won[campaign_id].add(str(_value(row, "customer_id", 1)))
    return PromotionAttribution(
        leads={key: frozenset(value) for key, value in leads.items()},
        bookings={key: frozenset(value) for key, value in bookings.items()},
        won={key: frozenset(value) for key, value in won.items()},
    )


__all__ = [
    "PromotionAttribution",
    "load_promotion_attribution",
    "promotion_event_window",
]
