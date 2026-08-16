from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from clientplatform.application.yandex_growth_analytics import (
    _AUTH_ERRORS,
    _current_period,
    _provider,
    _refresh_bundle,
    _vault,
)
from clientplatform.domain.ad_connections import normalize_external_campaign_id
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.ad_credential_vault import AdCredentialVault
from clientplatform.infrastructure.ad_worker_store import AdWorkerStore
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from clientplatform.integrations.yandex_direct import YandexDirectError, YandexTokenBundle
from clientplatform.integrations.yandex_direct_analytics import (
    ReadOnlyYandexDirectAnalyticsProvider,
    YandexCampaignPerformanceRow,
)
from services.db import get_db_ro

_REPORT_CAMPAIGN_BATCH_SIZE = 500
_VISIBLE_JOB_STATUSES = frozenset(
    {"draft", "queued", "publishing", "retry", "submitted", "failed"}
)


@dataclass(frozen=True, slots=True)
class _TrackedCampaign:
    connection_id: str
    external_login: str
    campaign_id: str
    campaign_name: str


@dataclass(frozen=True, slots=True)
class YandexCampaignDiagnosticsRow:
    connection_id: str
    campaign_id: str
    campaign_name: str
    impressions: int
    clicks: int
    cost_micros: int
    has_provider_row: bool

    @property
    def ctr_percent(self) -> float:
        if self.impressions <= 0:
            return 0.0
        return round((self.clicks / self.impressions) * 100.0, 1)

    @property
    def cpc_micros(self) -> int | None:
        if self.clicks <= 0:
            return None
        return self.cost_micros // self.clicks


@dataclass(frozen=True, slots=True)
class YandexCampaignDiagnosticsSnapshot:
    """Provider campaign diagnostics only; never business/revenue attribution."""

    date_from: str
    date_to: str
    period_days: int
    connected_accounts: int
    managed_campaigns: int
    impressions: int
    clicks: int
    cost_micros: int | None
    campaigns: tuple[YandexCampaignDiagnosticsRow, ...]

    @property
    def ctr_percent(self) -> float:
        if self.impressions <= 0:
            return 0.0
        return round((self.clicks / self.impressions) * 100.0, 1)

    @property
    def cpc_micros(self) -> int | None:
        if self.cost_micros is None or self.clicks <= 0:
            return None
        return self.cost_micros // self.clicks


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _clean_name(value: object) -> str:
    normalized = " ".join(str(value or "").replace("\x00", " ").split())
    return normalized[:255] or "Без названия"


