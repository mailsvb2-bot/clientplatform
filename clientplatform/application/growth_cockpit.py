from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clientplatform.application.activity import get_business_profile
from clientplatform.application.revenue_attribution import get_business_unit_economics
from clientplatform.application.sales_ui import list_sales_handoff_work, list_sales_work
from clientplatform.application.yandex_growth_analytics import get_yandex_growth_snapshot
from clientplatform.domain.attribution import AcquisitionSource
from clientplatform.domain.growth_cockpit import (
    GrowthCockpitAction,
    GrowthCockpitMetric,
    GrowthCockpitMoney,
    GrowthCockpitSnapshot,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from clientplatform.integrations.yandex_direct import YandexDirectError
from services.db import get_db_ro

_ALLOWED_PERIOD_DAYS = frozenset({7, 30})
_REVENUE_SOURCE = "outcome_ledger:first_touch_unit_economics"
_SALES_SOURCE = "sales_ui:open_work"
_AD_SOURCE = "yandex_direct:read_only_report+promotion_attribution"

_SOURCE_LABELS = {
    AcquisitionSource.ORGANIC: "органика",
    AcquisitionSource.REFERRAL: "рекомендации",
    AcquisitionSource.TELEGRAM: "Telegram",
    AcquisitionSource.VK: "VK",
    AcquisitionSource.MAX: "MAX",
    AcquisitionSource.WEBSITE: "сайт",
    AcquisitionSource.YANDEX_DIRECT: "реклама в Яндексе",
    AcquisitionSource.PARTNER: "партнёры",
    AcquisitionSource.MANUAL_IMPORT: "импортированные данные",
    AcquisitionSource.UNKNOWN: "источник пока не определён",
}
_NEXT_ACTION_LABELS = {
    "respond": "подготовит следующий ответ по обращению",
    "ask_qualification": "подготовит уточняющий вопрос клиенту",
    "present_offer": "подготовит подходящее предложение",
    "checkout_followup": "подготовит сопровождение до оплаты",
    "human_handoff": "оставит обращение владельцу для личного решения",
    "noop": "не будет предпринимать лишних действий",
}


def _current_actor(actor: TenantContext) -> TenantContext:
    with get_db_ro() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
    current.assert_can_view_outcome_ledger()
    current.assert_can_view_attribution_spine()
    current.assert_can_view_customer_records()
    return current


def _period_window(
    *,
    timezone_name: str,
    period_days: int,
    now: datetime,
) -> tuple[datetime, datetime, datetime]:
    if period_days not in _ALLOWED_PERIOD_DAYS:
        raise ValueError("growth cockpit period must be 7 or 30 days")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("growth cockpit now must be timezone-aware")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise RuntimeError("business timezone is invalid") from exc
    local_now = now.astimezone(zone)
    local_today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_period = local_today.replace()
    from datetime import timedelta

    local_period -= timedelta(days=period_days - 1)
    return (
        local_today.astimezone(timezone.utc),
        local_period.astimezone(timezone.utc),
        now.astimezone(timezone.utc),
    )


def _money_items(items: object, *, meaning: str) -> tuple[GrowthCockpitMoney, ...]:
    return tuple(
        GrowthCockpitMoney(
            currency=item.currency,
            amount_minor=item.amount_minor,
            source=_REVENUE_SOURCE,
            meaning=meaning,
        )
        for item in items
    )


def _what_worked(source_breakdown: object) -> str:
    entries = list(source_breakdown.items())
    if not entries:
        return "Пока недостаточно подтверждённых оплат, чтобы надёжно назвать лучший источник."
    source, count = max(entries, key=lambda pair: (int(pair[1]), str(pair[0])))
    label = _SOURCE_LABELS.get(source, "другой источник")
    return f"Больше всего подтверждённых денежных событий за период связано с источником «{label}»: {int(count)}."


def _decision_action(handoffs: list[dict[str, object]], work: list[dict[str, object]]) -> GrowthCockpitAction:
    if handoffs:
        return GrowthCockpitAction(
            kind="sales_handoff",
            title="Нужно подключиться лично",
            detail=f"Открытых обращений, где требуется человек: {len(handoffs)}.",
        )
    if work:
        return GrowthCockpitAction(
            kind="sales_work",
            title="Есть обращения в работе",
            detail=f"Следующий шаг ожидает по {len(work)} обращениям.",
        )
    return GrowthCockpitAction(
        kind="none",
        title="Срочных решений нет",
        detail="В текущем контуре нет обращений, требующих действия владельца.",
    )


def _next_action(work: list[dict[str, object]], handoffs: list[dict[str, object]]) -> GrowthCockpitAction:
    if handoffs:
        return GrowthCockpitAction(
            kind="sales_handoff",
            title="ClientPlatform ждёт решения владельца",
            detail="Автоматические действия не подменяют личное решение в обращениях, переданных человеку.",
        )
    for item in work:
        action_kind = str(item.get("next_action_kind") or "").strip()
        if not action_kind:
            continue
        return GrowthCockpitAction(
            kind="sales_work",
            title="Следующий шаг уже определён",
            detail=f"ClientPlatform {_NEXT_ACTION_LABELS.get(action_kind, 'продолжит канонический сценарий обращения')}.",
        )
    return GrowthCockpitAction(
        kind="none",
        title="Следующий шаг не требуется",
        detail="Новых канонических действий по обращениям сейчас нет.",
    )


def get_growth_cockpit(
    *,
    actor: TenantContext,
    period_days: int = 7,
    now: datetime | None = None,
) -> GrowthCockpitSnapshot:
    """Compose one owner view without creating a second analytics brain.

    Revenue/conversion facts come from ``UnitEconomicsSnapshot``; sales actions
    come from the existing sales work projection; advertising counts come from
    the existing read-only Yandex growth report.  The cockpit only composes and
    explains those canonical facts.
    """

    selected_days = int(period_days)
    if selected_days not in _ALLOWED_PERIOD_DAYS:
        raise ValueError("growth cockpit period must be 7 or 30 days")
    current = _current_actor(actor)
    profile = get_business_profile(actor=current)
    selected_now = now or datetime.now(timezone.utc)
    today_from, period_from, occurred_to = _period_window(
        timezone_name=profile.timezone,
        period_days=selected_days,
        now=selected_now,
    )
    today = get_business_unit_economics(
        actor=current,
        occurred_from=today_from,
        occurred_to=occurred_to,
    )
    period = get_business_unit_economics(
        actor=current,
        occurred_from=period_from,
        occurred_to=occurred_to,
    )
    work = list_sales_work(actor=current, limit=50)
    handoffs = list_sales_handoff_work(actor=current, limit=50)
    needs_reply = sum(
        1
        for item in work
        if str(item.get("next_action_kind") or "")
        in {"respond", "ask_qualification", "present_offer", "checkout_followup"}
    )

    metrics = [
        GrowthCockpitMetric(
            key="today_leads",
            label="Новые лиды сегодня",
            value=today.leads,
            source=_REVENUE_SOURCE,
            meaning="События lead_created в каноническом outcome ledger с начала текущего дня бизнеса.",
        ),
        GrowthCockpitMetric(
            key="today_bookings",
            label="Записи сегодня",
            value=today.bookings,
            source=_REVENUE_SOURCE,
            meaning="События booking_created в каноническом outcome ledger с начала текущего дня бизнеса.",
        ),
        GrowthCockpitMetric(
            key="today_paid_customers",
            label="Оплатили сегодня",
            value=today.paid_customers,
            source=_REVENUE_SOURCE,
            meaning="Уникальные клиенты с положительным order_paid в каноническом outcome ledger сегодня.",
        ),
        GrowthCockpitMetric(
            key="period_leads",
            label=f"Лиды за {selected_days} дней",
            value=period.leads,
            source=_REVENUE_SOURCE,
            meaning=f"События lead_created за текущие {selected_days} календарных дней бизнеса.",
        ),
        GrowthCockpitMetric(
            key="period_bookings",
            label=f"Записи за {selected_days} дней",
            value=period.bookings,
            source=_REVENUE_SOURCE,
            meaning=f"События booking_created за текущие {selected_days} календарных дней бизнеса.",
        ),
        GrowthCockpitMetric(
            key="period_paid_customers",
            label=f"Оплатили за {selected_days} дней",
            value=period.paid_customers,
            source=_REVENUE_SOURCE,
            meaning=f"Уникальные клиенты с положительным order_paid за текущие {selected_days} календарных дней бизнеса.",
        ),
        GrowthCockpitMetric(
            key="sales_open_work",
            label="Обращений в работе",
            value=len(work),
            source=_SALES_SOURCE,
            meaning="Открытые tenant-scoped обращения в существующей очереди продаж.",
        ),
        GrowthCockpitMetric(
            key="sales_needs_reply",
            label="Кому нужен следующий ответ",
            value=needs_reply,
            source=_SALES_SOURCE,
            meaning="Открытые обращения, для которых канонический sales plan определил следующий клиентский шаг.",
        ),
        GrowthCockpitMetric(
            key="sales_handoffs",
            label="Нужно подключиться лично",
            value=len(handoffs),
            source="sales_ui:handoff_work",
            meaning="Открытые или взятые в работу handoff-события, где автоматизация передала решение человеку.",
        ),
    ]
    limitations = [item for item in period.limitations if item != "spend_unavailable"]

    try:
        advertising = get_yandex_growth_snapshot(
            actor=current,
            period_days=selected_days,
            now=selected_now,
        )
    except (RuntimeError, YandexDirectError):
        advertising = None
        limitations.append("advertising_data_unavailable")
    if advertising is not None:
        metrics.extend(
            [
                GrowthCockpitMetric(
                    key="advertising_accounts",
                    label="Подключено рекламных аккаунтов",
                    value=advertising.connected_accounts,
                    source=_AD_SOURCE,
                    meaning="Активные рекламные подключения, доступные каноническому read-only контуру аналитики.",
                ),
                GrowthCockpitMetric(
                    key="advertising_clicks",
                    label="Переходы из рекламы",
                    value=advertising.clicks,
                    source=_AD_SOURCE,
                    meaning=f"Подтверждённые рекламной системой клики за её {selected_days}-дневное окно отчёта.",
                ),
                GrowthCockpitMetric(
                    key="advertising_attributed_leads",
                    label="Лиды из отслеживаемой рекламы",
                    value=advertising.leads,
                    source=_AD_SOURCE,
                    meaning="Лиды, связанные с отслеживаемыми рекламными публикациями существующим attribution-контуром.",
                ),
                GrowthCockpitMetric(
                    key="advertising_attributed_bookings",
                    label="Записи из отслеживаемой рекламы",
                    value=advertising.bookings,
                    source=_AD_SOURCE,
                    meaning="Записи, связанные с отслеживаемыми рекламными публикациями существующим attribution-контуром.",
                ),
            ]
        )
        if advertising.connected_accounts > 0:
            # The current ad-connection fact does not persist a trustworthy
            # account currency.  Do not turn provider micros into fake money.
            limitations.append("advertising_spend_currency_unverified")
    else:
        limitations.append("advertising_spend_unavailable")

    if not period.attribution_complete:
        limitations.append("attribution_incomplete")
    if len(period.revenue_by_currency) > 1:
        limitations.append("revenue_mixed_currency")

    return GrowthCockpitSnapshot(
        business_id=current.business_id,
        timezone=profile.timezone,
        period_days=selected_days,
        generated_at=occurred_to,
        metrics=tuple(metrics),
        today_revenue=_money_items(
            today.revenue_by_currency,
            meaning="Атрибутированная чистая сумма денежных outcome-событий сегодня; валюты не смешиваются.",
        ),
        period_revenue=_money_items(
            period.revenue_by_currency,
            meaning=f"Атрибутированная чистая сумма денежных outcome-событий за {selected_days} дней; валюты не смешиваются.",
        ),
        what_worked=_what_worked(period.source_breakdown),
        requires_decision=_decision_action(handoffs, work),
        next_action=_next_action(work, handoffs),
        limitations=tuple(limitations),
    )


__all__ = ["get_growth_cockpit"]
