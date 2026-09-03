from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from datetime import datetime, time, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clientplatform.application.activity import get_business_profile
from clientplatform.application.automation_policy import list_pending_automation_action_approvals
from clientplatform.application.bookings import list_booking_slots
from clientplatform.application.cockpit import cockpit_navigation, resolve_cockpit_context
from clientplatform.application.customer_activity import tenant_customer_activity
from clientplatform.application.growth_cockpit import (
    GrowthAction,
    get_customer_work_actions,
    get_growth_cockpit,
)
from clientplatform.application.tenancy import resolve_tenant_context
from clientplatform.domain.money import settlement_currency_minor_unit_exponent
from clientplatform.domain.tenancy import (
    TenantAccessDenied,
    TenantContext,
    TenantPermissionDenied,
)

_SCHEMA_VERSION = "2026-09-04.v1"
_MAX_METRICS = 10
_MAX_ATTENTION = 8
_MAX_ACTIONS = 5
_MAX_LIMITATIONS = 8


class CockpitHomeUnavailable(RuntimeError):
    """Required Home projection metadata is unavailable or invalid."""


@dataclass(frozen=True, slots=True)
class CockpitHomeMetric:
    key: str
    title: str
    value: int
    source: str
    meaning: str


@dataclass(frozen=True, slots=True)
class CockpitHomeMoney:
    amount_minor: int
    currency: str
    display: str
    period: str
    source: str
    meaning: str


@dataclass(frozen=True, slots=True)
class CockpitHomeAction:
    title: str
    reason: str
    section: str
    action_key: str


@dataclass(frozen=True, slots=True)
class CockpitHomeSnapshot:
    schema_version: str
    business_id: str
    business_name: str
    role: str
    timezone_name: str
    as_of: str
    today_from: str
    today_to: str
    metrics: tuple[CockpitHomeMetric, ...]
    money: tuple[CockpitHomeMoney, ...]
    attention: tuple[str, ...]
    actions: tuple[CockpitHomeAction, ...]
    limitations: tuple[str, ...]
    empty_message: str | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("cockpit home now must be timezone-aware")
    return current.astimezone(timezone.utc)


def _business_window(*, actor: TenantContext, now: datetime) -> tuple[str, datetime, datetime]:
    profile = get_business_profile(actor=actor)
    try:
        zone = ZoneInfo(profile.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CockpitHomeUnavailable("business timezone is invalid") from exc
    local_date = now.astimezone(zone).date()
    start = datetime.combine(local_date, time.min, tzinfo=zone).astimezone(timezone.utc)
    end = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)
    return zone.key, start, end


def _money_display(amount_minor: int, currency: str) -> str:
    exponent = settlement_currency_minor_unit_exponent(currency)
    divisor = Decimal(10) ** exponent
    major = Decimal(int(amount_minor)) / divisor
    return f"{major:.{exponent}f} {currency}"


def _section_for_action(action: GrowthAction) -> str:
    key = action.action_key
    if key.startswith("sales_") or key == "sales_handoff":
        return "sales"
    if key == "economic_open_slots":
        return "calendar"
    if key == "economic_reactivation":
        return "customers"
    if key == "economic_paid_acquisition":
        return "growth"
    if key == "attribution_review":
        return "analytics"
    return "home"


def _home_action(action: GrowthAction) -> CockpitHomeAction:
    return CockpitHomeAction(
        title=action.title,
        reason=action.reason,
        section=_section_for_action(action),
        action_key=action.action_key,
    )


def _source_unavailable(limitations: list[str], code: str) -> None:
    if code not in limitations and len(limitations) < _MAX_LIMITATIONS:
        limitations.append(code)


def _metric(
    metrics: list[CockpitHomeMetric],
    *,
    key: str,
    title: str,
    value: int,
    source: str,
    meaning: str,
) -> None:
    if len(metrics) >= _MAX_METRICS:
        return
    metrics.append(
        CockpitHomeMetric(
            key=key,
            title=title,
            value=max(0, int(value)),
            source=source,
            meaning=meaning,
        )
    )


