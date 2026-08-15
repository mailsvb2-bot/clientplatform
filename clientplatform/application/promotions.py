from __future__ import annotations

"""Promotion use cases over ClientPlatform's canonical tenant and booking core."""

from dataclasses import dataclass

from clientplatform.application.customer_role_guard import assert_external_customer
from clientplatform.application.owner_booking_journey import (
    connect_public_storefront_customer,
)
from clientplatform.application.promotion_creatives import (
    PromotionBrief,
    generate_promotion_candidates,
    select_promotion_creative,
)
from clientplatform.domain.bookings import BookingClaim, BookingSlotView
from clientplatform.domain.promotions import (
    PUBLIC_PROMOTION_PREFIX,
    PromotionCampaign,
    PromotionChannel,
    PromotionEventType,
    PromotionSourceResolution,
    PromotionStats,
    normalize_source_token,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.activity_repository import ActivityRepository
from clientplatform.infrastructure.attribution_repository import AttributionRepository
from clientplatform.infrastructure.booking_repository import BookingRepository
from clientplatform.infrastructure.promotion_repository import PromotionRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db import get_db, get_db_ro


@dataclass(frozen=True, slots=True)
class PromotionCampaignView:
    campaign: PromotionCampaign
    slot: BookingSlotView


@dataclass(frozen=True, slots=True)
class PromotionLanding:
    campaign: PromotionCampaign
    slot: BookingSlotView
    customer_id: str
    attribution_token: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "attribution_token",
            normalize_source_token(self.attribution_token or self.campaign.source_token),
        )


def _capture_verified_promotion_touch(
    *,
    conn: object,
    resolution: PromotionSourceResolution,
    customer_id: str,
) -> None:
    """Persist first-party attribution only after PromotionRepository verified the token."""

    campaign = resolution.campaign
    AttributionRepository(conn).capture_promotion_touch(
        business_id=campaign.business_id,
        source_token=resolution.attribution_token,
        campaign_id=campaign.id,
        channel=campaign.channel,
        source_kind=resolution.source_kind,
        source_key=resolution.source_key,
        customer_id=customer_id,
    )


def promotion_start_payload(source_token: str) -> str:
    return PUBLIC_PROMOTION_PREFIX + normalize_source_token(source_token)


def parse_promotion_start_payload(payload: str) -> str | None:
    raw = str(payload or "").strip()
    if not raw.startswith(PUBLIC_PROMOTION_PREFIX):
        return None
    try:
        return normalize_source_token(raw.removeprefix(PUBLIC_PROMOTION_PREFIX))
    except ValueError:
        return None


def list_promotable_slots(
    *,
    actor: TenantContext,
    now: str | None = None,
) -> list[BookingSlotView]:
    """List future open slots using promotion permissions, not customer-record access."""

    with get_db_ro() as conn:
        return PromotionRepository(conn).list_promotable_slots(
            actor=actor,
            now=now,
        )


def create_slot_promotion(
    *,
    actor: TenantContext,
    slot_id: str,
    channel: PromotionChannel | str,
) -> PromotionCampaignView:
    """Create or refresh one channel-specific campaign for an open slot."""

    with get_db() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_promotions()
        bookings = BookingRepository(conn)
        activity = ActivityRepository(conn)
        slot = bookings.get_slot(actor=current, slot_id=slot_id)
        profile = activity.get_profile(actor=current)
        offering = activity.get_offering(
            actor=current,
            offering_id=slot.slot.offering_id,
        )
        brief = PromotionBrief(
            business_name=slot.business_name,
            activity_description=profile.activity_description,
            offering_title=offering.title,
            offering_description=offering.description,
            local_start=slot.local_start,
            duration_minutes=slot.slot.duration_minutes,
        )
        creative = select_promotion_creative(
            generate_promotion_candidates(brief)
        )
        campaign, current_slot = PromotionRepository(conn).create_or_refresh_campaign(
            actor=current,
            slot_id=slot.slot.id,
            channel=channel,
            creative=creative,
        )
        return PromotionCampaignView(campaign=campaign, slot=current_slot)


