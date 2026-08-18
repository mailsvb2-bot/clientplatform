from __future__ import annotations

"""Safe spend preparation for the goal-first owner autopilot.

This module keeps the owner on an outcome-first path. It can read fresh provider
budget evidence before a Yandex DRAFT exists so the UI can show the exact bounded
spend cap on the one explicit launch button. The final click still creates the
canonical authorization from fresh provider evidence and an immutable receipt.
"""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from clientplatform.application import ad_spend as spend
from clientplatform.application.ad_spend import PreparedAdSpendAuthorization
from clientplatform.application.ad_spend_consent import request_ad_spend_consent
from clientplatform.application.ad_spend_runtime import provider_report_date
from clientplatform.domain.ad_connections import AdProvider
from clientplatform.domain.ad_spend import AdSpendAuthorization, AdSpendInvariantViolation
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.ad_connection_repository import AdConnectionRepository
from clientplatform.infrastructure.ad_spend_preparation_repository import (
    AdSpendPreparationRepository,
)
from clientplatform.infrastructure.ad_worker_store import AdWorkerStore
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from clientplatform.integrations.yandex_direct import YandexDirectError, YandexTokenBundle
from clientplatform.integrations.yandex_direct_budget import (
    YandexCampaignBudgetReadout,
    YandexDailySpendReadout,
    managed_weekly_spend_limit_micros,
    reconcile_yandex_budget_snapshot,
)
from services.db import get_db_ro


_DEFAULT_MAX_SPEND_MINOR = 10_000
_DEFAULT_TTL_SECONDS = 240
_MICROS_PER_MINOR = 10_000


@dataclass(frozen=True, slots=True)
class GoalSpendPreview:
    connection_id: str
    external_campaign_id: str
    currency: str
    recommended_hard_cap_minor: int
    recommended_daily_cap_minor: int
    available_budget_minor: int
    spent_today_minor: int


@dataclass(frozen=True, slots=True)
class GoalSpendPreparation:
    authorization: AdSpendAuthorization
    prepared: PreparedAdSpendAuthorization
    recommended_hard_cap_minor: int
    recommended_daily_cap_minor: int


def _configured_minor(name: str, default: int) -> int:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer number of minor currency units") from exc
    if value <= 0 or value > 9_000_000_000_000_000:
        raise RuntimeError(f"{name} is outside the supported range")
    return value


def _minimum_weekly_minor(minimum_weekly_spend_micros: int) -> int:
    if isinstance(minimum_weekly_spend_micros, (bool, float)):
        raise AdSpendInvariantViolation("Yandex minimum weekly budget is invalid")
    try:
        micros = int(minimum_weekly_spend_micros)
    except (TypeError, ValueError) as exc:
        raise AdSpendInvariantViolation("Yandex minimum weekly budget is invalid") from exc
    if micros <= 0:
        raise AdSpendInvariantViolation("Yandex minimum weekly budget is invalid")
    minor, remainder = divmod(micros, _MICROS_PER_MINOR)
    if remainder or minor <= 0:
        raise AdSpendInvariantViolation(
            "Yandex minimum weekly budget cannot be represented in minor currency units"
        )
    return minor


