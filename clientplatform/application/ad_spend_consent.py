from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from clientplatform.domain.ad_spend import (
    AdSpendAuthorization,
    AdSpendConsentReceipt,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.ad_spend_repository import AdSpendRepository
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
    now: datetime | str | None = None,
    receipt_id: str | None = None,
) -> GrantedAdSpendConsent:
    """Persist an immutable owner consent receipt.

    This operation intentionally does not enqueue or execute a provider mutation.
    Launch remains a separate server-side action and cannot be inferred from an
    earlier DRAFT-publication confirmation.
    """

    with get_db() as conn:
        authorization, receipt = AdSpendRepository(conn).authorize(
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
    """Immediately revoke a non-terminal authorization for the same business."""

    with get_db() as conn:
        return AdSpendRepository(conn).revoke(
            actor=actor,
            authorization_id=authorization_id,
            now=_now(now),
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
