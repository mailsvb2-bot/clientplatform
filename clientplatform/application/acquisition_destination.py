from __future__ import annotations

"""Channel-neutral acquisition destinations over canonical promotion attribution.

The promotion campaign records *where the acquisition effort belongs* for
attribution. The public destination is intentionally independent of that channel:
a visitor first opens one ClientPlatform HTTPS URL and may then continue through
any unambiguous connected messenger without changing the campaign source token.
"""

from dataclasses import dataclass

from clientplatform.application.messenger_switching import (
    PublicMessengerDestination,
    list_public_messenger_destinations,
)
from clientplatform.application.promotions import (
    PromotionCampaignView,
    create_slot_promotion,
    list_promotable_slots,
    promotion_public_url,
    promotion_start_payload,
)
from clientplatform.domain.promotions import PromotionChannel
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.promotion_repository import PromotionRepository
from services.db import get_db_ro


@dataclass(frozen=True, slots=True)
class AcquisitionDestination:
    campaign_id: str
    business_id: str
    attribution_channel: PromotionChannel
    source_token: str
    public_url: str
    messenger_destinations: tuple[PublicMessengerDestination, ...]

    @property
    def has_native_messenger_destination(self) -> bool:
        return bool(self.messenger_destinations)


@dataclass(frozen=True, slots=True)
class PreparedAcquisitionDestination:
    """One safe nearest-slot acquisition result shared by staff transports."""

    promotion: PromotionCampaignView
    destination: AcquisitionDestination


def build_acquisition_destination(
    *,
    promotion: PromotionCampaignView,
    public_base_url: object,
    attribution_token: str | None = None,
) -> AcquisitionDestination:
    """Build one campaign destination without coupling attribution to transport."""

    campaign = promotion.campaign
    source_token = str(attribution_token or campaign.source_token)
    payload = promotion_start_payload(source_token)
    return AcquisitionDestination(
        campaign_id=campaign.id,
        business_id=campaign.business_id,
        attribution_channel=campaign.channel,
        source_token=source_token,
        public_url=promotion_public_url(
            base_url=public_base_url,
            source_token=source_token,
        ),
        messenger_destinations=list_public_messenger_destinations(
            business_id=campaign.business_id,
            start_payload=payload,
        ),
    )


def prepare_nearest_acquisition_destination(
    *,
    actor: TenantContext,
    public_base_url: object,
    attribution_channel: PromotionChannel = PromotionChannel.WEBSITE,
) -> PreparedAcquisitionDestination | None:
    """Prepare the nearest promotable slot without coupling to staff transport."""

    slots = list_promotable_slots(actor=actor)
    if not slots:
        return None
    slot = min(slots, key=lambda item: item.slot.starts_at)
    promotion = create_slot_promotion(
        actor=actor,
        slot_id=slot.slot.id,
        channel=attribution_channel,
    )
    return PreparedAcquisitionDestination(
        promotion=promotion,
        destination=build_acquisition_destination(
            promotion=promotion,
            public_base_url=public_base_url,
        ),
    )


def resolve_acquisition_destination(
    *,
    source_token: str,
    public_base_url: object,
) -> AcquisitionDestination:
    """Resolve a public source and preserve its exact attribution token to transport."""

    with get_db_ro() as conn:
        repository = PromotionRepository(conn)
        resolution = repository.resolve_public_source(source_token=source_token)
        campaign = resolution.campaign
        promotion = PromotionCampaignView(
            campaign=campaign,
            slot=repository.get_public_campaign_slot(campaign=campaign),
        )
    return build_acquisition_destination(
        promotion=promotion,
        public_base_url=public_base_url,
        attribution_token=resolution.attribution_token,
    )


__all__ = [
    "AcquisitionDestination",
    "PreparedAcquisitionDestination",
    "build_acquisition_destination",
    "prepare_nearest_acquisition_destination",
    "resolve_acquisition_destination",
]
