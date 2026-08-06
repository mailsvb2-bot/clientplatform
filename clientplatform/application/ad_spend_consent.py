from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from clientplatform.domain.ad_spend import (
    AdSpendAuthorization,
    AdSpendAuthorizationStatus,
    AdSpendConsentReceipt,
    AdSpendInvariantViolation,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.ad_spend_repository import AdSpendRepository
from clientplatform.infrastructure.ad_spend_revocation_repository import (
    queue_stop_for_revoked_live_authorization,
)
from services.db import get_db, get_db_ro


@dataclass(frozen=True, slots=True)
class GrantedAdSpendConsent:
    authorization: AdSpendAuthorization
    receipt: AdSpendConsentReceipt


def _now(value: datetime | str | None = None) -> datetime | str:
    return value if value is not None else datetime.now(timezone.utc)


def request_ad_spend_consent(
    *,
    actor: TenantContext,
    authorization_id: str,
    now: datetime | str | None = None,
) -> AdSpendAuthorization:
    """Move a prepared authorization into the explicit owner-consent state."""

    with get_db() as conn:
        return AdSpendRepository(conn).request_consent(
            actor=actor,
            authorization_id=authorization_id,
            now=_now(now),
        )


def grant_ad_spend_consent(
    *,
    actor: TenantContext,
    authorization_id: str,
    expected_terms_hash: str,
    expected_snapshot_hash: str,
    now: datetime | str | None = None,
    receipt_id: str | None = None,
) -> GrantedAdSpendConsent:
    """Persist consent only for the exact terms shown to the owner."""

    with get_db() as conn:
        repository = AdSpendRepository(conn)
        current = repository.get(
            actor=actor,
            authorization_id=authorization_id,
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
        authorization, receipt = repository.authorize(
            actor=actor,
            authorization_id=authorization_id,
            receipt_id=receipt_id or str(uuid4()),
            now=_now(now),
        )
    return GrantedAdSpendConsent(
        authorization=authorization,
        receipt=receipt,
    )


def revoke_ad_spend_consent(
    *,
    actor: TenantContext,
    authorization_id: str,
    now: datetime | str | None = None,
) -> AdSpendAuthorization:
    """Revoke consent and durably queue provider stop when spend may be live."""

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
        revoked = repository.revoke(
            actor=actor,
            authorization_id=authorization_id,
            now=timestamp,
        )
        if not was_live:
            return revoked
        queue_stop_for_revoked_live_authorization(
            conn,
            business_id=revoked.business_id,
            authorization_id=revoked.id,
            actor_member_id=actor.membership_id,
            now=timestamp,
        )
        return repository.get(
            actor=actor,
            authorization_id=authorization_id,
        )


def list_ad_spend_authorizations(
    *,
    actor: TenantContext,
    limit: int = 50,
) -> list[AdSpendAuthorization]:
    with get_db_ro() as conn:
        return AdSpendRepository(conn).list_authorizations(
            actor=actor,
            limit=limit,
        )


__all__ = [
    "GrantedAdSpendConsent",
    "grant_ad_spend_consent",
    "list_ad_spend_authorizations",
    "request_ad_spend_consent",
    "revoke_ad_spend_consent",
]
