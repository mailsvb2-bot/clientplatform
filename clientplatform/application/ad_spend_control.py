from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from clientplatform.domain.ad_spend import (
    AdSpendAuthorization,
    AdSpendAuthorizationStatus,
    AdSpendConsentReceipt,
    AdSpendInvariantViolation,
    ProviderBudgetSnapshot,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.ad_spend_repository import AdSpendRepository
from clientplatform.infrastructure.ad_spend_revocation_repository import (
    queue_stop_for_revoked_live_authorization,
)
from services.db import get_db, get_db_ro


class AdSpendStopReason(StrEnum):
    HARD_CAP = "hard_cap"
    DAILY_CAP = "daily_cap"
    EXPIRED = "expired"
    REVOKED = "revoked"
    PROVIDER_INELIGIBLE = "provider_ineligible"
    SNAPSHOT_STALE = "snapshot_stale"


@dataclass(frozen=True, slots=True)
class AdSpendConsentView:
    authorization_id: str
    business_id: str
    external_campaign_id: str
    region_ids: tuple[int, ...]
    currency: str
    hard_cap_minor: int
    daily_cap_minor: int
    spent_today_minor: int
    available_budget_minor: int
    authorization_expires_at: str
    terms_hash: str
    snapshot_hash: str
    status: AdSpendAuthorizationStatus


@dataclass(frozen=True, slots=True)
class AdSpendGuardDecision:
    allowed: bool
    stop_reason: AdSpendStopReason | None


def _now(value: datetime | str | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _view(authorization: AdSpendAuthorization) -> AdSpendConsentView:
    return AdSpendConsentView(
        authorization_id=authorization.id,
        business_id=authorization.business_id,
        external_campaign_id=authorization.external_campaign_id,
        region_ids=authorization.region_ids,
        currency=authorization.currency,
        hard_cap_minor=authorization.hard_cap_minor,
        daily_cap_minor=authorization.daily_cap_minor,
        spent_today_minor=authorization.snapshot.spent_today_minor,
        available_budget_minor=authorization.snapshot.available_budget_minor,
        authorization_expires_at=authorization.authorization_expires_at,
        terms_hash=authorization.terms_hash,
        snapshot_hash=authorization.snapshot.snapshot_hash,
        status=authorization.status,
    )


def request_ad_spend_consent(
    *,
    actor: TenantContext,
    authorization_id: str,
    now: datetime | str | None = None,
) -> AdSpendConsentView:
    timestamp = _now(now)
    with get_db() as conn:
        authorization = AdSpendRepository(conn).request_consent(
            actor=actor,
            authorization_id=authorization_id,
            now=timestamp,
        )
    return _view(authorization)


def grant_ad_spend_consent(
    *,
    actor: TenantContext,
    authorization_id: str,
    expected_terms_hash: str,
    expected_snapshot_hash: str,
    now: datetime | str | None = None,
) -> tuple[AdSpendConsentView, AdSpendConsentReceipt]:
    timestamp = _now(now)
    with get_db() as conn:
        repository = AdSpendRepository(conn)
        current = repository.get(
            actor=actor,
            authorization_id=authorization_id,
        )
        if current.status not in {
            AdSpendAuthorizationStatus.AWAITING_CONSENT,
            AdSpendAuthorizationStatus.AUTHORIZED,
        }:
            raise AdSpendInvariantViolation(
                "authorization is not awaiting owner consent"
            )
        if current.terms_hash != str(expected_terms_hash or "").strip():
            raise AdSpendInvariantViolation(
                "consent terms changed before confirmation"
            )
        if current.snapshot.snapshot_hash != str(
            expected_snapshot_hash or ""
        ).strip():
            raise AdSpendInvariantViolation(
                "provider snapshot changed before confirmation"
            )
        authorized, receipt = repository.authorize(
            actor=actor,
            authorization_id=authorization_id,
            receipt_id=str(uuid4()),
            now=timestamp,
        )
    return _view(authorized), receipt


def revoke_ad_spend_consent(
    *,
    actor: TenantContext,
    authorization_id: str,
    now: datetime | str | None = None,
) -> AdSpendConsentView:
    timestamp = _now(now)
    with get_db() as conn:
        repository = AdSpendRepository(conn)
        current = repository.get(
            actor=actor,
            authorization_id=authorization_id,
        )
        was_live = current.status in {
            AdSpendAuthorizationStatus.LAUNCHING,
            AdSpendAuthorizationStatus.ACTIVE,
            AdSpendAuthorizationStatus.STOPPING,
        }
        authorization = repository.revoke(
            actor=actor,
            authorization_id=authorization_id,
            now=timestamp,
        )
        if was_live:
            queue_stop_for_revoked_live_authorization(
                conn,
                business_id=authorization.business_id,
                authorization_id=authorization.id,
                actor_member_id=actor.membership_id,
                now=timestamp,
            )
    return _view(authorization)


def get_ad_spend_consent_view(
    *,
    actor: TenantContext,
    authorization_id: str,
) -> AdSpendConsentView:
    with get_db_ro() as conn:
        authorization = AdSpendRepository(conn).get(
            actor=actor,
            authorization_id=authorization_id,
        )
    return _view(authorization)


def evaluate_ad_spend_guard(
    *,
    authorization: AdSpendAuthorization,
    provider_snapshot: ProviderBudgetSnapshot,
    total_spent_minor: int,
    now: datetime | str | None = None,
) -> AdSpendGuardDecision:
    timestamp = _now(now)
    if authorization.status == AdSpendAuthorizationStatus.REVOKED:
        return AdSpendGuardDecision(False, AdSpendStopReason.REVOKED)
    if timestamp >= _now(authorization.authorization_expires_at):
        return AdSpendGuardDecision(False, AdSpendStopReason.EXPIRED)
    try:
        provider_snapshot.assert_fresh(now=timestamp)
    except AdSpendInvariantViolation:
        return AdSpendGuardDecision(False, AdSpendStopReason.SNAPSHOT_STALE)
    if provider_snapshot.connection_id != authorization.connection_id:
        return AdSpendGuardDecision(False, AdSpendStopReason.PROVIDER_INELIGIBLE)
    if provider_snapshot.external_campaign_id != authorization.external_campaign_id:
        return AdSpendGuardDecision(False, AdSpendStopReason.PROVIDER_INELIGIBLE)
    if (
        provider_snapshot.currency != authorization.currency
        or not provider_snapshot.launch_eligible
    ):
        return AdSpendGuardDecision(False, AdSpendStopReason.PROVIDER_INELIGIBLE)
    if isinstance(total_spent_minor, bool) or int(total_spent_minor) < 0:
        raise ValueError("total_spent_minor must be a non-negative integer")
    if int(total_spent_minor) >= authorization.hard_cap_minor:
        return AdSpendGuardDecision(False, AdSpendStopReason.HARD_CAP)
    if provider_snapshot.spent_today_minor >= authorization.daily_cap_minor:
        return AdSpendGuardDecision(False, AdSpendStopReason.DAILY_CAP)
    if authorization.status not in {
        AdSpendAuthorizationStatus.AUTHORIZED,
        AdSpendAuthorizationStatus.LAUNCHING,
        AdSpendAuthorizationStatus.ACTIVE,
    }:
        return AdSpendGuardDecision(False, AdSpendStopReason.PROVIDER_INELIGIBLE)
    return AdSpendGuardDecision(True, None)


__all__ = [
    "AdSpendConsentView",
    "AdSpendGuardDecision",
    "AdSpendStopReason",
    "evaluate_ad_spend_guard",
    "get_ad_spend_consent_view",
    "grant_ad_spend_consent",
    "request_ad_spend_consent",
    "revoke_ad_spend_consent",
]
