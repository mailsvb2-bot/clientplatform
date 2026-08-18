from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clientplatform.application.activity import get_business_profile
from clientplatform.application.revenue_attribution import get_business_unit_economics
from clientplatform.application.sales_ui import (
    count_sales_handoff_work,
    list_sales_handoff_work,
    list_sales_work,
)
from clientplatform.application.yandex_growth_analytics import (
    YandexGrowthSnapshot,
    get_yandex_growth_snapshot,
)
from clientplatform.domain.revenue_attribution import UnitEconomicsSnapshot
from clientplatform.domain.tenancy import TenantContext

_ALLOWED_PERIODS = frozenset({7, 30})

_SOURCE_LABELS = {
    "direct": "Прямой источник",
    "organic": "Органический источник",
    "referral": "Рекомендации",
    "promotion": "Продвижение",
    "unknown": "Источник не определён",
    "yandex_direct": "Реклама",
    "telegram": "Telegram",
    "vk": "VK",
    "max": "MAX",
}


@dataclass(frozen=True, slots=True)
class GrowthMetric:
    key: str
    value: int
    source: str
    meaning: str


@dataclass(frozen=True, slots=True)
class GrowthMoney:
    amount_minor: int
    currency: str
    source: str
    meaning: str


@dataclass(frozen=True, slots=True)
class GrowthAction:
    title: str
    reason: str
    action_key: str
    source: str


@dataclass(frozen=True, slots=True)
class GrowthSourceResult:
    source: str
    outcomes: int
    label: str


@dataclass(frozen=True, slots=True)
class GrowthCockpitSnapshot:
    business_id: str
    timezone_name: str
    as_of: datetime
    period_days: int
    period_from: datetime
    period_to: datetime
    today_from: datetime
    today_to: datetime
    today_metrics: tuple[GrowthMetric, ...]
    period_metrics: tuple[GrowthMetric, ...]
    revenue: tuple[GrowthMoney, ...]
    needs_reply: int
    advertising: YandexGrowthSnapshot | None
    what_worked: tuple[GrowthSourceResult, ...]
    attention: tuple[str, ...]
    next_action: GrowthAction
    limitations: tuple[str, ...]


def _business_zone(actor: TenantContext) -> ZoneInfo:
    profile = get_business_profile(actor=actor)
    try:
        return ZoneInfo(profile.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("business timezone is invalid") from exc


def _window_for_days(*, zone: ZoneInfo, days: int, now: datetime | None) -> tuple[datetime, datetime]:
    if days not in _ALLOWED_PERIODS:
        raise ValueError("growth cockpit period must be 7 or 30 days")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("growth cockpit now must be timezone-aware")
    local_now = current.astimezone(zone)
    end_date = local_now.date()
    start_date = end_date - timedelta(days=days - 1)
    start = datetime.combine(start_date, time.min, tzinfo=zone).astimezone(timezone.utc)
    end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)
    return start, end


def _today_window(*, zone: ZoneInfo, now: datetime | None) -> tuple[datetime, datetime]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("growth cockpit now must be timezone-aware")
    local_date = current.astimezone(zone).date()
    start = datetime.combine(local_date, time.min, tzinfo=zone).astimezone(timezone.utc)
    end = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)
    return start, end


def _metrics(snapshot: UnitEconomicsSnapshot) -> tuple[GrowthMetric, ...]:
    return (
        GrowthMetric(
            key="leads",
            value=snapshot.leads,
            source="durable_outcome_ledger",
            meaning="Новые лиды, подтверждённые каноническими outcome-событиями.",
        ),
        GrowthMetric(
            key="qualified_leads",
            value=snapshot.qualified_leads,
            source="durable_outcome_ledger",
            meaning="Лиды, для которых подтверждена квалификация.",
        ),
        GrowthMetric(
            key="bookings",
            value=snapshot.bookings,
            source="durable_outcome_ledger",
            meaning="Созданные записи, подтверждённые каноническими outcome-событиями.",
        ),
        GrowthMetric(
            key="paid_customers",
            value=snapshot.paid_customers,
            source="durable_outcome_ledger",
            meaning="Уникальные клиенты с подтверждённой положительной оплатой.",
        ),
    )


def _revenue(snapshot: UnitEconomicsSnapshot) -> tuple[GrowthMoney, ...]:
    return tuple(
        GrowthMoney(
            amount_minor=item.amount_minor,
            currency=item.currency,
            source="revenue_attribution",
            meaning="Выручка из денежных outcome-событий с канонической attribution.",
        )
        for item in snapshot.revenue_by_currency
    )


def _what_worked(snapshot: UnitEconomicsSnapshot) -> tuple[GrowthSourceResult, ...]:
    rows = [
        GrowthSourceResult(
            source=source.value,
            outcomes=int(count),
            label=_SOURCE_LABELS.get(source.value, "Источник клиентов"),
        )
        for source, count in snapshot.source_breakdown.items()
        if int(count) > 0
    ]
    rows.sort(key=lambda item: (-item.outcomes, item.label, item.source))
    return tuple(rows)


