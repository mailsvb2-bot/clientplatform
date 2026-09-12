from __future__ import annotations

"""Acquisition/attribution helpers for provider-neutral online events.

This module intentionally reuses the existing event registration dimensions
(`source`, `campaign_ref`) instead of creating a second advertising or attribution
brain. Advertising integrations can use ``build_event_registration_url`` as the
campaign destination; the resulting event funnel can then be sliced by the exact
source/campaign reference that arrived on the public landing page.
"""

from dataclasses import dataclass
import re
from urllib.parse import urlencode

from clientplatform.application.event_analytics import CurrencyRevenue
from clientplatform.domain.ad_connections import normalize_external_campaign_id
from clientplatform.domain.events import EventValidationError, validate_external_https_url
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.event_repository import EventRepository
from services.db import get_db_ro


_DIRECT_SOURCE = "direct"


def _value(row: object, key: str, position: int) -> object:
    return row[key] if hasattr(row, "keys") else row[position]  # type: ignore[index]


def _clean_dimension(value: object | None, *, field_name: str, maximum: int) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).replace("\x00", " ").split()).strip()
    if not normalized:
        return None
    if len(normalized) > maximum:
        raise EventValidationError(f"{field_name} is too long")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise EventValidationError(f"{field_name} contains control characters")
    return normalized


def build_event_registration_url(
    *,
    public_base_url: object,
    public_slug: str,
    source: object | None = None,
    campaign_ref: object | None = None,
) -> str:
    """Build the first-party event landing URL used by an acquisition campaign.

    ``source`` should identify the channel (for example ``yandex_direct``), while
    ``campaign_ref`` should be the stable provider/canonical campaign identifier.
    The public registration surface already persists both dimensions.
    """

    base = validate_external_https_url(public_base_url, field_name="public_base_url").rstrip("/")
    slug = str(public_slug or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,96}", slug):
        raise EventValidationError("invalid public_slug")
    normalized_source = _clean_dimension(source, field_name="source", maximum=160)
    normalized_campaign = _clean_dimension(
        campaign_ref, field_name="campaign_ref", maximum=200
    )
    params: list[tuple[str, str]] = []
    if normalized_source:
        params.append(("source", normalized_source))
    if normalized_campaign:
        params.append(("campaign_ref", normalized_campaign))
    suffix = "" if not params else "?" + urlencode(params)
    return f"{base}/e/{slug}{suffix}"


def build_yandex_event_registration_url(
    *,
    public_base_url: object,
    public_slug: str,
    external_campaign_id: object,
) -> str:
    """Build a Yandex Direct destination whose campaign id survives to revenue."""

    campaign_id = normalize_external_campaign_id(external_campaign_id)
    return build_event_registration_url(
        public_base_url=public_base_url,
        public_slug=public_slug,
        source="yandex_direct",
        campaign_ref=campaign_id,
    )


@dataclass(frozen=True, slots=True)
class EventAcquisitionSlice:
    source: str
    campaign_ref: str | None
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
    def attendance_rate(self) -> float:
        return self._rate(self.attendance_confirmed, self.registered)

    @property
    def offer_click_rate(self) -> float:
        return self._rate(self.offer_clicked, self.registered)

    @property
    def paid_rate(self) -> float:
        return self._rate(self.paid, self.registered)


@dataclass(frozen=True, slots=True)
class EventRoas:
    source: str
    campaign_ref: str | None
    currency: str
    spend_minor: int
    revenue_minor: int

    @property
    def roas(self) -> float | None:
        return None if self.spend_minor <= 0 else round(self.revenue_minor / self.spend_minor, 4)

    @property
    def profit_after_ad_spend_minor(self) -> int:
        return self.revenue_minor - self.spend_minor


def event_roas(
    acquisition: EventAcquisitionSlice,
    *,
    spend_minor: int,
    currency: str,
) -> EventRoas:
    """Combine trusted ad-spend evidence with the event's first-party revenue.

    Spend stays outside the event subsystem and must come from the canonical ad
    spend/reconciliation boundary. This helper only combines already-trusted
    spend with first-party event revenue; it never invents provider spend.
    """

    if isinstance(spend_minor, bool) or not isinstance(spend_minor, int) or spend_minor < 0:
        raise ValueError("spend_minor must be a non-negative integer")
    normalized_currency = str(currency or "").strip().upper()
    if len(normalized_currency) != 3 or not normalized_currency.isalpha():
        raise ValueError("currency must be a three-letter code")
    revenue_minor = sum(
        item.amount_minor
        for item in acquisition.revenue
        if item.currency.upper() == normalized_currency
    )
    return EventRoas(
        source=acquisition.source,
        campaign_ref=acquisition.campaign_ref,
        currency=normalized_currency,
        spend_minor=spend_minor,
        revenue_minor=revenue_minor,
    )


