from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clientplatform.application.promotion_attribution import (
    PromotionAttribution,
    load_promotion_attribution,
    promotion_event_window,
)
from clientplatform.domain.ad_connections import AdConnection
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.ad_credential_vault import (
    AdCredentialVault,
    AgeAdCredentialVault,
)
from clientplatform.infrastructure.ad_worker_store import AdWorkerStore
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from clientplatform.integrations.yandex_direct import (
    YandexDirectError,
    YandexOAuthConfig,
    YandexTokenBundle,
)
from clientplatform.integrations.yandex_direct_analytics import (
    ReadOnlyYandexDirectAnalyticsProvider,
    YandexAdPerformanceRow,
)
from services.db import get_db, get_db_ro

_LocalAttribution = PromotionAttribution

_AUTH_ERRORS = frozenset(
    {
        "provider_http_401",
        "provider_53",
        "provider_54",
        "provider_55",
        "provider_56",
        "provider_invalid_token",
        "provider_unauthorized",
        "oauth_refresh_token_missing",
    }
)
_ALLOWED_PERIOD_DAYS = frozenset({7, 30})
_REPORT_AD_BATCH_SIZE = 500


@dataclass(frozen=True, slots=True)
class _TrackedAd:
    connection_id: str
    external_login: str
    promotion_campaign_id: str
    external_campaign_id: str
    external_campaign_name: str
    external_ad_id: str


@dataclass(frozen=True, slots=True)
class YandexGrowthCampaignSnapshot:
    connection_id: str
    campaign_id: str
    campaign_name: str
    tracked_ads: int
    impressions: int
    clicks: int
    cost_micros: int
    leads: int
    bookings: int
    won: int

    @property
    def ctr_percent(self) -> float:
        return _percent(self.clicks, self.impressions)

    @property
    def cpc_micros(self) -> int | None:
        return _unit_cost(self.cost_micros, self.clicks)

    @property
    def cpl_micros(self) -> int | None:
        return _unit_cost(self.cost_micros, self.leads)

    @property
    def booking_cost_micros(self) -> int | None:
        return _unit_cost(self.cost_micros, self.bookings)

    @property
    def cac_micros(self) -> int | None:
        return _unit_cost(self.cost_micros, self.won)


@dataclass(frozen=True, slots=True)
class YandexGrowthSnapshot:
    date_from: str
    date_to: str
    period_days: int
    connected_accounts: int
    tracked_ads: int
    impressions: int
    clicks: int
    cost_micros: int | None
    leads: int
    bookings: int
    won: int
    campaigns: tuple[YandexGrowthCampaignSnapshot, ...]

    @property
    def ctr_percent(self) -> float:
        return _percent(self.clicks, self.impressions)

    @property
    def cpc_micros(self) -> int | None:
        return _unit_cost(self.cost_micros, self.clicks)

    @property
    def cpl_micros(self) -> int | None:
        return _unit_cost(self.cost_micros, self.leads)

    @property
    def booking_cost_micros(self) -> int | None:
        return _unit_cost(self.cost_micros, self.bookings)

    @property
    def cac_micros(self) -> int | None:
        return _unit_cost(self.cost_micros, self.won)


