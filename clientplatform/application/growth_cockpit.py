from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clientplatform.application.activity import get_business_profile
from clientplatform.application.ad_spend_consent import list_ad_spend_authorizations
from clientplatform.application.bookings import list_booking_slots
from clientplatform.application.retention import ReactivationOpportunity, list_reactivation_opportunities
from clientplatform.application.revenue_attribution import (
    get_business_revenue_journey,
    get_business_unit_economics,
)
from clientplatform.application.sales_ui import (
    count_sales_handoff_work,
    list_sales_handoff_work,
    list_sales_work,
)
from clientplatform.application.yandex_growth_analytics import (
    YandexGrowthSnapshot,
    get_yandex_growth_snapshot,
)
from clientplatform.domain.ad_spend import AdSpendAuthorization, AdSpendAuthorizationStatus
from clientplatform.domain.money import settlement_currency_minor_unit_exponent
from clientplatform.domain.revenue_attribution import RevenueJourneySnapshot, UnitEconomicsSnapshot
from clientplatform.domain.tenancy import PlatformRole, TenantContext

_ALLOWED_PERIODS = frozenset({7, 30})

_SOURCE_LABELS = {
    "direct": "Прямой источник",
    "organic": "Органика",
    "referral": "Рекомендации",
    "promotion": "Продвижение",
    "unknown": "Источник не определён",
    "yandex_direct": "Яндекс Директ",
    "telegram": "Telegram",
    "vk": "ВКонтакте",
    "max": "MAX",
    "website": "Сайт",
    "partner": "Партнёры",
    "manual_import": "Импорт / вручную",
}


def acquisition_source_label(source: object) -> str:
    """Return one channel-neutral owner label for a canonical acquisition source."""

    key = str(getattr(source, "value", source) or "").strip()
    return _SOURCE_LABELS.get(key, "Источник клиентов")


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
    source_id: str | None = None


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
    journey: RevenueJourneySnapshot
    needs_reply: int
    advertising: YandexGrowthSnapshot | None
    what_worked: tuple[GrowthSourceResult, ...]
    attention: tuple[str, ...]
    next_action: GrowthAction
    limitations: tuple[str, ...]
    actions: tuple[GrowthAction, ...] = ()


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
            label=acquisition_source_label(source),
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


def _bounded_action_text(value: object, *, limit: int = 180) -> str | None:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _money_text(amount_minor: int, currency: str) -> str:
    exponent = settlement_currency_minor_unit_exponent(currency)
    amount = Decimal(int(amount_minor)) / (Decimal(10) ** exponent)
    rendered = f"{amount:,.{exponent}f}" if exponent else f"{amount:,.0f}"
    return f"{rendered.replace(',', ' ')} {str(currency).upper()}"