def get_event_acquisition_breakdown_in_transaction(
    conn: object,
    *,
    actor: TenantContext,
    event_id: str,
) -> tuple[EventAcquisitionSlice, ...]:
    """Return the event funnel grouped by exact acquisition source/campaign."""

    event = EventRepository(conn).get(actor=actor, event_id=event_id)
    metric_rows = conn.execute(  # type: ignore[attr-defined]
        """
        SELECT COALESCE(NULLIF(r.source,''), ?) AS source,
               NULLIF(r.campaign_ref,'') AS campaign_ref,
               COUNT(*) AS registered,
               SUM(CASE WHEN r.first_join_click_at IS NOT NULL THEN 1 ELSE 0 END) AS join_clicked,
               SUM(CASE WHEN r.attendance_confirmed_at IS NOT NULL THEN 1 ELSE 0 END)
                   AS attendance_confirmed,
               SUM(CASE WHEN r.offer_clicked_at IS NOT NULL THEN 1 ELSE 0 END) AS offer_clicked,
               SUM(CASE WHEN EXISTS (
                   SELECT 1 FROM clientplatform_event_conversion_links c
                   WHERE c.business_id=r.business_id
                     AND c.event_id=r.event_id
                     AND c.registration_id=r.id
               ) THEN 1 ELSE 0 END) AS paid
        FROM clientplatform_event_registrations r
        WHERE r.business_id=? AND r.event_id=? AND r.status='registered'
        GROUP BY COALESCE(NULLIF(r.source,''), ?), NULLIF(r.campaign_ref,'')
        ORDER BY registered DESC, source, campaign_ref
        """,
        (_DIRECT_SOURCE, event.business_id, event.id, _DIRECT_SOURCE),
    ).fetchall()
    revenue_rows = conn.execute(  # type: ignore[attr-defined]
        """
        SELECT COALESCE(NULLIF(r.source,''), ?) AS source,
               NULLIF(r.campaign_ref,'') AS campaign_ref,
               c.currency,
               COALESCE(SUM(c.amount_minor),0) AS amount_minor
        FROM clientplatform_event_conversion_links c
        JOIN clientplatform_event_registrations r
          ON r.id=c.registration_id
         AND r.event_id=c.event_id
         AND r.business_id=c.business_id
        WHERE c.business_id=? AND c.event_id=?
          AND r.status='registered'
          AND c.amount_minor IS NOT NULL AND c.currency IS NOT NULL
        GROUP BY COALESCE(NULLIF(r.source,''), ?), NULLIF(r.campaign_ref,''), c.currency
        ORDER BY source, campaign_ref, c.currency
        """,
        (_DIRECT_SOURCE, event.business_id, event.id, _DIRECT_SOURCE),
    ).fetchall()

    revenue_by_key: dict[tuple[str, str | None], list[CurrencyRevenue]] = {}
    for row in revenue_rows:
        source = str(_value(row, "source", 0) or _DIRECT_SOURCE)
        campaign_raw = _value(row, "campaign_ref", 1)
        campaign = None if campaign_raw is None else str(campaign_raw)
        revenue_by_key.setdefault((source, campaign), []).append(
            CurrencyRevenue(
                currency=str(_value(row, "currency", 2)),
                amount_minor=int(_value(row, "amount_minor", 3) or 0),
            )
        )

    result: list[EventAcquisitionSlice] = []
    for row in metric_rows:
        source = str(_value(row, "source", 0) or _DIRECT_SOURCE)
        campaign_raw = _value(row, "campaign_ref", 1)
        campaign = None if campaign_raw is None else str(campaign_raw)
        result.append(
            EventAcquisitionSlice(
                source=source,
                campaign_ref=campaign,
                registered=int(_value(row, "registered", 2) or 0),
                join_clicked=int(_value(row, "join_clicked", 3) or 0),
                attendance_confirmed=int(_value(row, "attendance_confirmed", 4) or 0),
                offer_clicked=int(_value(row, "offer_clicked", 5) or 0),
                paid=int(_value(row, "paid", 6) or 0),
                revenue=tuple(revenue_by_key.get((source, campaign), ())),
            )
        )
    return tuple(result)


def get_event_acquisition_breakdown(
    *,
    actor: TenantContext,
    event_id: str,
) -> tuple[EventAcquisitionSlice, ...]:
    with get_db_ro() as conn:
        return get_event_acquisition_breakdown_in_transaction(
            conn,
            actor=actor,
            event_id=event_id,
        )


__all__ = [
    "EventAcquisitionSlice",
    "EventRoas",
    "build_event_registration_url",
    "build_yandex_event_registration_url",
    "event_roas",
    "get_event_acquisition_breakdown",
    "get_event_acquisition_breakdown_in_transaction",
]
