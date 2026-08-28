from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Any

from clientplatform.application.growth_cockpit import GrowthCockpitSnapshot, acquisition_source_label
from clientplatform.domain.money import settlement_currency_minor_unit_exponent

_LIMITATION_LABELS = {
    "attribution_incomplete": "Часть оплат пока нельзя надёжно связать с источником клиента.",
    "revenue_mixed_currency": "Выручка есть в нескольких валютах и показывается раздельно.",
    "spend_unavailable": "Подтверждённая стоимость привлечения пока недоступна.",
    "roas_revenue_unavailable": "ROAS не рассчитывается без однозначной выручки.",
    "spend_currency_mismatch": "Валюта рекламных расходов и выручки не совпадает.",
    "advertising_unavailable": "Данные рекламы сейчас временно недоступны.",
    "advertising_currency_unverified": (
        "Стоимость рекламы скрыта до подтверждения ISO-валюты рекламного подключения."
    ),
    "verified_revenue_mixed_currency": (
        "Подтверждённая выручка есть в нескольких валютах и показывается раздельно."
    ),
    "journey_source_incomplete": (
        "Для части лидов или записей источник клиента пока не подтверждён."
    ),
    "booking_completion_unavailable": (
        "Посещения пока не подтверждаются отдельным каноническим событием; "
        "этап «пришли» не рассчитывается."
    ),
}


def _minor_money_text(amount_minor: int, currency: str) -> str:
    """Render canonical minor-unit money using the ISO-4217 currency exponent."""

    exponent = settlement_currency_minor_unit_exponent(currency)
    amount = Decimal(int(amount_minor)) / (Decimal(10) ** exponent)
    rendered = f"{amount:,.{exponent}f}" if exponent else f"{amount:,.0f}"
    return f"{rendered.replace(',', ' ')} {str(currency).upper()}"


def _metric_map(snapshot: GrowthCockpitSnapshot, *, today: bool) -> dict[str, int]:
    rows = snapshot.today_metrics if today else snapshot.period_metrics
    return {row.key: row.value for row in rows}


def _money_breakdown_text(rows: object, *, empty: str) -> str:
    values = tuple(rows or ())
    if not values:
        return empty
    return ", ".join(_minor_money_text(item.amount_minor, item.currency) for item in values)


def _best_source_text(snapshot: GrowthCockpitSnapshot) -> str:
    known = [item for item in snapshot.journey.sources if item.source.value != "unknown"]
    if not known:
        return "пока недостаточно подтверждённых данных"
    item = known[0]
    label = acquisition_source_label(item.source)
    money = _money_breakdown_text(item.revenue_by_currency, empty="выручка пока не подтверждена")
    return f"{label} — {money} · оплативших: {item.paid_customers}"


