from __future__ import annotations

from dataclasses import asdict
from typing import Any

from clientplatform.application.growth_cockpit import GrowthCockpitSnapshot

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
}


def _minor_money_text(amount_minor: int, currency: str) -> str:
    """Render canonical minor-unit money using the established two-decimal UI convention."""

    sign = "-" if amount_minor < 0 else ""
    absolute = abs(int(amount_minor))
    return f"{sign}{absolute // 100:,}.{absolute % 100:02d} {currency}".replace(",", " ")


def _metric_map(snapshot: GrowthCockpitSnapshot, *, today: bool) -> dict[str, int]:
    rows = snapshot.today_metrics if today else snapshot.period_metrics
    return {row.key: row.value for row in rows}


def telegram_growth_summary(snapshot: GrowthCockpitSnapshot) -> str:
    """Compact owner copy: business meaning first, provider jargon hidden."""

    today = _metric_map(snapshot, today=True)
    period = _metric_map(snapshot, today=False)
    revenue = ", ".join(
        _minor_money_text(item.amount_minor, item.currency) for item in snapshot.revenue
    ) or "пока нет подтверждённой выручки"
    worked = ", ".join(
        f"{item.label}: {item.outcomes}" for item in snapshot.what_worked[:3]
    ) or "пока недостаточно данных"

    advertising = "не подключена или нет данных"
    if snapshot.advertising is not None:
        advertising = (
            f"{snapshot.advertising.leads} лидов · "
            f"{snapshot.advertising.bookings} записей · "
            f"{snapshot.advertising.won} продаж · "
            "стоимость скрыта до подтверждения валюты"
        )

    attention = "\n".join(f"• {item}" for item in snapshot.attention) or "• Срочных сигналов нет."
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

    return (
        "📈 Что происходит с бизнесом\n\n"
        "Сегодня\n"
        f"• Новые лиды: {today.get('leads', 0)}\n"
        f"• Записи: {today.get('bookings', 0)}\n"
        f"• Оплатившие клиенты: {today.get('paid_customers', 0)}\n"
        f"• Кому нужно ответить: {snapshot.needs_reply}\n\n"
        f"За {snapshot.period_days} дней\n"
        f"• Лиды: {period.get('leads', 0)}\n"
        f"• Записи: {period.get('bookings', 0)}\n"
        f"• Оплатившие клиенты: {period.get('paid_customers', 0)}\n"
        f"• Подтверждённая выручка: {revenue}\n"
        f"• Реклама: {advertising}\n"
        f"• Что сработало: {worked}\n\n"
        "Что требует решения\n"
        f"{attention}\n\n"
        "Что ClientPlatform сделает дальше\n"
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
