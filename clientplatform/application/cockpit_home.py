from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clientplatform.application.activity import get_business_profile
from clientplatform.application.automation_policy import list_pending_automation_action_approvals
from clientplatform.application.bookings import list_booking_slots
from clientplatform.application.customer_activity import tenant_customer_activity
from clientplatform.application.growth_cockpit import (
    GrowthAction,
    GrowthCockpitSnapshot,
    get_growth_cockpit,
    project_sales_actions,
)
from clientplatform.application.sales_ui import list_sales_handoff_work, list_sales_work
from clientplatform.domain.money import settlement_currency_minor_unit_exponent
from clientplatform.domain.tenancy import TenantContext, TenantPermissionDenied

_HOME_SCHEMA_VERSION = 1
_HOME_ACTION_LIMIT = 5
_HOME_APPROVAL_LIMIT = 10

@dataclass(frozen=True, slots=True)
class CockpitHomeMetric:
    key: str
    title: str
    value: int
    detail: str


@dataclass(frozen=True, slots=True)
class CockpitHomeMoney:
    title: str
    amount_minor: int
    currency: str
    display_amount: str
    detail: str


@dataclass(frozen=True, slots=True)
class CockpitHomeAction:
    title: str
    reason: str
    route: str | None
    action_key: str

@dataclass(frozen=True, slots=True)
class CockpitHomeSource:
    id: str
    status: str
    message: str