def list_promotion_campaigns(
    *,
    actor: TenantContext,
) -> list[PromotionCampaignView]:
    with get_db_ro() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        repository = PromotionRepository(conn)
        return [
            PromotionCampaignView(
                campaign=campaign,
                slot=repository.get_public_campaign_slot(campaign=campaign),
            )
            for campaign in repository.list_campaigns(actor=current)
        ]


def promotion_stats(
    *,
    actor: TenantContext,
    campaign_id: str | None = None,
) -> PromotionStats:
    with get_db_ro() as conn:
        return PromotionRepository(conn).stats(
            actor=actor,
            campaign_id=campaign_id,
        )


def open_promotion_link(
    *,
    source_token: str,
    telegram_user_id: int,
    username: str | None,
    display_name: str | None,
) -> PromotionLanding:
    """Connect one visitor and preserve the exact campaign/variant source."""

    token = normalize_source_token(source_token)
    with get_db_ro() as conn:
        initial = PromotionRepository(conn).resolve_public_source(source_token=token)
    link = connect_public_storefront_customer(
        business_id=initial.campaign.business_id,
        telegram_user_id=telegram_user_id,
        username=username,
        display_name=display_name,
    )
    with get_db() as conn:
        repository = PromotionRepository(conn)
        resolution = repository.resolve_public_source(source_token=token)
        campaign = resolution.campaign
        slot = repository.get_public_campaign_slot(campaign=campaign)
        repository.record_event(
            campaign=campaign,
            customer_id=link.customer_id,
            event_type=PromotionEventType.OPENED,
            source_token=resolution.attribution_token,
        )
        _capture_verified_promotion_touch(
            conn=conn,
            resolution=resolution,
            customer_id=link.customer_id,
        )
        return PromotionLanding(
            campaign=campaign,
            slot=slot,
            customer_id=link.customer_id,
            attribution_token=resolution.attribution_token,
        )


def book_promoted_slot(
    *,
    source_token: str,
    telegram_user_id: int,
) -> tuple[BookingClaim, PromotionCampaign]:
    """Book canonically and persist exact source attribution in one transaction."""

    token = normalize_source_token(source_token)
    with get_db() as conn:
        promotions = PromotionRepository(conn)
        resolution = promotions.resolve_public_source(source_token=token)
        campaign = resolution.campaign
        assert_external_customer(
            conn,
            telegram_user_id=telegram_user_id,
            business_id=campaign.business_id,
        )
        claim = BookingRepository(conn).book_slot(
            telegram_user_id=telegram_user_id,
            business_id=campaign.business_id,
            slot_id=campaign.booking_slot_id,
        )
        promotions.record_event(
            campaign=campaign,
            customer_id=claim.customer_id,
            event_type=PromotionEventType.OPENED,
            source_token=resolution.attribution_token,
        )
        promotions.record_event(
            campaign=campaign,
            customer_id=claim.customer_id,
            event_type=PromotionEventType.BOOKED,
            source_token=resolution.attribution_token,
        )
        _capture_verified_promotion_touch(
            conn=conn,
            resolution=resolution,
            customer_id=claim.customer_id,
        )
        AttributionRepository(conn).link_booking_from_customer(
            business_id=campaign.business_id,
            customer_id=claim.customer_id,
            booking_slot_id=campaign.booking_slot_id,
        )
        return claim, campaign


__all__ = [
    "PUBLIC_PROMOTION_PREFIX",
    "PromotionCampaignView",
    "PromotionLanding",
    "book_promoted_slot",
    "create_slot_promotion",
    "list_promotable_slots",
    "list_promotion_campaigns",
    "open_promotion_link",
    "parse_promotion_start_payload",
    "promotion_start_payload",
    "promotion_stats",
]
