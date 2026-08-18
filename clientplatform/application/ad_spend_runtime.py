from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clientplatform.application.ad_spend_control import (
    AdSpendGuardDecision,
    AdSpendStopReason,
    evaluate_ad_spend_guard,
)
from clientplatform.domain.ad_connections import AdProvider
from clientplatform.domain.ad_spend import (
    AdSpendAuthorization,
    AdSpendAuthorizationStatus,
    AdSpendInvariantViolation,
    ProviderBudgetSnapshot,
)
from clientplatform.infrastructure.ad_credential_vault import (
    AdCredentialVault,
    AgeAdCredentialVault,
)
from clientplatform.infrastructure.ad_spend_operation_repository import (
    AdSpendOperationContext,
    AdSpendOperationRepository,
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
    managed_strategy_matches_authorization,
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
_REPORT_TIMEZONE_ENV = "CLIENTPLATFORM_YANDEX_DIRECT_REPORT_TIMEZONE"


@dataclass(frozen=True, slots=True)
class AdSpendSweepResult:
    scanned: int = 0
    allowed: int = 0
    stops_queued: int = 0
    failed_closed: int = 0


def _timestamp(value: datetime | str | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def _report_timezone() -> ZoneInfo:
    configured = (os.getenv(_REPORT_TIMEZONE_ENV) or "").strip()
    if not configured:
        raise AdSpendInvariantViolation(
            f"{_REPORT_TIMEZONE_ENV} is required for server-side daily spend limits"
        )
    try:
        return ZoneInfo(configured)
    except ZoneInfoNotFoundError as exc:
        raise AdSpendInvariantViolation(
            f"{_REPORT_TIMEZONE_ENV} is not a valid IANA timezone"
        ) from exc


def provider_report_date(*, now: datetime | str | None = None) -> str:
    return _timestamp(now).astimezone(_report_timezone()).date().isoformat()


def _provider() -> ReadOnlyYandexDirectBudgetProvider:
    client_id = (os.getenv("CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_ID") or "").strip()
    redirect_uri = (os.getenv("CLIENTPLATFORM_AD_OAUTH_REDIRECT_URI") or "").strip()
    if not client_id or not redirect_uri:
        raise AdSpendInvariantViolation("Yandex Direct provider is not configured")
    return ReadOnlyYandexDirectBudgetProvider(
        oauth=YandexOAuthConfig(
            client_id=client_id,
            client_secret=(
                os.getenv("CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_SECRET") or ""
            ).strip(),
            redirect_uri=redirect_uri,
        )
    )


def _load_authorization_system(
    *,
    business_id: str,
    authorization_id: str,
) -> AdSpendAuthorization:
    with get_db_ro() as conn:
        repository = AdSpendRepository(conn)
        authorization, _version = repository._get_with_version(  # noqa: SLF001
            business_id=business_id,
            authorization_id=authorization_id,
        )
    return authorization


def _read_provider_evidence(
    *,
    provider: ReadOnlyYandexDirectBudgetProvider,
    access_token: str,
    authorization: AdSpendAuthorization,
    external_login: str,
    report_date: str,
    now: datetime,
):
    campaign = provider.campaign_budget_readout(
        access_token=access_token,
        external_campaign_id=authorization.external_campaign_id,
        captured_at=now,
        client_login=external_login,
    )
    daily_spend = provider.daily_spend_readout(
        access_token=access_token,
        campaign=campaign,
        report_date=report_date,
        captured_at=now,
        client_login=external_login,
    )
    return campaign, daily_spend


def fresh_provider_snapshot(
    *,
    authorization: AdSpendAuthorization,
    now: datetime | str | None = None,
    vault: AdCredentialVault | None = None,
    provider: ReadOnlyYandexDirectBudgetProvider | None = None,
) -> ProviderBudgetSnapshot:
    current = _timestamp(now)
    report_date = provider_report_date(now=current)
    selected_vault = vault or AgeAdCredentialVault()
    selected_provider = provider or _provider()

    with get_db_ro() as conn:
        connection, token_json = AdWorkerStore(
            conn,
            vault=selected_vault,
        ).load_active(
            business_id=authorization.business_id,
            connection_id=authorization.connection_id,
        )
    if connection.provider != AdProvider.YANDEX_DIRECT:
        raise AdSpendInvariantViolation("advertising provider is unsupported")
    if connection.external_account_id != authorization.snapshot.external_account_id:
        raise AdSpendInvariantViolation(
            "advertising account changed after owner consent"
        )

    bundle = YandexTokenBundle.from_json(token_json)
    try:
        campaign, daily_spend = _read_provider_evidence(
            provider=selected_provider,
            access_token=bundle.access_token,
            authorization=authorization,
            external_login=connection.external_login,
            report_date=report_date,
            now=current,
        )
    except YandexDirectError as exc:
        if exc.code not in _AUTH_ERRORS or not bundle.refresh_token:
            raise
        refreshed = selected_provider.refresh(bundle=bundle)
        with get_db() as conn:
            AdWorkerStore(conn, vault=selected_vault).replace_token_bundle(
                connection=connection,
                token_bundle_json=refreshed.to_json(),
                now=current.isoformat(timespec="seconds"),
            )
        campaign, daily_spend = _read_provider_evidence(
            provider=selected_provider,
            access_token=refreshed.access_token,
            authorization=authorization,
            external_login=connection.external_login,
            report_date=report_date,
            now=current,
        )

    return reconcile_yandex_budget_snapshot(
        connection_id=authorization.connection_id,
        external_account_id=authorization.snapshot.external_account_id,
        campaign=campaign,
        daily_spend=daily_spend,
        expected_report_date=report_date,
        now=current,
        validity_seconds=30,
    )


def evaluate_runtime_spend_guard(
    *,
    authorization: AdSpendAuthorization,
    provider_snapshot: ProviderBudgetSnapshot,
    now: datetime | str | None = None,
) -> AdSpendGuardDecision:
    current = _timestamp(now)
    if provider_snapshot.external_account_id != authorization.snapshot.external_account_id:
        return AdSpendGuardDecision(False, AdSpendStopReason.PROVIDER_INELIGIBLE)

    timezone_value = _report_timezone()
    consent_day = _timestamp(authorization.snapshot.captured_at).astimezone(
        timezone_value
    ).date()
    current_day = current.astimezone(timezone_value).date()
    if consent_day != current_day:
        return AdSpendGuardDecision(False, AdSpendStopReason.PROVIDER_INELIGIBLE)
    if provider_snapshot.spent_today_minor < authorization.snapshot.spent_today_minor:
        return AdSpendGuardDecision(False, AdSpendStopReason.PROVIDER_INELIGIBLE)
    if not managed_strategy_matches_authorization(
        consented_strategy=authorization.snapshot.strategy,
        current_strategy=provider_snapshot.strategy,
        hard_cap_minor=authorization.hard_cap_minor,
        daily_cap_minor=authorization.daily_cap_minor,
        require_applied_limit=(
            authorization.status == AdSpendAuthorizationStatus.ACTIVE
        ),
    ):
        return AdSpendGuardDecision(False, AdSpendStopReason.PROVIDER_INELIGIBLE)

    total_spent_minor = (
        provider_snapshot.spent_today_minor
        - authorization.snapshot.spent_today_minor
    )
    return evaluate_ad_spend_guard(
        authorization=authorization,
        provider_snapshot=provider_snapshot,
        total_spent_minor=total_spent_minor,
        now=current,
    )


def production_pre_mutation_guard(
    context: AdSpendOperationContext,
    now: datetime,
) -> bool:
    authorization = _load_authorization_system(
        business_id=context.operation.business_id,
        authorization_id=context.operation.authorization_id,
    )
    receipt = authorization.consent_receipt
    if receipt is None or receipt.receipt_hash != context.receipt_hash:
        return False
    if authorization.connection_id != context.connection_id:
        return False
    if authorization.external_campaign_id != context.external_campaign_id:
        return False
    if authorization.currency != context.currency:
        return False
    if authorization.hard_cap_minor != context.hard_cap_minor:
        return False
    if authorization.daily_cap_minor != context.daily_cap_minor:
        return False
    if authorization.authorization_expires_at != context.authorization_expires_at:
        return False

    snapshot = fresh_provider_snapshot(authorization=authorization, now=now)
    return evaluate_runtime_spend_guard(
        authorization=authorization,
        provider_snapshot=snapshot,
        now=now,
    ).allowed


def _active_authorization_ids(*, limit: int) -> list[tuple[str, str]]:
    with get_db_ro() as conn:
        rows = conn.execute(
            """
            SELECT business_id, id
            FROM ad_spend_authorizations
            WHERE status IN ('launching', 'active')
              AND consent_receipt_id IS NOT NULL
            ORDER BY updated_at, id
            LIMIT ?
            """,
            (max(1, min(int(limit), 500)),),
        ).fetchall()
    return [
        (
            str(_value(row, "business_id", 0)),
            str(_value(row, "id", 1)),
        )
        for row in rows
    ]


def _fail_closed(
    exc: BaseException,
) -> tuple[AdSpendGuardDecision, str]:
    return (
        AdSpendGuardDecision(False, AdSpendStopReason.PROVIDER_INELIGIBLE),
        f"provider_guard_{type(exc).__name__.lower()}",
    )


def sweep_active_ad_spend_authorizations(
    *,
    limit: int = 100,
    now: datetime | str | None = None,
) -> AdSpendSweepResult:
    current = _timestamp(now)
    scanned = 0
    allowed = 0
    stops_queued = 0
    failed_closed = 0

    for business_id, authorization_id in _active_authorization_ids(limit=limit):
        scanned += 1
        authorization = _load_authorization_system(
            business_id=business_id,
            authorization_id=authorization_id,
        )
        try:
            if current >= _timestamp(authorization.authorization_expires_at):
                decision = AdSpendGuardDecision(False, AdSpendStopReason.EXPIRED)
            elif authorization.status == AdSpendAuthorizationStatus.REVOKED:
                decision = AdSpendGuardDecision(False, AdSpendStopReason.REVOKED)
            else:
                snapshot = fresh_provider_snapshot(
                    authorization=authorization,
                    now=current,
                )
                decision = evaluate_runtime_spend_guard(
                    authorization=authorization,
                    provider_snapshot=snapshot,
                    now=current,
                )
            reason = (
                decision.stop_reason.value
                if decision.stop_reason is not None
                else "provider_ineligible"
            )
        except YandexDirectError as exc:
            failed_closed += 1
            decision = AdSpendGuardDecision(
                False,
                AdSpendStopReason.PROVIDER_INELIGIBLE,
            )
            reason = f"provider_guard_{exc.code}"
        except AdSpendInvariantViolation as exc:
            failed_closed += 1
            decision, reason = _fail_closed(exc)
        except OSError as exc:
            failed_closed += 1
            decision, reason = _fail_closed(exc)
        except RuntimeError as exc:
            failed_closed += 1
            decision, reason = _fail_closed(exc)
        except ValueError as exc:
            failed_closed += 1
            decision, reason = _fail_closed(exc)

        if decision.allowed:
            allowed += 1
            continue
        with get_db() as conn:
            AdSpendOperationRepository(conn).enqueue_stop_system(
                business_id=business_id,
                authorization_id=authorization_id,
                reason=reason,
                now=current,
            )
        stops_queued += 1

    return AdSpendSweepResult(
        scanned=scanned,
        allowed=allowed,
        stops_queued=stops_queued,
        failed_closed=failed_closed,
    )


__all__ = [
    "AdSpendSweepResult",
    "evaluate_runtime_spend_guard",
    "fresh_provider_snapshot",
    "production_pre_mutation_guard",
    "provider_report_date",
    "sweep_active_ad_spend_authorizations",
]
