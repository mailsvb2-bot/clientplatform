from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone

from clientplatform.domain.ad_connections import AdConnection, AdProvider
from clientplatform.domain.ad_spend import (
    AdSpendAuthorization,
    AdSpendInvariantViolation,
    ProviderBudgetSnapshot,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.ad_credential_vault import (
    AdCredentialVault,
    AgeAdCredentialVault,
)
from clientplatform.infrastructure.ad_spend_preparation_repository import (
    AdSpendPreparationRepository,
    AdSpendPreparationTarget,
)
from clientplatform.infrastructure.ad_spend_repository import AdSpendRepository
from clientplatform.infrastructure.ad_worker_store import AdWorkerStore
from clientplatform.integrations.yandex_direct import (
    YandexDirectError,
    YandexOAuthConfig,
    YandexTokenBundle,
)
from clientplatform.integrations.yandex_direct_budget import (
    ReadOnlyYandexDirectBudgetProvider,
    YandexCampaignBudgetReadout,
    YandexDailySpendReadout,
    reconcile_yandex_budget_snapshot,
)
from services.db import get_db, get_db_ro


_AUTH_ERRORS = {
    "provider_http_401",
    "provider_53",
    "provider_54",
    "provider_55",
    "provider_56",
    "provider_invalid_token",
    "provider_unauthorized",
    "oauth_refresh_token_missing",
}
_DEFAULT_REPORT_TIMEZONE = "Europe/Moscow"


@dataclass(frozen=True, slots=True)
class PreparedAdSpendAuthorization:
    authorization: AdSpendAuthorization
    snapshot: ProviderBudgetSnapshot
    campaign: YandexCampaignBudgetReadout
    daily_spend: YandexDailySpendReadout


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("now must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _expiry(value: datetime | str) -> datetime:
    try:
        parsed = _timestamp(value)
    except ValueError as exc:
        raise ValueError("authorization_expires_at must be timezone-aware") from exc
    return parsed


def _report_date(value: date | str, *, now: datetime) -> str:
    if isinstance(value, datetime):
        raise ValueError("provider_report_date must not include time")
    if isinstance(value, date):
        parsed = value
    else:
        try:
            parsed = date.fromisoformat(str(value).strip())
        except ValueError as exc:
            raise ValueError("provider_report_date must use YYYY-MM-DD") from exc
    if abs((parsed - now.date()).days) > 1:
        raise AdSpendInvariantViolation(
            "provider report date is outside the current account-day boundary"
        )
    return parsed.isoformat()


def _report_timezone() -> str:
    return str(
        os.getenv("CLIENTPLATFORM_YANDEX_DIRECT_REPORT_TIMEZONE")
        or _DEFAULT_REPORT_TIMEZONE
    ).strip()


def _vault() -> AdCredentialVault:
    return AgeAdCredentialVault()


def _redirect_uri() -> str:
    explicit = (os.getenv("CLIENTPLATFORM_AD_OAUTH_REDIRECT_URI") or "").strip()
    if explicit:
        return explicit
    domain = (os.getenv("CLIENTPLATFORM_DOMAIN") or "").strip()
    if not domain:
        raise RuntimeError("CLIENTPLATFORM_DOMAIN is required for advertising OAuth")
    return f"https://{domain}/oauth/yandex-direct/callback"


def _provider() -> ReadOnlyYandexDirectBudgetProvider:
    enabled = (os.getenv("CLIENTPLATFORM_AD_CONNECTIONS_ENABLED") or "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        raise RuntimeError("advertising account connections are disabled")
    client_id = (os.getenv("CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_ID") or "").strip()
    if not client_id:
        raise RuntimeError("Yandex Direct OAuth application is not configured")
    return ReadOnlyYandexDirectBudgetProvider(
        oauth=YandexOAuthConfig(
            client_id=client_id,
            client_secret=(
                os.getenv("CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_SECRET") or ""
            ).strip(),
            redirect_uri=_redirect_uri(),
        )
    )


def _refresh_bundle(
    *,
    provider: ReadOnlyYandexDirectBudgetProvider,
    vault: AdCredentialVault,
    connection: AdConnection,
    bundle: YandexTokenBundle,
    now: datetime,
) -> YandexTokenBundle:
    refreshed = provider.refresh(bundle=bundle)
    with get_db() as conn:
        AdWorkerStore(conn, vault=vault).replace_token_bundle(
            connection=connection,
            token_bundle_json=refreshed.to_json(),
            now=now.isoformat(timespec="seconds"),
        )
    return refreshed


def _read_provider_evidence(
    *,
    provider: ReadOnlyYandexDirectBudgetProvider,
    target: AdSpendPreparationTarget,
    bundle: YandexTokenBundle,
    report_date: str,
    captured_at: datetime,
) -> tuple[YandexCampaignBudgetReadout, YandexDailySpendReadout]:
    campaign = provider.campaign_budget_readout(
        access_token=bundle.access_token,
        external_campaign_id=target.external_campaign_id,
        captured_at=captured_at,
        client_login=target.external_login,
    )
    daily_spend = provider.daily_spend_readout(
        access_token=bundle.access_token,
        campaign=campaign,
        report_date=report_date,
        captured_at=captured_at,
        client_login=target.external_login,
    )
    return campaign, daily_spend


def prepare_ad_spend_authorization(
    *,
    actor: TenantContext,
    publication_job_id: str,
    hard_cap_minor: int,
    daily_cap_minor: int,
    authorization_expires_at: datetime | str,
    provider_report_date: date | str,
    now: datetime | str | None = None,
    vault: AdCredentialVault | None = None,
    provider: ReadOnlyYandexDirectBudgetProvider | None = None,
) -> PreparedAdSpendAuthorization:
    """Prepare a persisted authorization from fresh, read-only provider evidence.

    ``provider_report_date`` is an internal server-side value. No Telegram or
    frontend handler is wired to this function in this slice.
    """

    current_time = _timestamp(now or _utc_now())
    expires_at = _expiry(authorization_expires_at)
    ttl_seconds = math.ceil((expires_at - current_time).total_seconds())
    if not 1 <= ttl_seconds <= 300:
        raise AdSpendInvariantViolation(
            "authorization validity must be between 1 and 300 seconds"
        )
    report_date = _report_date(provider_report_date, now=current_time)
    selected_vault = vault or _vault()
    selected_provider = provider or _provider()

    with get_db_ro() as conn:
        current, target = AdSpendPreparationRepository(conn).load_submitted_target(
            actor=actor,
            publication_job_id=publication_job_id,
        )
        connection, token_json = AdWorkerStore(
            conn,
            vault=selected_vault,
        ).load_active(
            business_id=current.business_id,
            connection_id=target.connection_id,
        )
    if connection.provider != AdProvider.YANDEX_DIRECT:
        raise AdSpendInvariantViolation("advertising provider is unsupported")
    if connection.external_account_id != target.external_account_id:
        raise AdSpendInvariantViolation(
            "advertising account changed during authorization preparation"
        )

    bundle = YandexTokenBundle.from_json(token_json)
    try:
        campaign, daily_spend = _read_provider_evidence(
            provider=selected_provider,
            target=target,
            bundle=bundle,
            report_date=report_date,
            captured_at=current_time,
        )
    except YandexDirectError as exc:
        if exc.code not in _AUTH_ERRORS or not bundle.refresh_token:
            raise
        bundle = _refresh_bundle(
            provider=selected_provider,
            vault=selected_vault,
            connection=connection,
            bundle=bundle,
            now=current_time,
        )
        campaign, daily_spend = _read_provider_evidence(
            provider=selected_provider,
            target=target,
            bundle=bundle,
            report_date=report_date,
            captured_at=current_time,
        )

    snapshot = reconcile_yandex_budget_snapshot(
        connection_id=target.connection_id,
        external_account_id=target.external_account_id,
        campaign=campaign,
        daily_spend=daily_spend,
        expected_report_date=report_date,
        now=current_time,
        provider_timezone=_report_timezone(),
        validity_seconds=ttl_seconds,
    )
    if not snapshot.launch_eligible:
        raise AdSpendInvariantViolation(
            "provider evidence is not eligible for advertising spend"
        )

    with get_db() as conn:
        authorization = AdSpendRepository(conn).create_or_get_draft(
            actor=current,
            publication_job_id=target.publication_job_id,
            snapshot=snapshot,
            region_ids=target.region_ids,
            hard_cap_minor=hard_cap_minor,
            daily_cap_minor=daily_cap_minor,
            authorization_expires_at=expires_at,
            now=current_time,
        )
    return PreparedAdSpendAuthorization(
        authorization=authorization,
        snapshot=snapshot,
        campaign=campaign,
        daily_spend=daily_spend,
    )


__all__ = ["PreparedAdSpendAuthorization", "prepare_ad_spend_authorization"]