def _utc(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("economic action timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _current_approved_spend(
    authorizations: list[AdSpendAuthorization], *, now: datetime
) -> AdSpendAuthorization | None:
    current = now.astimezone(timezone.utc)
    allowed = {
        AdSpendAuthorizationStatus.AUTHORIZED,
        AdSpendAuthorizationStatus.LAUNCHING,
        AdSpendAuthorizationStatus.ACTIVE,
    }
    for item in authorizations:
        if item.status not in allowed or item.consent_receipt is None:
            continue
        try:
            if _utc(item.authorization_expires_at) <= current:
                continue
            if _utc(item.snapshot.valid_until) <= current:
                continue
        except (TypeError, ValueError):
            continue
        return item
    return None


def _economic_next_action(
    *,
    open_slots: int,
    reactivation: list[ReactivationOpportunity],
    authorizations: list[AdSpendAuthorization],
    journey: RevenueJourneySnapshot,
    now: datetime,
) -> GrowthAction | None:
    """Pick capacity, free reactivation, then already-consented paid acquisition."""
    if open_slots < 1:
        return GrowthAction(
            title="Сначала открыть время для записи",
            reason=(
                "Свободных времён нет. До привлечения или возврата клиентов сначала "
                "нужно создать доступное время — так ClientPlatform не предлагает тратить "
                "деньги на спрос, который сейчас нельзя принять."
            ),
            action_key="economic_open_slots",
            source="booking_availability",
        )

    routable = [item for item in reactivation if item.route_platform]
    if routable:
        candidate = routable[0]
        history = (
            f" За выбранный период уже подтверждено возвратов: {journey.reactivated_customers}."
            if journey.reactivated_customers > 0
            else ""
        )
        return GrowthAction(
            title="Сначала вернуть существующих клиентов",
            reason=(
                f"Есть {len(routable)} клиент(ов) из reactivation cohort с доступным "
                "разрешённым каналом и свободное время для записи. Начните с этого без "
                "рекламных расходов; сообщение само не отправится." + history
            ),
            action_key="economic_reactivation",
            source="retention_projection",
            source_id=candidate.candidate.customer_id,
        )

    approved = _current_approved_spend(authorizations, now=now)
    if approved is None:
        return None
    paid_from_yandex = next(
        (
            item.paid_customers
            for item in journey.sources
            if str(getattr(item.source, "value", item.source)) == "yandex_direct"
        ),
        0,
    )
    history = (
        f" За выбранный период Яндекс Директ дал оплативших клиентов: {paid_from_yandex}."
        if int(paid_from_yandex) > 0
        else " Исторические оплаты из Яндекс Директ за выбранный период не подтверждены."
    )
    return GrowthAction(
        title="Проверить уже разрешённое платное привлечение",
        reason=(
            f"Свободных времён: {open_slots}. Владелец уже подтвердил рекламные пределы: "
            f"общий {_money_text(approved.hard_cap_minor, approved.currency)}, "
            f"дневной {_money_text(approved.daily_cap_minor, approved.currency)}. "
            "Любой расход остаётся только внутри этих consent-bound лимитов." + history
        ),
        action_key="economic_paid_acquisition",
        source="ad_spend_authorization",
        source_id=approved.id,
    )


def _action_queue(
    *,
    handoffs: list[dict[str, object]],
    sales_work: list[dict[str, object]],
    economics: UnitEconomicsSnapshot,
    economic_action: GrowthAction | None = None,
    limit: int = 5,
) -> tuple[GrowthAction, ...]:
    """Project a small deterministic owner queue from canonical read models only."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 10:
        raise ValueError("owner action queue limit must be an integer between 1 and 10")

    actions: list[GrowthAction] = []
    handoff_leads: set[str] = set()
    severity_reason = {
        "urgent": "Срочная передача: клиенту требуется личное участие сотрудника.",
        "high": "Важная передача: клиенту требуется личное участие сотрудника.",
        "normal": "Открытая передача: клиенту требуется личное участие сотрудника.",
    }
    for item in handoffs:
        lead_id = str(item.get("lead_id") or "").strip()
        handoff_id = str(item.get("id") or "").strip()
        if not lead_id or not handoff_id:
            continue
        handoff_leads.add(lead_id)
        severity = str(item.get("severity") or "normal").strip().lower()
        customer_name = _bounded_action_text(item.get("customer_name"), limit=80) or "Клиент"
        actions.append(
            GrowthAction(
                title=f"Ответить лично: {customer_name}",
                reason=severity_reason.get(severity, severity_reason["normal"]),
                action_key="sales_handoff",
                source="sales_handoff_queue",
                source_id=handoff_id,
            )
        )

    for item in sales_work:
        lead_id = str(item.get("id") or "").strip()
        if not lead_id or lead_id in handoff_leads:
            continue
        customer_name = _bounded_action_text(item.get("customer_name"), limit=80) or "Клиент"
        plan_id = str(item.get("next_plan_id") or "").strip()
        next_action_kind = str(item.get("next_action_kind") or "").strip()
        next_action = _bounded_action_text(item.get("next_action"))
        due_at = _bounded_action_text(item.get("due_at"), limit=80)
        if plan_id and next_action_kind:
            requires_approval = bool(item.get("next_plan_requires_approval"))
            reason = (
                "ClientPlatform уже подготовил следующий шаг; перед выполнением требуется Ваше подтверждение."
                if requires_approval
                else "Следующий шаг уже сохранён в работе с клиентом."
            )
            if due_at:
                reason += f" Срок: {due_at}."
            actions.append(
                GrowthAction(
                    title=f"Продолжить работу с клиентом: {customer_name}",
                    reason=reason,
                    action_key=f"sales_plan:{plan_id}",
                    source="sales_action_plan",
                    source_id=plan_id,
                )
            )
            continue
        if next_action:
            reason = f"Сохранён следующий шаг: {next_action}."
            if due_at:
                reason += f" Срок: {due_at}."
            actions.append(
                GrowthAction(
                    title=f"Следующий шаг по клиенту: {customer_name}",
                    reason=reason,
                    action_key=f"sales_lead:{lead_id}",
                    source="sales_lead",
                    source_id=lead_id,
                )
            )

    if economic_action is not None:
        actions.append(economic_action)

    if economics.unattributed_monetary_outcomes > 0:
        count = int(economics.unattributed_monetary_outcomes)
        actions.append(
            GrowthAction(
                title="Проверить источники оплат",
                reason=(
                    f"Есть денежные результаты без подтверждённого источника клиента: {count}."
                ),
                action_key="attribution_review",
                source="revenue_attribution",
                source_id=None,
            )
        )

    # Each upstream projection already owns its deterministic ordering:
    # handoffs by severity/time/id, sales work by due-time/update/id. Keep those
    # canonical orders and only compose the source classes in the explicit
    # business priority above instead of inventing a hidden numeric score.
    return tuple(actions[:limit])


def _next_action(actions: tuple[GrowthAction, ...]) -> GrowthAction:
    if actions:
        return actions[0]
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
    advertising_loader: Callable[..., YandexGrowthSnapshot | None] = get_yandex_growth_snapshot,
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
    journey = get_business_revenue_journey(
        actor=actor,
        occurred_from=period_from,
        occurred_to=period_to,
    )
    needs_reply = count_sales_handoff_work(actor=actor)
    handoffs = list_sales_handoff_work(actor=actor, limit=5) if needs_reply else []
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
    except (RuntimeError, ValueError):
        advertising_error = True
    except OSError:
        advertising_error = True

    limitations = list(period.limitations)
    limitations.extend(journey.limitations)
    if advertising_error:
        limitations.append("advertising_unavailable")
    elif advertising is not None and advertising.connected_accounts > 0:
        limitations.append("advertising_currency_unverified")

    economic_action: GrowthAction | None = None
    economic_projection_incomplete = False
    try:
        open_slots = len(list_booking_slots(actor=actor, include_unavailable=False))
        reactivation = list_reactivation_opportunities(actor=actor, now=now, limit=100)
        authorizations = (
            list_ad_spend_authorizations(actor=actor, limit=50)
            if actor.role == PlatformRole.OWNER
            else []
        )
        economic_action = _economic_next_action(
            open_slots=open_slots,
            reactivation=reactivation,
            authorizations=authorizations,
            journey=journey,
            now=(now or datetime.now(timezone.utc)),
        )
    except OSError:
        economic_projection_incomplete = True
    except RuntimeError:
        economic_projection_incomplete = True
    except ValueError:
        economic_projection_incomplete = True

    if economic_projection_incomplete:
        limitations.append("economic_next_action_unavailable")

    actions = _action_queue(
        handoffs=handoffs,
        sales_work=sales_work,
        economics=period,
        economic_action=economic_action,
    )

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
        journey=journey,
        needs_reply=needs_reply,
        advertising=advertising,
        what_worked=_what_worked(period),
        attention=_attention(
            economics=period,
            needs_reply=needs_reply,
            advertising=advertising,
            advertising_error=advertising_error,
        ),
        actions=actions,
        next_action=_next_action(actions),
        limitations=tuple(dict.fromkeys(limitations)),
    )


__all__ = [
    "GrowthAction",
    "GrowthCockpitSnapshot",
    "GrowthMetric",
    "GrowthMoney",
    "GrowthSourceResult",
    "acquisition_source_label",
    "get_growth_cockpit",
]
