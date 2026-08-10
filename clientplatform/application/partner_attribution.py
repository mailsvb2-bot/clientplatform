from __future__ import annotations

from dataclasses import dataclass

from clientplatform.infrastructure.partner_repository import PartnerRepository
from services.db import get_db, get_db_ro


@dataclass(frozen=True, slots=True)
class PartnerReferralLanding:
    business_id: str
    campaign_id: str
    candidate_id: str
    candidate_name: str
    referral_token: str


def resolve_partner_referral(*, referral_token: str) -> PartnerReferralLanding:
    with get_db_ro() as conn:
        candidate, campaign = PartnerRepository(conn).resolve_public_referral(
            referral_token=referral_token,
        )
        return PartnerReferralLanding(
            business_id=candidate.business_id,
            campaign_id=campaign.id,
            candidate_id=candidate.id,
            candidate_name=candidate.name,
            referral_token=candidate.referral_token,
        )


def record_partner_referral_open(*, referral_token: str) -> bool:
    """Record a link opening with a repository-generated opaque event key."""

    with get_db() as conn:
        return PartnerRepository(conn).record_referral_event(
            referral_token=referral_token,
            event_type="opened",
        )


def record_partner_referral_result(
    *,
    referral_token: str,
    result_key: str,
) -> bool:
    """Record one confirmed downstream result under a stable non-personal key."""

    key = str(result_key or "").strip()
    if not key or len(key) > 160:
        raise ValueError("partner result key must be a bounded non-empty value")
    with get_db() as conn:
        return PartnerRepository(conn).record_referral_event(
            referral_token=referral_token,
            event_type="result",
            event_key=key,
        )


__all__ = [
    "PartnerReferralLanding",
    "record_partner_referral_open",
    "record_partner_referral_result",
    "resolve_partner_referral",
]