def build_cockpit_home(
    *,
    actor: TenantContext,
    business_name: str,
    now: datetime | None = None,
    growth_loader: Callable[..., object] = get_growth_cockpit,
    customer_work_loader: Callable[..., tuple[GrowthAction, ...]] = get_customer_work_actions,
    customer_activity_loader: Callable[..., object] = tenant_customer_activity,
    booking_loader: Callable[..., list[object]] = list_booking_slots,
    approval_loader: Callable[..., tuple[object, ...]] = list_pending_automation_action_approvals,
) -> CockpitHomeSnapshot:
    """Compose a bounded read-only Home projection from existing canonical owners."""

    current = resolve_tenant_context(user_id=actor.user_id, business_id=actor.business_id)
    timestamp = _now(now)
    timezone_name, today_from, today_to = _business_window(actor=current, now=timestamp)
    metrics: list[CockpitHomeMetric] = []
    money: list[CockpitHomeMoney] = []
    attention: list[str] = []
    actions: list[CockpitHomeAction] = []
    limitations: list[str] = []

    try:
        growth = growth_loader(
            actor=current,
            period_days=7,
            now=timestamp,
            advertising_loader=lambda **_kwargs: None,
        )
    except TenantAccessDenied:
        raise
    except TenantPermissionDenied:
        growth = None
    except OSError:
        growth = None
        _source_unavailable(limitations, "economics_unavailable")
    except RuntimeError:
        growth = None
        _source_unavailable(limitations, "economics_unavailable")
    except ValueError:
        growth = None
        _source_unavailable(limitations, "economics_unavailable")
    if growth is not None:
        for item in tuple(getattr(growth, "today_metrics", ()))[:4]:
            _metric(
                metrics,
                key=f"today_{item.key}",
                title={
                    "leads": "Лиды сегодня",
                    "qualified_leads": "Квалифицировано сегодня",
                    "bookings": "Записи сегодня",
                    "paid_customers": "Оплатили сегодня",
                }.get(item.key, item.key),
                value=item.value,
                source=item.source,
                meaning=item.meaning,
            )
        for item in tuple(getattr(growth, "revenue", ()))[:4]:
            try:
                display = _money_display(int(item.amount_minor), str(item.currency))
            except ValueError:
                _source_unavailable(limitations, "economics_currency_unavailable")
                continue
            money.append(
                CockpitHomeMoney(
                    amount_minor=int(item.amount_minor),
                    currency=str(item.currency),
                    display=display,
                    period="7d",
                    source=item.source,
                    meaning=item.meaning,
                )
            )
        attention.extend(str(value) for value in tuple(getattr(growth, "attention", ()))[:_MAX_ATTENTION])
        actions.extend(_home_action(item) for item in tuple(getattr(growth, "actions", ()))[:_MAX_ACTIONS])
        for code in tuple(getattr(growth, "limitations", ())):
            _source_unavailable(limitations, str(code))

    try:
        activity = customer_activity_loader(actor=current, now=timestamp, limit=1)
    except TenantAccessDenied:
        raise
    except TenantPermissionDenied:
        activity = None
    except OSError:
        activity = None
        _source_unavailable(limitations, "customer_activity_unavailable")
    except RuntimeError:
        activity = None
        _source_unavailable(limitations, "customer_activity_unavailable")
    except ValueError:
        activity = None
        _source_unavailable(limitations, "customer_activity_unavailable")
    if activity is not None:
        _metric(
            metrics,
            key="customers_total",
            title="Клиентов всего",
            value=int(getattr(activity, "total", 0)),
            source="customer_activity",
            meaning="Активные клиенты этого бизнеса; показатель не трактуется как число за сегодня.",
        )

    try:
        slots = booking_loader(actor=current, include_unavailable=True)
    except TenantAccessDenied:
        raise
    except TenantPermissionDenied:
        slots = None
    except OSError:
        slots = None
        _source_unavailable(limitations, "booking_unavailable")
    except RuntimeError:
        slots = None
        _source_unavailable(limitations, "booking_unavailable")
    except ValueError:
        slots = None
        _source_unavailable(limitations, "booking_unavailable")
    if slots is not None:
        open_today = 0
        booked_today = 0
        booking_valid = True
        for view in slots:
            slot = getattr(view, "slot", view)
            try:
                starts_at = datetime.fromisoformat(str(getattr(slot, "starts_at")))
                if starts_at.tzinfo is None or starts_at.utcoffset() is None:
                    raise ValueError("booking slot start must be timezone-aware")
            except (AttributeError, ValueError):
                booking_valid = False
                _source_unavailable(limitations, "booking_unavailable")
                break
            if not today_from <= starts_at.astimezone(timezone.utc) < today_to:
                continue
            status = str(getattr(getattr(slot, "status", ""), "value", getattr(slot, "status", "")))
            if status == "open":
                open_today += 1
            elif status == "booked":
                booked_today += 1
        if booking_valid:
            _metric(
                metrics,
                key="open_slots_today",
                title="Свободных окон сегодня",
                value=open_today,
                source="booking_slots",
                meaning="Оставшиеся открытые слоты в локальном сегодняшнем дне бизнеса.",
            )
            _metric(
                metrics,
                key="booked_slots_today",
                title="Предстоящих записей сегодня",
                value=booked_today,
                source="booking_slots",
                meaning="Оставшиеся забронированные слоты в локальном сегодняшнем дне бизнеса.",
            )

    if not actions:
        try:
            work_actions = customer_work_loader(actor=current, limit=_MAX_ACTIONS)
        except TenantAccessDenied:
            raise
        except TenantPermissionDenied:
            work_actions = ()
        except OSError:
            work_actions = ()
            _source_unavailable(limitations, "customer_work_unavailable")
        except RuntimeError:
            work_actions = ()
            _source_unavailable(limitations, "customer_work_unavailable")
        except ValueError:
            work_actions = ()
            _source_unavailable(limitations, "customer_work_unavailable")
        actions.extend(_home_action(item) for item in work_actions[:_MAX_ACTIONS])

    try:
        approvals = approval_loader(actor=current, now=timestamp, limit=20)
    except TenantAccessDenied:
        raise
    except TenantPermissionDenied:
        approvals = None
    except OSError:
        approvals = None
        _source_unavailable(limitations, "automation_approvals_unavailable")
    except RuntimeError:
        approvals = None
        _source_unavailable(limitations, "automation_approvals_unavailable")
    except ValueError:
        approvals = None
        _source_unavailable(limitations, "automation_approvals_unavailable")
    if approvals is not None:
        pending = len(approvals)
        _metric(
            metrics,
            key="automation_pending",
            title="Ждут решения по автоматизации",
            value=pending,
            source="automation_policy",
            meaning="Текущие approval-запросы; Home их не подтверждает и не исполняет.",
        )
        if pending and len(attention) < _MAX_ATTENTION:
            attention.append(f"Есть решения по автоматизации, ожидающие проверки: {pending}.")

    metrics = metrics[:_MAX_METRICS]
    attention = list(dict.fromkeys(attention))[:_MAX_ATTENTION]
    actions = actions[:_MAX_ACTIONS]
    limitations = limitations[:_MAX_LIMITATIONS]
    has_factual_signals = bool(metrics or attention or actions)
    if not actions:
        fallback = next(
            (
                item
                for item in cockpit_navigation(current)
                if item.id != "home" and item.status == "available"
            ),
            None,
        )
        if fallback is not None:
            actions.append(
                CockpitHomeAction(
                    title=f"Открыть: {fallback.title}",
                    reason=fallback.summary,
                    section=fallback.id,
                    action_key=f"navigation:{fallback.id}",
                )
            )
    empty_message = None
    if not has_factual_signals:
        empty_message = (
            "На сегодня нет доступных Вашей роли срочных сигналов. "
            "ClientPlatform не расширяет права и не подменяет недоступные данные нулями."
        )
    return CockpitHomeSnapshot(
        schema_version=_SCHEMA_VERSION,
        business_id=current.business_id,
        business_name=str(business_name),
        role=current.role.value,
        timezone_name=timezone_name,
        as_of=timestamp.isoformat(),
        today_from=today_from.isoformat(),
        today_to=today_to.isoformat(),
        metrics=tuple(metrics),
        money=tuple(money),
        attention=tuple(attention),
        actions=tuple(actions),
        limitations=tuple(limitations),
        empty_message=empty_message,
    )


def resolve_cockpit_home(
    *,
    telegram_user_id: int,
    requested_business_id: str | None = None,
    now: datetime | None = None,
) -> CockpitHomeSnapshot:
    """Resolve verified identity selection through M7-001 before loading Home."""

    context = resolve_cockpit_context(
        telegram_user_id=telegram_user_id,
        requested_business_id=requested_business_id,
    )
    if context.onboarding_required or context.business_id is None or context.business_name is None:
        raise TenantAccessDenied("active business membership was not found")
    actor = resolve_tenant_context(user_id=context.user_id, business_id=context.business_id)
    return build_cockpit_home(
        actor=actor,
        business_name=context.business_name,
        now=now,
    )


__all__ = [
    "CockpitHomeAction",
    "CockpitHomeUnavailable",
    "CockpitHomeMetric",
    "CockpitHomeMoney",
    "CockpitHomeSnapshot",
    "build_cockpit_home",
    "resolve_cockpit_home",
]