@dataclass(frozen=True, slots=True)
class CockpitHomeProjection:
    schema_version: int
    business_id: str
    role: str
    timezone_name: str
    as_of: str
    today_from: str
    today_to: str
    today: tuple[CockpitHomeMetric, ...]
    money: tuple[CockpitHomeMoney, ...]
    attention: tuple[str, ...]
    next_action: CockpitHomeAction | None
    sources: tuple[CockpitHomeSource, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

def _now_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("cockpit home now must be timezone-aware")
    return current.astimezone(timezone.utc)


def _business_zone(actor: TenantContext) -> ZoneInfo:
    profile = get_business_profile(actor=actor)
    try:
        return ZoneInfo(profile.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("business timezone is invalid") from exc


def _today_window(*, zone: ZoneInfo, now: datetime) -> tuple[datetime, datetime]:
    local_date = now.astimezone(zone).date()
    start = datetime.combine(local_date, time.min, tzinfo=zone).astimezone(timezone.utc)
    end = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)
    return start, end


def _allowed(check: Callable[[], None]) -> bool:
    try:
        check()
    except TenantPermissionDenied:
        return False
    return True

def _source(source_id: str, status: str, message: str) -> CockpitHomeSource:
    return CockpitHomeSource(id=source_id, status=status, message=message)


def _growth_route(action_key: str) -> str | None:
    if action_key == "sales_handoff":
        return "sales"
    if action_key.startswith("sales_plan:"):
        return "sales"
    if action_key.startswith("sales_lead:"):
        return "sales"
    if action_key == "economic_reactivation":
        return "sales"
    if action_key == "economic_open_slots":
        return "calendar"
    if action_key == "attribution_review":
        return "growth"
    if action_key == "economic_paid_acquisition":
        return "growth"
    return None


def _home_action(action: GrowthAction) -> CockpitHomeAction | None:
    if action.action_key == "none":
        return None
    return CockpitHomeAction(
        title=action.title,
        reason=action.reason,
        route=_growth_route(action.action_key),
        action_key=action.action_key,
    )

def _growth_metrics(snapshot: GrowthCockpitSnapshot) -> list[CockpitHomeMetric]:
    labels = {
        "leads": "Новые лиды",
        "qualified_leads": "Подтверждённый интерес",
        "bookings": "Новые записи",
        "paid_customers": "Оплатившие клиенты",
    }
    return [
        CockpitHomeMetric(
            key=item.key,
            title=labels.get(item.key, "Результат"),
            value=int(item.value),
            detail=item.meaning,
        )
        for item in snapshot.today_metrics
        if item.key in labels
    ]


def _money_display(amount_minor: int, currency: str) -> str:
    exponent = settlement_currency_minor_unit_exponent(currency)
    amount = Decimal(int(amount_minor)) / (Decimal(10) ** exponent)
    rendered = f"{amount:,.{exponent}f}" if exponent else f"{amount:,.0f}"
    return f"{rendered.replace(',', ' ')} {currency.upper()}"


def _growth_money(snapshot: GrowthCockpitSnapshot) -> list[CockpitHomeMoney]:
    return [
        CockpitHomeMoney(
            title="Подтверждённая выручка · 7 дней",
            amount_minor=int(item.amount_minor),
            currency=str(item.currency).upper(),
            display_amount=_money_display(int(item.amount_minor), str(item.currency)),
            detail=item.meaning,
        )
        for item in snapshot.revenue
    ]

def get_cockpit_home(
    *,
    actor: TenantContext,
    now: datetime | None = None,
) -> CockpitHomeProjection:
    """Build a bounded read-only Home projection from existing canonical owners."""

    current = _now_utc(now)
    zone = _business_zone(actor)
    today_from, today_to = _today_window(zone=zone, now=current)
    can_customers = _allowed(actor.assert_can_view_customer_records)
    can_outcomes = _allowed(actor.assert_can_view_outcome_ledger)
    can_growth = _allowed(actor.assert_can_view_promotion_analytics)

    metrics: list[CockpitHomeMetric] = []
    money: list[CockpitHomeMoney] = []
    attention: list[str] = []
    sources: list[CockpitHomeSource] = []
    next_action: CockpitHomeAction | None = None

    growth_snapshot: GrowthCockpitSnapshot | None = None
    if can_customers and can_outcomes and can_growth:
        try:
            growth_snapshot = get_growth_cockpit(
                actor=actor,
                period_days=7,
                now=current,
                advertising_loader=lambda **_kwargs: None,
            )
        except TenantPermissionDenied:
            sources.append(_source("growth", "restricted", "Результаты и деньги недоступны для текущей роли."))
        except (OSError, ValueError):
            sources.append(_source("growth", "unavailable", "Часть результатов сейчас недоступна. Попробуйте обновить позже."))
        except RuntimeError:
            sources.append(_source("growth", "unavailable", "Часть результатов сейчас недоступна. Попробуйте обновить позже."))
        else:
            metrics.extend(_growth_metrics(growth_snapshot))
            money.extend(_growth_money(growth_snapshot))
            attention.extend(growth_snapshot.attention)
            next_action = _home_action(growth_snapshot.next_action)
            sources.append(_source("growth", "available", "Результаты и следующий шаг получены из канонических данных бизнеса."))
    else:
        sources.append(_source("growth", "restricted", "Результаты и деньги скрыты для текущей роли."))

    if can_customers:
        try:
            activity = tenant_customer_activity(
                actor=actor,
                now=current,
                limit=25,
                today_from=today_from,
                today_to=today_to,
            )
        except TenantPermissionDenied:
            sources.append(_source("customer_activity", "restricted", "Данные клиентов недоступны для текущей роли."))
        except (OSError, ValueError):
            sources.append(_source("customer_activity", "unavailable", "Активность клиентов сейчас недоступна."))
        except RuntimeError:
            sources.append(_source("customer_activity", "unavailable", "Активность клиентов сейчас недоступна."))
        else:
            metrics.extend(
                (
                    CockpitHomeMetric(
                        key="new_customers_today",
                        title="Новые клиенты",
                        value=int(activity.new_today),
                        detail="Клиенты, впервые появившиеся в локальном дне бизнеса.",
                    ),
                    CockpitHomeMetric(
                        key="active_customers_today",
                        title="Активные клиенты",
                        value=int(activity.active_today),
                        detail="Клиенты с подтверждённой активностью в локальном дне бизнеса.",
                    ),
                )
            )
            sources.append(_source("customer_activity", "available", "Активность клиентов рассчитана в часовом поясе бизнеса."))
    else:
        sources.append(_source("customer_activity", "restricted", "Данные клиентов скрыты для текущей роли."))
    if can_customers:
        try:
            slots = list_booking_slots(actor=actor, include_unavailable=False)
        except TenantPermissionDenied:
            sources.append(_source("bookings", "restricted", "Записи недоступны для текущей роли."))
        except (OSError, ValueError):
            sources.append(_source("bookings", "unavailable", "Расписание сейчас недоступно."))
        except RuntimeError:
            sources.append(_source("bookings", "unavailable", "Расписание сейчас недоступно."))
        else:
            today_slots = [
                item
                for item in slots
                if today_from <= datetime.fromisoformat(item.slot.starts_at) < today_to
            ]
            metrics.append(
                CockpitHomeMetric(
                    key="open_slots_today",
                    title="Свободные окна сегодня",
                    value=len(today_slots),
                    detail="Доступные для записи окна до конца локального дня бизнеса.",
                )
            )
            sources.append(_source("bookings", "available", "Расписание прочитано без изменения записей."))
    else:
        sources.append(_source("bookings", "restricted", "Расписание клиентов скрыто для текущей роли."))
    if growth_snapshot is None and can_customers:
        try:
            sales_actions = project_sales_actions(
                handoffs=list_sales_handoff_work(actor=actor, limit=_HOME_ACTION_LIMIT),
                sales_work=list_sales_work(actor=actor, limit=10),
                limit=_HOME_ACTION_LIMIT,
            )
        except TenantPermissionDenied:
            sources.append(_source("sales", "restricted", "Работа с клиентами недоступна для текущей роли."))
        except (OSError, ValueError):
            sources.append(_source("sales", "unavailable", "Рабочая очередь клиентов сейчас недоступна."))
        except RuntimeError:
            sources.append(_source("sales", "unavailable", "Рабочая очередь клиентов сейчас недоступна."))
        else:
            if sales_actions:
                attention.append(f"В рабочей очереди действий: {len(sales_actions)}.")
                if next_action is None:
                    next_action = _home_action(sales_actions[0])
            sources.append(_source("sales", "available", "Порядок действий взят из канонической sales/handoff очереди."))

    pending_approvals = ()
    try:
        pending_approvals = list_pending_automation_action_approvals(
            actor=actor,
            now=current,
            limit=_HOME_APPROVAL_LIMIT,
        )
    except TenantPermissionDenied:
        sources.append(_source("automation_approvals", "restricted", "Согласования недоступны для текущей роли."))
    except (OSError, ValueError):
        sources.append(_source("automation_approvals", "unavailable", "Согласования сейчас недоступны."))
    except RuntimeError:
        sources.append(_source("automation_approvals", "unavailable", "Согласования сейчас недоступны."))
    else:
        sources.append(_source("automation_approvals", "available", "Согласования прочитаны из канонического AutomationPolicy."))
        if pending_approvals:
            attention.append(f"Ждут решения по автоматическим действиям: {len(pending_approvals)}.")
            if next_action is None:
                owner = actor.role.value == "owner"
                next_action = CockpitHomeAction(
                    title=("Принять решение по действию" if owner else "Посмотреть ожидающее согласование"),
                    reason=(
                        "Первое ожидающее действие требует решения владельца до выполнения."
                        if owner
                        else "Действие не будет выполнено, пока владелец его не согласует."
                    ),
                    route="automation",
                    action_key="automation_approval",
                )

    if can_growth:
        sources.append(_source("advertising", "available_elsewhere", "Реклама доступна в разделе «Рост и реклама»."))
    else:
        sources.append(_source("advertising", "restricted", "Рекламные данные скрыты для текущей роли."))
    if next_action is None:
        fallback = {
            "support": ("Открыть клиентов", "Проверьте текущую работу с клиентами.", "customers"),
            "content_manager": ("Открыть контент", "Перейдите к материалам и публикациям.", "content"),
            "marketer": ("Открыть рост", "Перейдите к продвижению и результатам рекламы.", "growth"),
            "analyst": ("Открыть аналитику", "Перейдите к доступной аналитике бизнеса.", "analytics"),
        }.get(actor.role.value)
        if fallback is not None:
            next_action = CockpitHomeAction(
                title=fallback[0],
                reason=fallback[1],
                route=fallback[2],
                action_key="role_home_route",
            )

    return CockpitHomeProjection(
        schema_version=_HOME_SCHEMA_VERSION,
        business_id=actor.business_id,
        role=actor.role.value,
        timezone_name=zone.key,
        as_of=current.isoformat(timespec="seconds"),
        today_from=today_from.isoformat(timespec="seconds"),
        today_to=today_to.isoformat(timespec="seconds"),
        today=tuple(metrics[:8]),
        money=tuple(money[:8]),
        attention=tuple(dict.fromkeys(attention))[:6],
        next_action=next_action,
        sources=tuple(sources[:8]),
    )


__all__ = [
    "CockpitHomeAction",
    "CockpitHomeMetric",
    "CockpitHomeMoney",
    "CockpitHomeProjection",
    "CockpitHomeSource",
    "get_cockpit_home",
]