def _percent(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((int(numerator) / int(denominator)) * 100.0, 1)


def _unit_cost(cost_micros: int | None, outcomes: int) -> int | None:
    if cost_micros is None or outcomes <= 0:
        return None
    return int(cost_micros) // int(outcomes)


def _report_zone() -> ZoneInfo:
    zone_name = (
        os.getenv("CLIENTPLATFORM_YANDEX_DIRECT_REPORT_TIMEZONE") or "Europe/Moscow"
    ).strip()
    try:
        return ZoneInfo(zone_name)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError("Yandex Direct report timezone is invalid") from exc


def _current_period(days: int, now: datetime | date | None = None) -> tuple[str, str]:
    if int(days) not in _ALLOWED_PERIOD_DAYS:
        raise ValueError("Yandex analytics period must be 7 or 30 days")
    zone = _report_zone()
    if isinstance(now, datetime):
        if now.tzinfo is not None and now.utcoffset() is not None:
            end = now.astimezone(zone).date()
        else:
            end = now.date()
    elif isinstance(now, date):
        end = now
    else:
        end = datetime.now(zone).date()
    start = end - timedelta(days=int(days) - 1)
    return start.isoformat(), end.isoformat()


def _event_window(date_from: str, date_to: str) -> tuple[str, str]:
    return promotion_event_window(date_from, date_to, zone=_report_zone())


def _provider() -> ReadOnlyYandexDirectAnalyticsProvider:
    enabled = (os.getenv("CLIENTPLATFORM_AD_CONNECTIONS_ENABLED") or "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        raise RuntimeError("advertising account connections are disabled")
    client_id = (os.getenv("CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_ID") or "").strip()
    if not client_id:
        raise RuntimeError("Yandex Direct OAuth application is not configured")
    redirect_uri = (os.getenv("CLIENTPLATFORM_AD_OAUTH_REDIRECT_URI") or "").strip()
    if not redirect_uri:
        domain = (os.getenv("CLIENTPLATFORM_DOMAIN") or "").strip()
        if not domain:
            raise RuntimeError("CLIENTPLATFORM_DOMAIN is required for advertising OAuth")
        redirect_uri = f"https://{domain}/oauth/yandex-direct/callback"
    return ReadOnlyYandexDirectAnalyticsProvider(
        oauth=YandexOAuthConfig(
            client_id=client_id,
            client_secret=(
                os.getenv("CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_SECRET") or ""
            ).strip(),
            redirect_uri=redirect_uri,
        )
    )


def _vault() -> AdCredentialVault:
    return AgeAdCredentialVault()


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _load_tracked_ads(
    *, actor: TenantContext
) -> tuple[TenantContext, int, list[_TrackedAd]]:
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
            SELECT j.connection_id, c.external_login, j.promotion_campaign_id,
                   j.external_campaign_id, j.external_campaign_name, j.external_ad_id
            FROM ad_publication_jobs j
            JOIN ad_connections c
              ON c.id=j.connection_id AND c.business_id=j.business_id
            WHERE j.business_id=?
              AND c.provider='yandex_direct'
              AND c.status='active'
              AND j.status='submitted'
              AND j.external_ad_id IS NOT NULL
              AND j.external_ad_id<>''
            ORDER BY j.created_at, j.id
            """,
            (current.business_id,),
        ).fetchall()
    tracked = [
        _TrackedAd(
            connection_id=str(_value(row, "connection_id", 0)),
            external_login=str(_value(row, "external_login", 1)),
            promotion_campaign_id=str(_value(row, "promotion_campaign_id", 2)),
            external_campaign_id=str(_value(row, "external_campaign_id", 3)),
            external_campaign_name=str(_value(row, "external_campaign_name", 4)),
            external_ad_id=str(_value(row, "external_ad_id", 5)),
        )
        for row in rows
    ]
    return current, connected_accounts, tracked


def _load_local_attribution(
    *,
    actor: TenantContext,
    promotion_campaign_ids: set[str],
    date_from: str,
    date_to: str,
) -> PromotionAttribution:
    if not promotion_campaign_ids:
        return PromotionAttribution(leads={}, bookings={}, won={})
    event_from, event_until = _event_window(date_from, date_to)
    with get_db_ro() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_view_promotion_analytics()
        return load_promotion_attribution(
            conn,
            business_id=current.business_id,
            promotion_campaign_ids=promotion_campaign_ids,
            event_from=event_from,
            event_until=event_until,
        )


def _refresh_bundle(
    *,
    provider: ReadOnlyYandexDirectAnalyticsProvider,
    vault: AdCredentialVault,
    connection: AdConnection,
    bundle: YandexTokenBundle,
) -> YandexTokenBundle:
    refreshed = provider.refresh(bundle=bundle)
    with get_db() as conn:
        AdWorkerStore(conn, vault=vault).replace_token_bundle(
            connection=connection,
            token_bundle_json=refreshed.to_json(),
        )
    return refreshed


def _ad_id_batches(ad_ids: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    return tuple(
        ad_ids[offset : offset + _REPORT_AD_BATCH_SIZE]
        for offset in range(0, len(ad_ids), _REPORT_AD_BATCH_SIZE)
    )


def _provider_rows(
    *,
    current: TenantContext,
    tracked: list[_TrackedAd],
    date_from: str,
    date_to: str,
    vault: AdCredentialVault,
    provider: ReadOnlyYandexDirectAnalyticsProvider,
) -> dict[tuple[str, str], YandexAdPerformanceRow]:
    by_connection: dict[str, list[_TrackedAd]] = defaultdict(list)
    for item in tracked:
        by_connection[item.connection_id].append(item)
    rows: dict[tuple[str, str], YandexAdPerformanceRow] = {}
    for connection_id, items in by_connection.items():
        with get_db_ro() as conn:
            connection, token_json = AdWorkerStore(conn, vault=vault).load_active(
                business_id=current.business_id,
                connection_id=connection_id,
            )
        bundle = YandexTokenBundle.from_json(token_json)
        ad_ids = tuple(sorted({item.external_ad_id for item in items}, key=int))
        expected_campaign_by_ad = {
            item.external_ad_id: item.external_campaign_id for item in items
        }
        for ad_batch in _ad_id_batches(ad_ids):
            try:
                report = provider.performance_report(
                    access_token=bundle.access_token,
                    ad_ids=ad_batch,
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
                report = provider.performance_report(
                    access_token=bundle.access_token,
                    ad_ids=ad_batch,
                    date_from=date_from,
                    date_to=date_to,
                    client_login=items[0].external_login,
                )
            for row in report.rows:
                expected_campaign = expected_campaign_by_ad.get(row.ad_id)
                if expected_campaign is None or row.campaign_id != expected_campaign:
                    raise YandexDirectError("analytics_report_campaign_mismatch")
                rows[(connection_id, row.ad_id)] = row
    return rows


def get_yandex_growth_snapshot(
    *,
    actor: TenantContext,
    period_days: int = 30,
    now: datetime | date | None = None,
    vault: AdCredentialVault | None = None,
    provider: ReadOnlyYandexDirectAnalyticsProvider | None = None,
) -> YandexGrowthSnapshot:
    date_from, date_to = _current_period(period_days, now=now)
    current, connected_accounts, tracked = _load_tracked_ads(actor=actor)
    if not tracked:
        return YandexGrowthSnapshot(
            date_from=date_from,
            date_to=date_to,
            period_days=int(period_days),
            connected_accounts=connected_accounts,
            tracked_ads=0,
            impressions=0,
            clicks=0,
            cost_micros=0,
            leads=0,
            bookings=0,
            won=0,
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
    attribution = _load_local_attribution(
        actor=current,
        promotion_campaign_ids={item.promotion_campaign_id for item in tracked},
        date_from=date_from,
        date_to=date_to,
    )

    campaign_targets: dict[tuple[str, str], list[_TrackedAd]] = defaultdict(list)
    for item in tracked:
        campaign_targets[(item.connection_id, item.external_campaign_id)].append(item)

    campaigns: list[YandexGrowthCampaignSnapshot] = []
    all_leads: set[str] = set()
    all_bookings: set[str] = set()
    all_won: set[str] = set()
    for (connection_id, campaign_id), items in sorted(
        campaign_targets.items(),
        key=lambda pair: (pair[0][0], int(pair[0][1])),
    ):
        unique_ad_ids = {item.external_ad_id for item in items}
        provider_items = [
            report_rows[(connection_id, ad_id)]
            for ad_id in unique_ad_ids
            if (connection_id, ad_id) in report_rows
        ]
        promotion_ids = {item.promotion_campaign_id for item in items}
        leads = set().union(
            *(attribution.leads.get(item, frozenset()) for item in promotion_ids)
        )
        bookings = set().union(
            *(attribution.bookings.get(item, frozenset()) for item in promotion_ids)
        )
        won = set().union(
            *(attribution.won.get(item, frozenset()) for item in promotion_ids)
        )
        all_leads.update(leads)
        all_bookings.update(bookings)
        all_won.update(won)
        provider_name = next(
            (row.campaign_name for row in provider_items if row.campaign_name),
            items[0].external_campaign_name,
        )
        campaigns.append(
            YandexGrowthCampaignSnapshot(
                connection_id=connection_id,
                campaign_id=campaign_id,
                campaign_name=provider_name,
                tracked_ads=len(unique_ad_ids),
                impressions=sum(row.impressions for row in provider_items),
                clicks=sum(row.clicks for row in provider_items),
                cost_micros=sum(row.cost_micros for row in provider_items),
                leads=len(leads),
                bookings=len(bookings),
                won=len(won),
            )
        )

    unique_report_rows = list(report_rows.values())
    tracked_connections = {item.connection_id for item in tracked}
    # Ad connections currently do not persist a trustworthy currency identity.
    # Never add monetary micros or derive CPC/CPL/CAC across multiple accounts:
    # they may be RUB, KZT or another currency. Non-monetary counts remain safe
    # to aggregate, while per-campaign costs stay scoped to one connection.
    aggregate_cost_micros = (
        sum(row.cost_micros for row in unique_report_rows)
        if len(tracked_connections) == 1
        else None
    )
    return YandexGrowthSnapshot(
        date_from=date_from,
        date_to=date_to,
        period_days=int(period_days),
        connected_accounts=connected_accounts,
        tracked_ads=len({(item.connection_id, item.external_ad_id) for item in tracked}),
        impressions=sum(row.impressions for row in unique_report_rows),
        clicks=sum(row.clicks for row in unique_report_rows),
        cost_micros=aggregate_cost_micros,
        leads=len(all_leads),
        bookings=len(all_bookings),
        won=len(all_won),
        campaigns=tuple(
            sorted(
                campaigns,
                key=lambda item: (item.cost_micros, item.clicks, item.impressions),
                reverse=True,
            )
        ),
    )


__all__ = [
    "YandexGrowthCampaignSnapshot",
    "YandexGrowthSnapshot",
    "get_yandex_growth_snapshot",
]