def _caps(
    available_budget_minor: int,
    *,
    minimum_weekly_spend_micros: int,
) -> tuple[int, int]:
    minimum_weekly_minor = _minimum_weekly_minor(minimum_weekly_spend_micros)
    hard_raw = str(os.getenv("CLIENTPLATFORM_GOAL_MAX_SPEND_MINOR") or "").strip()
    configured_hard = _configured_minor(
        "CLIENTPLATFORM_GOAL_MAX_SPEND_MINOR",
        _DEFAULT_MAX_SPEND_MINOR,
    )
    if hard_raw and configured_hard < minimum_weekly_minor:
        raise AdSpendInvariantViolation(
            "configured goal spend maximum is below Yandex minimum weekly budget"
        )
    # When no operator maximum is configured, the provider's published minimum
    # is a floor for the recommendation shown to the owner, not a hidden spend.
    # The owner still sees and explicitly approves this exact cap.
    recommended_hard = (
        configured_hard
        if hard_raw
        else max(configured_hard, minimum_weekly_minor)
    )
    hard_cap = min(recommended_hard, int(available_budget_minor))
    if hard_cap < minimum_weekly_minor:
        raise AdSpendInvariantViolation(
            "available Yandex budget is below the minimum weekly budget"
        )

    daily_raw = str(os.getenv("CLIENTPLATFORM_GOAL_DAILY_SPEND_MINOR") or "").strip()
    configured_daily = _configured_minor(
        "CLIENTPLATFORM_GOAL_DAILY_SPEND_MINOR",
        hard_cap,
    )
    daily_cap = min(configured_daily, hard_cap)
    if daily_cap <= 0:
        raise AdSpendInvariantViolation("goal-first spend cap is unavailable")
    target_weekly_micros = managed_weekly_spend_limit_micros(
        hard_cap_minor=hard_cap,
        daily_cap_minor=daily_cap,
    )
    if target_weekly_micros < minimum_weekly_spend_micros:
        reason = (
            "configured goal daily spend maximum is below Yandex minimum weekly budget"
            if daily_raw
            else "goal-first spend cap is below Yandex minimum weekly budget"
        )
        raise AdSpendInvariantViolation(reason)
    return hard_cap, daily_cap


def _read_goal_evidence(
    *,
    actor: TenantContext,
    publication_job_id: str,
    now: datetime,
) -> tuple[YandexCampaignBudgetReadout, YandexDailySpendReadout, str, int]:
    selected_vault = spend._vault()
    selected_provider = spend._provider()
    report_date = provider_report_date(now=now)

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
            "advertising account changed during goal preparation"
        )

    def read(active_bundle: YandexTokenBundle):
        campaign, daily_spend = spend._read_provider_evidence(
            provider=selected_provider,
            target=target,
            bundle=active_bundle,
            report_date=report_date,
            captured_at=now,
        )
        minimum_weekly = selected_provider.minimum_weekly_spend_limit_micros(
            access_token=active_bundle.access_token,
            currency=campaign.currency,
            client_login=target.external_login,
        )
        return campaign, daily_spend, minimum_weekly

    bundle = YandexTokenBundle.from_json(token_json)
    try:
        campaign, daily_spend, minimum_weekly = read(bundle)
    except YandexDirectError as exc:
        if exc.code not in spend._AUTH_ERRORS or not bundle.refresh_token:
            raise
        refreshed = spend._refresh_bundle(
            provider=selected_provider,
            vault=selected_vault,
            connection=connection,
            bundle=bundle,
            now=now,
        )
        campaign, daily_spend, minimum_weekly = read(refreshed)
    return campaign, daily_spend, report_date, minimum_weekly