def _load_managed_campaigns(
    *,
    actor: TenantContext,
) -> tuple[TenantContext, int, list[_TrackedCampaign]]:
    """Load only campaigns already referenced by this tenant's own ad jobs.

    CampaignId is persisted before AdId exists, so this deliberately does not
    require a submitted ad. Cancelled drafts are excluded from the active
    diagnostics surface, while failed/retry jobs stay visible because their
    provider campaign can still exist and accumulate delivery independently.
    """

    with get_db_ro() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_view_promotion_analytics()
        count_row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM ad_connections
            WHERE business_id=? AND provider='yandex_direct' AND status='active'
            """,
            (current.business_id,),
        ).fetchone()
        connected_accounts = int(_value(count_row, "c", 0) or 0) if count_row else 0
        rows = conn.execute(
            """
            SELECT j.connection_id, c.external_login,
                   j.external_campaign_id, j.external_campaign_name, j.status
            FROM ad_publication_jobs j
            JOIN ad_connections c
              ON c.id=j.connection_id AND c.business_id=j.business_id
            WHERE j.business_id=?
              AND c.provider='yandex_direct'
              AND c.status='active'
              AND j.external_campaign_id IS NOT NULL
              AND TRIM(j.external_campaign_id)<>''
            ORDER BY j.updated_at DESC, j.created_at DESC, j.id DESC
            """,
            (current.business_id,),
        ).fetchall()

    tracked_by_key: dict[tuple[str, str], _TrackedCampaign] = {}
    for row in rows:
        status = str(_value(row, "status", 4) or "").strip().lower()
        if status not in _VISIBLE_JOB_STATUSES:
            continue
        connection_id = str(_value(row, "connection_id", 0))
        campaign_id = normalize_external_campaign_id(
            _value(row, "external_campaign_id", 2)
        )
        key = (connection_id, campaign_id)
        tracked_by_key.setdefault(
            key,
            _TrackedCampaign(
                connection_id=connection_id,
                external_login=str(_value(row, "external_login", 1)),
                campaign_id=campaign_id,
                campaign_name=_clean_name(_value(row, "external_campaign_name", 3)),
            ),
        )
    tracked = [
        tracked_by_key[key]
        for key in sorted(tracked_by_key, key=lambda item: (item[0], int(item[1])))
    ]
    return current, connected_accounts, tracked


def _campaign_batches(campaign_ids: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    return tuple(
        campaign_ids[offset : offset + _REPORT_CAMPAIGN_BATCH_SIZE]
        for offset in range(0, len(campaign_ids), _REPORT_CAMPAIGN_BATCH_SIZE)
    )


def _provider_rows(
    *,
    current: TenantContext,
    tracked: list[_TrackedCampaign],
    date_from: str,
    date_to: str,
    vault: AdCredentialVault,
    provider: ReadOnlyYandexDirectAnalyticsProvider,
) -> dict[tuple[str, str], YandexCampaignPerformanceRow]:
    by_connection: dict[str, list[_TrackedCampaign]] = defaultdict(list)
    for item in tracked:
        by_connection[item.connection_id].append(item)

    rows: dict[tuple[str, str], YandexCampaignPerformanceRow] = {}
    for connection_id, items in by_connection.items():
        with get_db_ro() as conn:
            connection, token_json = AdWorkerStore(conn, vault=vault).load_active(
                business_id=current.business_id,
                connection_id=connection_id,
            )
        bundle = YandexTokenBundle.from_json(token_json)
        campaign_ids = tuple(sorted({item.campaign_id for item in items}, key=int))
        for campaign_batch in _campaign_batches(campaign_ids):
            try:
                report = provider.campaign_performance_report(
                    access_token=bundle.access_token,
                    campaign_ids=campaign_batch,
                    date_from=date_from,
                    date_to=date_to,
                    client_login=items[0].external_login,
                )
            except YandexDirectError as exc:
                if exc.code not in _AUTH_ERRORS or not bundle.refresh_token:
                    raise
                bundle = _refresh_bundle(
                    provider=provider,
                    vault=vault,
                    connection=connection,
                    bundle=bundle,
                )
                report = provider.campaign_performance_report(
                    access_token=bundle.access_token,
                    campaign_ids=campaign_batch,
                    date_from=date_from,
                    date_to=date_to,
                    client_login=items[0].external_login,
                )
            expected = set(campaign_batch)
            for row in report.rows:
                if row.campaign_id not in expected:
                    raise YandexDirectError("analytics_report_campaign_mismatch")
                key = (connection_id, row.campaign_id)
                if key in rows:
                    raise YandexDirectError("analytics_report_campaign_duplicate")
                rows[key] = row
    return rows


def get_yandex_campaign_diagnostics(
    *,
    actor: TenantContext,
    period_days: int = 30,
    now: datetime | date | None = None,
    vault: AdCredentialVault | None = None,
    provider: ReadOnlyYandexDirectAnalyticsProvider | None = None,
) -> YandexCampaignDiagnosticsSnapshot:
    """Read campaign-level Yandex metrics without inventing business attribution."""

    date_from, date_to = _current_period(period_days, now=now)
    current, connected_accounts, tracked = _load_managed_campaigns(actor=actor)
    if not tracked:
        return YandexCampaignDiagnosticsSnapshot(
            date_from=date_from,
            date_to=date_to,
            period_days=int(period_days),
            connected_accounts=connected_accounts,
            managed_campaigns=0,
            impressions=0,
            clicks=0,
            cost_micros=0,
            campaigns=(),
        )

    selected_vault = vault or _vault()
    selected_provider = provider or _provider()
    report_rows = _provider_rows(
        current=current,
        tracked=tracked,
        date_from=date_from,
        date_to=date_to,
        vault=selected_vault,
        provider=selected_provider,
    )

    campaigns: list[YandexCampaignDiagnosticsRow] = []
    for item in tracked:
        provider_row = report_rows.get((item.connection_id, item.campaign_id))
        campaigns.append(
            YandexCampaignDiagnosticsRow(
                connection_id=item.connection_id,
                campaign_id=item.campaign_id,
                campaign_name=(
                    provider_row.campaign_name if provider_row is not None else item.campaign_name
                ),
                impressions=provider_row.impressions if provider_row is not None else 0,
                clicks=provider_row.clicks if provider_row is not None else 0,
                cost_micros=provider_row.cost_micros if provider_row is not None else 0,
                has_provider_row=provider_row is not None,
            )
        )

    # Keep the exact same money safety as the existing diagnostics: Yandex
    # report micros are meaningful inside one account, but the connection model
    # does not persist a proven currency identity. Never add money across two
    # account connections even when the numeric micros happen to look compatible.
    connection_ids = {item.connection_id for item in tracked}
    aggregate_cost = (
        sum(item.cost_micros for item in campaigns)
        if len(connection_ids) <= 1
        else None
    )
    return YandexCampaignDiagnosticsSnapshot(
        date_from=date_from,
        date_to=date_to,
        period_days=int(period_days),
        connected_accounts=connected_accounts,
        managed_campaigns=len(campaigns),
        impressions=sum(item.impressions for item in campaigns),
        clicks=sum(item.clicks for item in campaigns),
        cost_micros=aggregate_cost,
        campaigns=tuple(campaigns),
    )


__all__ = [
    "YandexCampaignDiagnosticsRow",
    "YandexCampaignDiagnosticsSnapshot",
    "get_yandex_campaign_diagnostics",
]
