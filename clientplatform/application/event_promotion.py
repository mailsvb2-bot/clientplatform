from __future__ import annotations

"""Event-scoped promotion orchestration over the canonical webinar funnel.

This module intentionally does not reuse the booking-slot PromotionCampaign model:
that contract is structurally bound to an offering and booking slot. Webinar
promotion keeps the event as the source of truth and drives traffic to the
existing public /e/<slug> registration surface, where source/campaign_ref are
already persisted with the registration.
"""

from dataclasses import dataclass
from urllib.parse import quote, urlencode

from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.event_landing_repository import EventLandingRepository
from clientplatform.infrastructure.event_repository import EventRepository
from services.db import get_db_ro


@dataclass(frozen=True, slots=True)
class EventPromotionSnapshot:
    event_id: str
    title: str
    advertising_url: str
    provider_label: str
    join_ready: bool
    landing_published: bool
    registrations: int
    registrations_from_ads: int


def event_advertising_url(
    *,
    public_base_url: object,
    public_slug: object,
    event_id: object,
) -> str:
    base = str(public_base_url or "").strip().rstrip("/")
    slug = str(public_slug or "").strip()
    event = normalize_uuid(event_id, field_name="event_id")
    if not base.startswith("https://"):
        raise ValueError("public event advertising URL requires HTTPS base")
    if not slug:
        raise ValueError("public event slug is required")
    query = urlencode(
        {
            "source": "ads",
            "campaign_ref": f"event:{event}",
        }
    )
    return f"{base}/e/{quote(slug, safe='')}?{query}"


def get_event_promotion_snapshot(
    *,
    actor: TenantContext,
    event_id: str,
    public_base_url: object,
) -> EventPromotionSnapshot:
    actor.assert_can_manage_business()
    normalized_event_id = normalize_uuid(event_id, field_name="event_id")
    with get_db_ro() as conn:
        event = EventRepository(conn).get(actor=actor, event_id=normalized_event_id)
        landing = EventLandingRepository(conn).get(
            actor=actor,
            event_id=normalized_event_id,
        )
        row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN status='registered' THEN 1 ELSE 0 END) AS registrations,
                SUM(
                    CASE
                        WHEN status='registered' AND source='ads' THEN 1
                        ELSE 0
                    END
                ) AS registrations_from_ads
            FROM clientplatform_event_registrations
            WHERE business_id=? AND event_id=?
            """,
            (actor.business_id, normalized_event_id),
        ).fetchone()

    def value(name: str, position: int) -> int:
        if row is None:
            return 0
        raw = row[name] if hasattr(row, "keys") else row[position]
        return int(raw or 0)

    return EventPromotionSnapshot(
        event_id=normalized_event_id,
        title=event.title,
        advertising_url=event_advertising_url(
            public_base_url=public_base_url,
            public_slug=event.public_slug,
            event_id=normalized_event_id,
        ),
        provider_label=str(event.provider_label or event.provider_key or "площадка"),
        join_ready=bool(event.join_is_ready),
        landing_published=bool(landing is not None and landing.is_published),
        registrations=value("registrations", 0),
        registrations_from_ads=value("registrations_from_ads", 1),
    )


__all__ = [
    "EventPromotionSnapshot",
    "event_advertising_url",
    "get_event_promotion_snapshot",
]