def preview_goal_spend(
    *,
    actor: TenantContext,
    connection_id: str,
    external_campaign_id: str,
    now: datetime | None = None,
) -> GoalSpendPreview:
    """Read the exact campaign budget before the owner sees the launch button.

    This performs no Yandex mutation. It exists so the default UX does not need
    a non-money "continue" click merely to learn the safe spend cap.
    """

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(
        microsecond=0
    )
    selected_vault = spend._vault()
    selected_provider = spend._provider()
    with get_db_ro() as conn:
        current_actor = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current_actor.assert_can_manage_promotions()
        connection = AdConnectionRepository(conn, vault=selected_vault).get_connection(
            actor=current_actor,
            connection_id=connection_id,
        )
        connection, token_json = AdWorkerStore(
            conn,
            vault=selected_vault,
        ).load_active(
            business_id=current_actor.business_id,
            connection_id=connection.id,
        )
    if connection.provider != AdProvider.YANDEX_DIRECT:
        raise AdSpendInvariantViolation("advertising provider is unsupported")
    bundle = YandexTokenBundle.from_json(token_json)
    report_date = provider_report_date(now=current)

    def read(active_bundle: YandexTokenBundle):
        campaign = selected_provider.campaign_budget_readout(
            access_token=active_bundle.access_token,
            external_campaign_id=external_campaign_id,
            captured_at=current,
            client_login=connection.external_login,
        )
        daily = selected_provider.daily_spend_readout(
            access_token=active_bundle.access_token,
            campaign=campaign,
            report_date=report_date,
            captured_at=current,
            client_login=connection.external_login,
        )
        minimum_weekly = selected_provider.minimum_weekly_spend_limit_micros(
            access_token=active_bundle.access_token,
            currency=campaign.currency,
            client_login=connection.external_login,
        )
        return campaign, daily, minimum_weekly

    try:
        campaign, daily_spend, minimum_weekly = read(bundle)
    except YandexDirectError as exc:
        if exc.code not in spend._AUTH_ERRORS or not bundle.refresh_token:
            raise
        bundle = spend._refresh_bundle(
            provider=selected_provider,
            vault=selected_vault,
            connection=connection,
            bundle=bundle,
            now=current,
        )
        campaign, daily_spend, minimum_weekly = read(bundle)

    snapshot = reconcile_yandex_budget_snapshot(
        connection_id=connection.id,
        external_account_id=connection.external_account_id,
        campaign=campaign,
        daily_spend=daily_spend,
        expected_report_date=report_date,
        now=current,
        provider_timezone=spend._report_timezone(),
        validity_seconds=_DEFAULT_TTL_SECONDS,
    )
    if not snapshot.launch_eligible or snapshot.available_budget_minor <= 0:
        raise AdSpendInvariantViolation(
            "provider evidence is not eligible for goal-first advertising spend"
        )
    hard_cap, daily_cap = _caps(
        snapshot.available_budget_minor,
        minimum_weekly_spend_micros=minimum_weekly,
    )
    return GoalSpendPreview(
        connection_id=connection.id,
        external_campaign_id=snapshot.external_campaign_id,
        currency=snapshot.currency,
        recommended_hard_cap_minor=hard_cap,
        recommended_daily_cap_minor=daily_cap,
        available_budget_minor=snapshot.available_budget_minor,
        spent_today_minor=snapshot.spent_today_minor,
    )


def prepare_goal_spend_consent(
    *,
    actor: TenantContext,
    publication_job_id: str,
    now: datetime | None = None,
) -> GoalSpendPreparation:
    """Prepare one exact, bounded spend authorization from fresh evidence."""

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(
        microsecond=0
    )
    campaign, daily_spend, report_date, minimum_weekly = _read_goal_evidence(
        actor=actor,
        publication_job_id=publication_job_id,
        now=current,
    )

    with get_db_ro() as conn:
        _current, target = AdSpendPreparationRepository(conn).load_submitted_target(
            actor=actor,
            publication_job_id=publication_job_id,
        )
    snapshot = reconcile_yandex_budget_snapshot(
        connection_id=target.connection_id,
        external_account_id=target.external_account_id,
        campaign=campaign,
        daily_spend=daily_spend,
        expected_report_date=report_date,
        now=current,
        provider_timezone=spend._report_timezone(),
        validity_seconds=_DEFAULT_TTL_SECONDS,
    )
    if not snapshot.launch_eligible or snapshot.available_budget_minor <= 0:
        raise AdSpendInvariantViolation(
            "provider evidence is not eligible for goal-first advertising spend"
        )

    hard_cap, daily_cap = _caps(
        snapshot.available_budget_minor,
        minimum_weekly_spend_micros=minimum_weekly,
    )
    prepared = spend.prepare_ad_spend_authorization(
        actor=actor,
        publication_job_id=publication_job_id,
        hard_cap_minor=hard_cap,
        daily_cap_minor=daily_cap,
        authorization_expires_at=current + timedelta(seconds=_DEFAULT_TTL_SECONDS),
        provider_report_date=report_date,
        now=current,
    )
    authorization = request_ad_spend_consent(
        actor=actor,
        authorization_id=prepared.authorization.id,
        now=current,
    )
    return GoalSpendPreparation(
        authorization=authorization,
        prepared=prepared,
        recommended_hard_cap_minor=hard_cap,
        recommended_daily_cap_minor=daily_cap,
    )


__all__ = [
    "GoalSpendPreparation",
    "GoalSpendPreview",
    "prepare_goal_spend_consent",
    "preview_goal_spend",
]
