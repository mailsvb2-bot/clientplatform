from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from clientplatform.domain.sales_ai_policy import SalesAIDataMode, normalize_sales_ai_data_mode
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.sales_ai_consent_repository import (
    SalesAIConsent,
    SalesAIConsentRepository,
)
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from clientplatform.runtime.sales_ai_config import SalesAIRuntimeConfig
from services.db import get_db, get_db_ro


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def business_sales_ai_consent_in_conn(conn: Any, *, business_id: str) -> SalesAIConsent | None:
    return SalesAIConsentRepository(conn).get(business_id=business_id)


def business_sales_ai_enabled_in_conn(conn: Any, *, business_id: str, consent_target: str) -> bool:
    consent = business_sales_ai_consent_in_conn(conn, business_id=business_id)
    return bool(
        consent
        and consent.enabled
        and consent.data_mode != SalesAIDataMode.NO_CLOUD
        and consent.customer_notice_confirmed
        and consent.consent_target == str(consent_target or "").strip()
    )


def get_business_sales_ai_consent(*, actor: TenantContext) -> SalesAIConsent | None:
    with get_db_ro() as conn:
        current = TenancyRepository(conn).resolve_context(user_id=actor.user_id, business_id=actor.business_id)
        current.assert_can_view_customer_records()
        return business_sales_ai_consent_in_conn(conn, business_id=current.business_id)


def get_business_sales_ai_enabled(*, actor: TenantContext) -> bool:
    config = SalesAIRuntimeConfig.from_env()
    if not config.enabled:
        return False
    with get_db_ro() as conn:
        current = TenancyRepository(conn).resolve_context(user_id=actor.user_id, business_id=actor.business_id)
        current.assert_can_view_customer_records()
        return business_sales_ai_enabled_in_conn(conn, business_id=current.business_id, consent_target=config.consent_target)


def _cancel_nonterminal_jobs(conn: Any, *, business_id: str, reason: str, timestamp: str) -> None:
    conn.execute(
        """
        UPDATE clientplatform_sales_ai_jobs
        SET status='done', completed_at=?, updated_at=?, locked_at=NULL, lock_token=NULL,
            last_error_code=?
        WHERE business_id=? AND status IN ('pending','processing','retry')
        """,
        (timestamp, timestamp, reason, business_id),
    )


def set_business_sales_ai_enabled(
    *,
    actor: TenantContext,
    enabled: bool,
    data_mode: SalesAIDataMode | str = SalesAIDataMode.REDACTED,
    customer_notice_confirmed: bool = False,
) -> bool:
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    if not isinstance(customer_notice_confirmed, bool):
        raise ValueError("customer_notice_confirmed must be a boolean")
    mode = normalize_sales_ai_data_mode(data_mode)
    config = SalesAIRuntimeConfig.from_env() if enabled else None
    if enabled and (config is None or not config.enabled):
        raise ValueError("sales AI runtime is disabled")
    if enabled and mode == SalesAIDataMode.NO_CLOUD:
        raise ValueError("no_cloud mode intentionally disables external Sales AI")

    # The consent row is also the cross-process egress barrier. A worker holds a
    # no-op UPDATE lock on this row while the provider request is in flight, so a
    # disable/provider-target change cannot return before earlier egress exits.
    with get_db() as conn:
        current = TenancyRepository(conn).resolve_context(user_id=actor.user_id, business_id=actor.business_id)
        current.assert_can_manage_business()
        timestamp = _utc_now()
        target = config.consent_target if enabled and config is not None else ""
        SalesAIConsentRepository(conn).set(
            business_id=current.business_id,
            enabled=enabled,
            consent_target=target,
            data_mode=mode,
            customer_notice_confirmed=customer_notice_confirmed if enabled else False,
            updated_by_member_id=current.membership_id,
            now=timestamp,
        )
        _cancel_nonterminal_jobs(
            conn,
            business_id=current.business_id,
            reason="consent_epoch_changed" if enabled else "tenant_ai_disabled",
            timestamp=timestamp,
        )
        return enabled


__all__ = [
    "business_sales_ai_consent_in_conn",
    "business_sales_ai_enabled_in_conn",
    "get_business_sales_ai_consent",
    "get_business_sales_ai_enabled",
    "set_business_sales_ai_enabled",
]
