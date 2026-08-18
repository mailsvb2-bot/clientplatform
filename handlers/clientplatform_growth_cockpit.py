from __future__ import annotations

"""Owner-facing Growth Cockpit over the canonical ClientPlatform read models."""

import asyncio
import os

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from clientplatform.application.growth_cockpit import get_growth_cockpit
from clientplatform.domain.growth_cockpit import GrowthCockpitAction, GrowthCockpitMoney

from . import clientplatform_control as control

router = Router(name="clientplatform_growth_cockpit")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())

_LIMITATION_COPY = {
    "attribution_incomplete": "Часть оплат пока нельзя надёжно связать с источником — вывод «что сработало» неполный.",
    "revenue_mixed_currency": "Есть денежные события в разных валютах; ClientPlatform не складывает их в одну ложную сумму.",
    "advertising_data_unavailable": "Рекламная статистика сейчас недоступна; бизнес-результаты выше продолжают считаться по внутренним подтверждённым событиям.",
    "advertising_spend_currency_unverified": "Расход рекламы не показывается как деньги, пока валюта рекламного аккаунта не подтверждена надёжным источником.",
    "advertising_spend_unavailable": "Подтверждённого рекламного расхода для этого экрана пока нет.",
}


def _token(value: str) -> str:
    return control._uuid_token(value)


def _uuid(value: str) -> str:
    return control._token_uuid(value)


def _metric(snapshot, key: str) -> int:
    return snapshot.metric(key).value


def _money_text(items: tuple[GrowthCockpitMoney, ...]) -> str:
    if not items:
        return "пока нет подтверждённых денежных событий"
    rendered: list[str] = []
    for item in items:
        sign = "-" if item.amount_minor < 0 else ""
        absolute = abs(item.amount_minor)
        rendered.append(f"{sign}{absolute // 100:,}.{absolute % 100:02d} {item.currency}".replace(",", " "))
    return " · ".join(rendered)


def _action_callback(action: GrowthCockpitAction, business_id: str) -> str | None:
    token = _token(business_id)
    if action.kind == "sales_handoff":
        return f"cps:sh:{token}"
    if action.kind == "sales_work":
        return f"cps:sw:{token}"
    return None


def _webapp_url(business_id: str) -> str | None:
    domain = (os.getenv("CLIENTPLATFORM_DOMAIN") or "").strip().strip("/")
    if not domain:
        return None
    return f"https://{domain}/dashboard/growth?business={business_id}"


def _keyboard(snapshot) -> InlineKeyboardMarkup:
    business_id = snapshot.business_id
    token = _token(business_id)
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=("✓ 7 дней" if snapshot.period_days == 7 else "7 дней"),
                callback_data=f"cpg:v:{token}:7",
            ),
            InlineKeyboardButton(
                text=("✓ 30 дней" if snapshot.period_days == 30 else "30 дней"),
                callback_data=f"cpg:v:{token}:30",
            ),
        ]
    ]
    decision_callback = _action_callback(snapshot.requires_decision, business_id)
    if decision_callback:
        rows.append(
            [InlineKeyboardButton(text="Перейти к следующему шагу", callback_data=decision_callback)]
        )
    webapp_url = _webapp_url(business_id)
    if webapp_url:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Открыть полный Growth Cockpit",
                    web_app=WebAppInfo(url=webapp_url),
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="← В кабинет", callback_data=f"cp:business:{token}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _render(snapshot) -> str:
    today_revenue = _money_text(snapshot.today_revenue)
    period_revenue = _money_text(snapshot.period_revenue)
    lines = [
        "📈 Что сегодня происходит с бизнесом",
        "",
        "Сегодня",
        f"• Новые лиды: {_metric(snapshot, 'today_leads')}",
        f"• Записи: {_metric(snapshot, 'today_bookings')}",
        f"• Оплатили: {_metric(snapshot, 'today_paid_customers')}",
        f"• Деньги: {today_revenue}",
        "",
        f"За {snapshot.period_days} дней",
        f"• Лиды: {_metric(snapshot, 'period_leads')}",
        f"• Записи: {_metric(snapshot, 'period_bookings')}",
        f"• Оплатили: {_metric(snapshot, 'period_paid_customers')}",
        f"• Деньги: {period_revenue}",
        "",
        "Обращения",
        f"• В работе: {_metric(snapshot, 'sales_open_work')}",
        f"• Кому нужен следующий ответ: {_metric(snapshot, 'sales_needs_reply')}",
        f"• Нужно подключиться лично: {_metric(snapshot, 'sales_handoffs')}",
    ]
    metric_keys = {item.key for item in snapshot.metrics}
    if "advertising_accounts" in metric_keys:
        lines.extend(
            [
                "",
                "Реклама",
                f"• Подключено аккаунтов: {_metric(snapshot, 'advertising_accounts')}",
                f"• Переходы: {_metric(snapshot, 'advertising_clicks')}",
                f"• Лиды из отслеживаемой рекламы: {_metric(snapshot, 'advertising_attributed_leads')}",
                f"• Записи из отслеживаемой рекламы: {_metric(snapshot, 'advertising_attributed_bookings')}",
            ]
        )
    lines.extend(
        [
            "",
            "Что сработало",
            snapshot.what_worked,
            "",
            "Что требует решения",
            f"{snapshot.requires_decision.title}. {snapshot.requires_decision.detail}",
            "",
            "Что ClientPlatform сделает дальше",
            f"{snapshot.next_action.title}. {snapshot.next_action.detail}",
        ]
    )
    visible_limitations = [
        _LIMITATION_COPY[item]
        for item in snapshot.limitations
        if item in _LIMITATION_COPY
    ]
    if visible_limitations:
        lines.extend(["", "Ограничения данных"])
        lines.extend(f"• {item}" for item in visible_limitations)
    return "\n".join(lines)


async def send_growth_cockpit(
    message: Message,
    *,
    user_id: int,
    business_id: str,
    period_days: int = 7,
) -> None:
    actor = await control._actor(user_id, business_id)
    snapshot = await asyncio.to_thread(
        get_growth_cockpit,
        actor=actor,
        period_days=period_days,
    )
    await message.answer(_render(snapshot), reply_markup=_keyboard(snapshot))


@router.callback_query(F.data.startswith("cpg:v:"))
async def open_growth_cockpit(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data).split(":")
    if len(parts) != 4 or parts[3] not in {"7", "30"}:
        await callback.answer("Не удалось определить период", show_alert=True)
        return
    business_id = _uuid(parts[2])
    await state.clear()
    await callback.answer()
    await send_growth_cockpit(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
        period_days=int(parts[3]),
    )


__all__ = ["_render", "router", "send_growth_cockpit"]