def _attention(
    *,
    economics: UnitEconomicsSnapshot,
    needs_reply: int,
    advertising: YandexGrowthSnapshot | None,
    advertising_error: bool,
) -> tuple[str, ...]:
    items: list[str] = []
    if needs_reply > 0:
        items.append(f"{needs_reply} клиент(ов) требуют ответа или решения владельца.")
    if not economics.attribution_complete:
        items.append("Часть денежных результатов пока нельзя надёжно связать с источником клиента.")
    if len(economics.revenue_by_currency) > 1:
        items.append("Выручка есть в нескольких валютах; суммы не объединяются.")
    if advertising_error:
        items.append("Данные рекламы сейчас недоступны; бизнес-результаты показаны без них.")
    elif advertising is not None and advertising.connected_accounts > 0:
        items.append(
            "Стоимость рекламы не включается в денежные итоги Growth Cockpit: "
            "ISO-валюта рекламного подключения пока не подтверждена."
        )
    return tuple(items)


def _next_action(
    *,
    has_handoff: bool,
    sales_work: list[dict[str, object]],
    economics: UnitEconomicsSnapshot,
) -> GrowthAction:
    if has_handoff:
        return GrowthAction(
            title="Ответить клиентам, которым нужен человек",
            reason="Есть открытые обращения, переданные владельцу или сотруднику.",
            action_key="sales_handoff",
            source="sales_handoff_queue",
        )
    for item in sales_work:
        action_kind = str(item.get("next_action_kind") or "").strip()
        if action_kind:
            customer_name = str(item.get("customer_name") or "Клиент").strip()
            return GrowthAction(
                title=f"Продолжить работу с клиентом: {customer_name}",
                reason="Для клиента уже существует канонический следующий шаг продаж.",
                action_key=f"sales_plan:{item.get('next_plan_id') or ''}",
                source="sales_action_plan",
            )
    if economics.unattributed_monetary_outcomes > 0:
        return GrowthAction(
            title="Проверить источники оплат",
            reason="Есть денежные результаты без подтверждённой attribution.",
            action_key="attribution_review",
            source="revenue_attribution",
        )
    return GrowthAction(
        title="Ничего срочного",
        reason="Канонические источники не показывают обязательного действия владельца.",
        action_key="none",
        source="growth_cockpit_projection",
    )


def get_growth_cockpit(
    *,
    actor: TenantContext,
    period_days: int = 7,
    now: datetime | None = None,
    advertising_loader: Callable[..., YandexGrowthSnapshot] = get_yandex_growth_snapshot,
) -> GrowthCockpitSnapshot:
    """Build the owner growth view from existing canonical facts only.

    This projection deliberately owns no business facts and makes no provider
    mutations. Money remains currency-safe; provider cost never becomes
    business money until the provider source proves its ISO currency.
    """

    if int(period_days) not in _ALLOWED_PERIODS:
        raise ValueError("growth cockpit period must be 7 or 30 days")
    zone = _business_zone(actor)
    period_from, period_to = _window_for_days(zone=zone, days=int(period_days), now=now)
    today_from, today_to = _today_window(zone=zone, now=now)
    today = get_business_unit_economics(
        actor=actor,
        occurred_from=today_from,
        occurred_to=today_to,
    )
    period = get_business_unit_economics(
        actor=actor,
        occurred_from=period_from,
        occurred_to=period_to,
    )
    needs_reply = count_sales_handoff_work(actor=actor)
    handoffs = list_sales_handoff_work(actor=actor, limit=1) if needs_reply else []
    sales_work = list_sales_work(actor=actor, limit=50)

    advertising: YandexGrowthSnapshot | None = None
    advertising_error = False
    try:
        local_now: datetime | date | None = None if now is None else now.astimezone(zone)
        advertising = advertising_loader(
            actor=actor,
            period_days=int(period_days),
            now=local_now,
        )
    except (RuntimeError, ValueError, OSError):  # validator: allow-wide-except
        advertising_error = True

    limitations = list(period.limitations)
    if advertising_error:
        limitations.append("advertising_unavailable")
    elif advertising is not None and advertising.connected_accounts > 0:
        limitations.append("advertising_currency_unverified")

    return GrowthCockpitSnapshot(
        business_id=period.business_id,
        timezone_name=zone.key,
        as_of=(now or datetime.now(timezone.utc)).astimezone(timezone.utc),
        period_days=int(period_days),
        period_from=period_from,
        period_to=period_to,
        today_from=today_from,
        today_to=today_to,
        today_metrics=_metrics(today),
        period_metrics=_metrics(period),
        revenue=_revenue(period),
        needs_reply=needs_reply,
        advertising=advertising,
        what_worked=_what_worked(period),
        attention=_attention(
            economics=period,
            needs_reply=needs_reply,
            advertising=advertising,
            advertising_error=advertising_error,
        ),
        next_action=_next_action(
            has_handoff=bool(handoffs),
            sales_work=sales_work,
            economics=period,
        ),
        limitations=tuple(dict.fromkeys(limitations)),
    )


__all__ = [
    "GrowthAction",
    "GrowthCockpitSnapshot",
    "GrowthMetric",
    "GrowthMoney",
    "GrowthSourceResult",
    "get_growth_cockpit",
]
