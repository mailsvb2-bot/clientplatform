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
    promotion_public_url,
    promotion_start_payload,
)
from clientplatform.domain.promotions import PromotionChannel


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


def build_acquisition_destination(
    *,
    promotion: PromotionCampaignView,
    public_base_url: object,
) -> AcquisitionDestination:
    """Build one campaign destination without coupling attribution to transport."""

    campaign = promotion.campaign
    payload = promotion_start_payload(campaign.source_token)
    return AcquisitionDestination(
        campaign_id=campaign.id,
        business_id=campaign.business_id,
        attribution_channel=campaign.channel,
        source_token=campaign.source_token,
        public_url=promotion_public_url(
            base_url=public_base_url,
            source_token=campaign.source_token,
        ),
        messenger_destinations=list_public_messenger_destinations(
            business_id=campaign.business_id,
            start_payload=payload,
        ),
    )


__all__ = [
    "AcquisitionDestination",
    "build_acquisition_destination",
]