def telegram_growth_summary(snapshot: GrowthCockpitSnapshot) -> str:
    """Compact owner copy: money and customer journey first, provider jargon hidden."""

    today = _metric_map(snapshot, today=True)
    journey = snapshot.journey
    verified_revenue = _money_breakdown_text(
        journey.verified_revenue_by_currency,
        empty="пока нет подтверждённой выручки",
    )
    attributed_revenue = _money_breakdown_text(
        journey.attributed_revenue_by_currency,
        empty="пока не связана с источниками",
    )
    unattributed_revenue = _money_breakdown_text(
        journey.unattributed_revenue_by_currency,
        empty="0",
    )

    advertising = "не подключена или нет данных"
    if snapshot.advertising is not None:
        advertising = (
            f"{snapshot.advertising.leads} лидов · "
            f"{snapshot.advertising.bookings} записей · "
            f"{snapshot.advertising.won} продаж · "
            "стоимость скрыта до подтверждения валюты"
        )

    attention = "\n".join(f"• {item}" for item in snapshot.attention) or "• Срочных сигналов нет."
    actions = tuple(getattr(snapshot, "actions", ()))[:5]
    action_queue = (
        "\n".join(
            f"{index}. {item.title}\n   {item.reason}"
            for index, item in enumerate(actions, start=1)
        )
        if actions
        else "• Срочных действий нет."
    )
    limitations = [
        _LIMITATION_LABELS.get(item, item)
        for item in snapshot.limitations
        if item in _LIMITATION_LABELS
    ]
    limitation_text = ""
    if limitations:
        limitation_text = "\n\nЧто пока нельзя утверждать точно:\n" + "\n".join(
            f"• {item}" for item in limitations
        )

    completion_text = (
        "нет подтверждённых данных"
        if "booking_completion_unavailable" in getattr(journey, "limitations", ())
        else str(journey.completed_bookings)
    )

    return (
        "📈 Что происходит с бизнесом\n\n"
        "Сегодня\n"
        f"• Новые лиды: {today.get('leads', 0)}\n"
        f"• Записи: {today.get('bookings', 0)}\n"
        f"• Оплатившие клиенты: {today.get('paid_customers', 0)}\n"
        f"• Кому нужно ответить: {snapshot.needs_reply}\n\n"
        f"Деньги и путь клиента · {snapshot.period_days} дней\n"
        f"• Лиды: {journey.leads} → записи: {journey.bookings} → "
        f"пришли: {completion_text} → оплатили: {journey.paid_customers}\n"
        f"• Вернувшиеся клиенты: {journey.reactivated_customers}\n"
        f"• Подтверждённая выручка: {verified_revenue}\n"
        f"• Связано с источником: {attributed_revenue}\n"
        f"• Без подтверждённого источника: {unattributed_revenue}\n"
        f"• Лучший подтверждённый источник: {_best_source_text(snapshot)}\n"
        f"• Реклама: {advertising}\n\n"
        "Что требует решения\n"
        f"{attention}\n\n"
        "Важные действия\n"
        f"{action_queue}\n\n"
        "Главное действие\n"
        f"• {snapshot.next_action.title}\n"
        f"  {snapshot.next_action.reason}"
        f"{limitation_text}"
    )


def growth_cockpit_payload(snapshot: GrowthCockpitSnapshot) -> dict[str, Any]:
    """Stable full-dashboard payload with source and meaning on every metric."""

    payload = asdict(snapshot)
    payload["as_of"] = snapshot.as_of.isoformat()
    payload["period_from"] = snapshot.period_from.isoformat()
    payload["period_to"] = snapshot.period_to.isoformat()
    payload["today_from"] = snapshot.today_from.isoformat()
    payload["today_to"] = snapshot.today_to.isoformat()
    payload["journey"]["occurred_from"] = snapshot.journey.occurred_from.isoformat()
    payload["journey"]["occurred_to"] = snapshot.journey.occurred_to.isoformat()
    payload["journey"]["source"] = "canonical_outcome_revenue_projection"
    payload["journey"]["meaning"] = (
        "Путь клиента и деньги собраны из существующих outcome, attribution, booking, "
        "payment и retention facts без отдельного хранилища."
    )
    if snapshot.advertising is not None:
        payload["advertising"] = {
            "period_days": snapshot.advertising.period_days,
            "date_from": snapshot.advertising.date_from,
            "date_to": snapshot.advertising.date_to,
            "connected_accounts": snapshot.advertising.connected_accounts,
            "tracked_ads": snapshot.advertising.tracked_ads,
            "impressions": snapshot.advertising.impressions,
            "clicks": snapshot.advertising.clicks,
            "leads": snapshot.advertising.leads,
            "bookings": snapshot.advertising.bookings,
            "won": snapshot.advertising.won,
            "cost": None,
            "source": "verified_yandex_direct_report",
            "meaning": (
                "Read-only рекламные факты и локально подтверждённые результаты; "
                "денежная стоимость не публикуется до подтверждения ISO-валюты подключения."
            ),
        }
    payload["limitations_human"] = [
        _LIMITATION_LABELS.get(item, item) for item in snapshot.limitations
    ]
    return payload


__all__ = ["growth_cockpit_payload", "telegram_growth_summary"]
