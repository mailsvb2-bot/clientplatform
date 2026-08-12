from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clientplatform.application.promotion_attribution import (
    load_promotion_attribution,
    promotion_event_window,
)
from clientplatform.domain.creative_growth import (
    CreativeAttributionScope,
    CreativeTrafficPlan,
    CreativeVariantOutcome,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.creative_growth_repository import CreativeGrowthRepository
from services.db import get_db_ro


@dataclass(frozen=True, slots=True)
class CreativeSharedCampaignOutcome:
    promotion_campaign_id: str
    variant_ids: tuple[str, ...]
    leads: int
    bookings: int
    won: int


@dataclass(frozen=True, slots=True)
class CreativeGrowthOutcomeSnapshot:
    plan: CreativeTrafficPlan
    date_from: str
    date_to: str
    variants: tuple[CreativeVariantOutcome, ...]
    shared_campaigns: tuple[CreativeSharedCampaignOutcome, ...]

    @property
    def variant_level_ready(self) -> bool:
        return bool(self.variants) and all(
            item.attribution_scope == CreativeAttributionScope.VARIANT
            for item in self.variants
        )


def _analytics_zone() -> ZoneInfo:
    name = (
        os.getenv("CLIENTPLATFORM_ANALYTICS_TIMEZONE")
        or os.getenv("CLIENTPLATFORM_YANDEX_DIRECT_REPORT_TIMEZONE")
        or "Europe/Moscow"
    ).strip()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError("ClientPlatform analytics timezone is invalid") from exc


def _period(days: int, now: datetime | date | None = None) -> tuple[str, str]:
    span = int(days)
    if span < 1 or span > 90:
        raise ValueError("creative growth analytics period must be between 1 and 90 days")
    zone = _analytics_zone()
    if isinstance(now, datetime):
        end = now.astimezone(zone).date() if now.tzinfo else now.date()
    elif isinstance(now, date):
        end = now
    else:
        end = datetime.now(zone).date()
    start = end.fromordinal(end.toordinal() - span + 1)
    return start.isoformat(), end.isoformat()


def get_creative_growth_outcomes(
    *,
    actor: TenantContext,
    trial_id: str,
    days: int = 30,
    now: datetime | date | None = None,
) -> CreativeGrowthOutcomeSnapshot:
    date_from, date_to = _period(days, now=now)
    zone = _analytics_zone()
    event_from, event_until = promotion_event_window(date_from, date_to, zone=zone)
    with get_db_ro() as conn:
        plan = CreativeGrowthRepository(conn).get(actor=actor, trial_id=trial_id)
        campaigns = [arm.promotion_campaign_id for arm in plan.arms if arm.promotion_campaign_id]
        frequencies = Counter(campaigns)
        attribution = load_promotion_attribution(
            conn,
            business_id=plan.business_id,
            promotion_campaign_ids=set(campaigns),
            event_from=event_from,
            event_until=event_until,
        )

    variants: list[CreativeVariantOutcome] = []
    shared_campaign_ids: set[str] = set()
    for arm in plan.arms:
        campaign_id = arm.promotion_campaign_id
        source_token = arm.promotion_source_token
        if not campaign_id:
            scope = CreativeAttributionScope.UNAVAILABLE
            leads = bookings = won = 0
        elif source_token:
            scope = CreativeAttributionScope.VARIANT
            leads = len(attribution.source_leads.get(source_token, ()))
            bookings = len(attribution.source_bookings.get(source_token, ()))
            won = len(attribution.source_won.get(source_token, ()))
        elif frequencies[campaign_id] > 1:
            # Legacy/shared campaigns without a source-level token remain
            # intentionally unallocated. Never duplicate their totals per arm.
            scope = CreativeAttributionScope.SHARED_CAMPAIGN
            leads = bookings = won = 0
            shared_campaign_ids.add(campaign_id)
        else:
            scope = CreativeAttributionScope.VARIANT
            leads = len(attribution.leads.get(campaign_id, ()))
            bookings = len(attribution.bookings.get(campaign_id, ()))
            won = len(attribution.won.get(campaign_id, ()))
        variants.append(
            CreativeVariantOutcome(
                variant_id=arm.variant_id,
                publication_job_id=arm.publication_job_id,
                promotion_campaign_id=campaign_id,
                attribution_scope=scope,
                leads=leads,
                bookings=bookings,
                won=won,
            )
        )

    shared: list[CreativeSharedCampaignOutcome] = []
    for campaign_id in sorted(shared_campaign_ids):
        shared.append(
            CreativeSharedCampaignOutcome(
                promotion_campaign_id=campaign_id,
                variant_ids=tuple(
                    arm.variant_id
                    for arm in plan.arms
                    if arm.promotion_campaign_id == campaign_id
                ),
                leads=len(attribution.leads.get(campaign_id, ())),
                bookings=len(attribution.bookings.get(campaign_id, ())),
                won=len(attribution.won.get(campaign_id, ())),
            )
        )
    return CreativeGrowthOutcomeSnapshot(
        plan=plan,
        date_from=date_from,
        date_to=date_to,
        variants=tuple(variants),
        shared_campaigns=tuple(shared),
    )


__all__ = [
    "CreativeGrowthOutcomeSnapshot",
    "CreativeSharedCampaignOutcome",
    "get_creative_growth_outcomes",
]
