from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Iterable

from clientplatform.application.cockpit import resolve_cockpit_context
from clientplatform.application.growth_cockpit import get_growth_cockpit
from clientplatform.application.tenancy import resolve_tenant_context
from clientplatform.domain.money import settlement_currency_minor_unit_exponent
from clientplatform.domain.tenancy import TenantAccessDenied, TenantContext, TenantPermissionDenied

_SCHEMA_VERSION = "2026-09-07.v1"


@dataclass(frozen=True, slots=True)
class CockpitGrowthMetric:
    key: str
    value: int
    meaning: str


@dataclass(frozen=True, slots=True)
class CockpitGrowthMoney:
    currency: str
    amount_minor: int
    display: str


@dataclass(frozen=True, slots=True)
class CockpitGrowthSource:
    source: str
    label: str
    outcomes: int


@dataclass(frozen=True, slots=True)
class CockpitGrowthAction:
    title: str
    reason: str
    action_key: str


@dataclass(frozen=True, slots=True)
class CockpitAdvertisingSummary:
    connected_accounts: int
    tracked_ads: int
    impressions: int
    clicks: int
    leads: int
    bookings: int
    won: int
    ctr_percent: float


@dataclass(frozen=True, slots=True)
class CockpitJourneySummary:
    leads: int
    bookings: int
    completed_bookings: int
    paid_customers: int
    reactivated_customers: int
    verified_revenue: tuple[CockpitGrowthMoney, ...]
    attributed_revenue: tuple[CockpitGrowthMoney, ...]
    unattributed_revenue: tuple[CockpitGrowthMoney, ...]


@dataclass(frozen=True, slots=True)
class CockpitGrowthAnalyticsSnapshot:
    schema_version: str
    business_id: str
    business_name: str
    period_days: int
    period_from: str
    period_to: str
    metrics: tuple[CockpitGrowthMetric, ...]
    revenue: tuple[CockpitGrowthMoney, ...]
    sources: tuple[CockpitGrowthSource, ...]
    attention: tuple[str, ...]
    actions: tuple[CockpitGrowthAction, ...]
    advertising: CockpitAdvertisingSummary | None
    journey: CockpitJourneySummary
    limitations: tuple[str, ...]
    can_manage_promotions: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _money(amount_minor: int, currency: str) -> CockpitGrowthMoney:
    exponent = settlement_currency_minor_unit_exponent(currency)
    amount = Decimal(int(amount_minor)) / (Decimal(10) ** exponent)
    rendered = f"{amount:,.{exponent}f}" if exponent else f"{amount:,.0f}"
    return CockpitGrowthMoney(
        currency=str(currency).upper(),
        amount_minor=int(amount_minor),
        display=f"{rendered.replace(',', ' ')} {str(currency).upper()}",
    )


def _money_rows(values: Iterable[Any]) -> tuple[CockpitGrowthMoney, ...]:
    return tuple(
        _money(int(item.amount_minor), str(item.currency))
        for item in values
    )


def _current(actor: TenantContext) -> TenantContext:
    current = resolve_tenant_context(user_id=actor.user_id, business_id=actor.business_id)
    current.assert_can_view_promotion_analytics()
    return current


def build_cockpit_growth_analytics(
    *, actor: TenantContext, business_name: str, period_days: int = 7
) -> CockpitGrowthAnalyticsSnapshot:
    current = _current(actor)
    snapshot = get_growth_cockpit(actor=current, period_days=period_days)
    advertising = snapshot.advertising
    ad_summary = None
    if advertising is not None:
        ad_summary = CockpitAdvertisingSummary(
            connected_accounts=int(advertising.connected_accounts),
            tracked_ads=int(advertising.tracked_ads),
            impressions=int(advertising.impressions),
            clicks=int(advertising.clicks),
            leads=int(advertising.leads),
            bookings=int(advertising.bookings),
            won=int(advertising.won),
            ctr_percent=float(advertising.ctr_percent),
        )

    can_manage_promotions = True
    try:
        current.assert_can_manage_promotions()
    except TenantPermissionDenied:
        can_manage_promotions = False

    journey = snapshot.journey
    completed = int(journey.completed_bookings)
    if "booking_completion_unavailable" in tuple(journey.limitations):
        completed = -1

    return CockpitGrowthAnalyticsSnapshot(
        schema_version=_SCHEMA_VERSION,
        business_id=current.business_id,
        business_name=str(business_name),
        period_days=int(snapshot.period_days),
        period_from=snapshot.period_from.isoformat(),
        period_to=snapshot.period_to.isoformat(),
        metrics=tuple(
            CockpitGrowthMetric(key=item.key, value=int(item.value), meaning=item.meaning)
            for item in snapshot.period_metrics
        ),
        revenue=tuple(
            _money(int(item.amount_minor), str(item.currency)) for item in snapshot.revenue
        ),
        sources=tuple(
            CockpitGrowthSource(
                source=item.source,
                label=item.label,
                outcomes=int(item.outcomes),
            )
            for item in snapshot.what_worked
        ),
        attention=tuple(str(item) for item in snapshot.attention),
        actions=tuple(
            CockpitGrowthAction(
                title=item.title,
                reason=item.reason,
                action_key=item.action_key,
            )
            for item in snapshot.actions
        ),
        advertising=ad_summary,
        journey=CockpitJourneySummary(
            leads=int(journey.leads),
            bookings=int(journey.bookings),
            completed_bookings=completed,
            paid_customers=int(journey.paid_customers),
            reactivated_customers=int(journey.reactivated_customers),
            verified_revenue=_money_rows(journey.verified_revenue_by_currency),
            attributed_revenue=_money_rows(journey.attributed_revenue_by_currency),
            unattributed_revenue=_money_rows(journey.unattributed_revenue_by_currency),
        ),
        limitations=tuple(dict.fromkeys(str(item) for item in snapshot.limitations)),
        can_manage_promotions=can_manage_promotions,
    )


def _resolve_actor(*, telegram_user_id: int, requested_business_id: str | None) -> tuple[TenantContext, str]:
    context = resolve_cockpit_context(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    if context.onboarding_required or context.business_id is None or context.business_name is None:
        raise TenantAccessDenied("active business membership was not found")
    actor = resolve_tenant_context(user_id=context.user_id, business_id=context.business_id)
    return _current(actor), context.business_name


def resolve_cockpit_growth_analytics(
    *,
    telegram_user_id: int,
    requested_business_id: str | None = None,
    period_days: int = 7,
) -> CockpitGrowthAnalyticsSnapshot:
    actor, business_name = _resolve_actor(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    return build_cockpit_growth_analytics(
        actor=actor,
        business_name=business_name,
        period_days=period_days,
    )


__all__ = [
    "CockpitAdvertisingSummary",
    "CockpitGrowthAction",
    "CockpitGrowthAnalyticsSnapshot",
    "CockpitGrowthMetric",
    "CockpitGrowthMoney",
    "CockpitGrowthSource",
    "CockpitJourneySummary",
    "build_cockpit_growth_analytics",
    "resolve_cockpit_growth_analytics",
]
