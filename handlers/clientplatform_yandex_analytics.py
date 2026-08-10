from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from clientplatform.application.yandex_growth_analytics import (
    YandexGrowthSnapshot,
    get_yandex_growth_snapshot,
)
from clientplatform.integrations.yandex_direct import YandexDirectError
from clientplatform.integrations.yandex_screen_code import (
    screen_code_configuration_available,
)

from . import clientplatform_control as control

router = Router(name="clientplatform_yandex_analytics")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())
log = logging.getLogger(__name__)


def _money(micros: int | None) -> str:
    if micros is None:
        return "—"
    return f"{int(micros) / 1_000_000:,.2f}".replace(",", " ")


def _metric(label: str, value: int | None) -> str:
    if value is None:
        return f"{label}: —"
    return f"{label}: {_money(value)}"


def _keyboard(
    business_id: str,
    period_days: int,
    *,
    connected_accounts: int,
    connect_available: bool,
) -> InlineKeyboardMarkup:
    token = control._uuid_token(business_id)
    rows: list[list[tuple[str, str]]] = []
    if connected_accounts <= 0:
        if connect_available:
            rows.append(
                [("➕ Подключить Яндекс Директ", f"cpa:connect:{token}")]
            )
    else:
        rows.append(
            [
                (
                    "7 дней" if period_days != 7 else "✅ 7 дней",
                    f"cpy:a:{token}:7",
                ),
                (
                    "30 дней" if period_days != 30 else "✅ 30 дней",
                    f"cpy:a:{token}:30",
                ),
            ]
        )
    rows.extend(
        [
            [("📣 Рекламные кабинеты", f"cpa:home:{token}")],
            [("← Получать клиентов", f"cps:s:{token}")],
        ]
    )
    return control._keyboard(rows)


def _format_snapshot(
    snapshot: YandexGrowthSnapshot,
    *,
    connect_available: bool = True,
) -> str:
    if snapshot.connected_accounts == 0:
        if connect_available:
            return (
                "📊 Яндекс Директ\n\n"
                "Рекламный кабинет ещё не подключён. Нажмите «Подключить Яндекс "
                "Директ» ниже — OAuth-токен будет храниться зашифрованно, а этот "
                "экран будет читать только подтверждённую статистику Яндекса."
            )
        return (
            "📊 Яндекс Директ\n\n"
            "Рекламный кабинет ещё не подключён. Подключение Яндекс Директа "
            "сейчас отключено или не настроено администратором. Когда безопасная "
            "OAuth-конфигурация будет готова, действие подключения появится здесь."
        )
    if snapshot.tracked_ads == 0:
        return (
            "📊 Яндекс Директ\n\n"
            f"Подключённых кабинетов: {snapshot.connected_accounts}\n\n"
            "Пока нет объявлений, созданных ClientPlatform и связанных с его "
            "измеряемыми ссылками. Поэтому расходы не смешиваются с посторонними "
            "кампаниями кабинета и CPL/CAC не придумываются."
        )

    lines = [
        "📊 Яндекс Директ",
        "",
        f"Период: {snapshot.date_from} — {snapshot.date_to}",
        "Только объявления ClientPlatform по точным Yandex AdId.",
        "",
        f"Объявлений: {snapshot.tracked_ads}",
        f"Показы: {snapshot.impressions}",
        f"Клики: {snapshot.clicks}",
        f"CTR: {snapshot.ctr_percent:.1f}%",
        f"Расход: {_money(snapshot.cost_micros)} в валюте кабинета",
        _metric("Средний CPC", snapshot.cpc_micros),
        "",
        f"Лиды по измеряемым ссылкам: {snapshot.leads}",
        f"Записались: {snapshot.bookings}",
        f"Стали клиентами: {snapshot.won}",
        _metric("CPL", snapshot.cpl_micros),
        _metric("Стоимость записи", snapshot.booking_cost_micros),
        _metric("CAC", snapshot.cac_micros),
    ]
    if snapshot.cost_micros is None:
        lines.extend(
            [
                "",
                "Денежные итоги не складываются между несколькими рекламными "
                "кабинетами: ClientPlatform пока не хранит подтверждённую валюту "
                "каждого подключения. Показы, клики и подтверждённые исходы "
                "суммируются, а общий расход/CPC/CPL/CAC остаются пустыми.",
            ]
        )
    if snapshot.campaigns:
        lines.extend(["", "По кампаниям:"])
        for campaign in snapshot.campaigns[:8]:
            lines.append(
                f"• {campaign.campaign_name[:38]} — "
                f"{_money(campaign.cost_micros)} в валюте кабинета · "
                f"{campaign.clicks} кликов · {campaign.leads} лидов · "
                f"{campaign.won} клиентов"
            )
    lines.extend(
        [
            "",
            "Расход берётся из Yandex Reports без НДС, как и существующий "
            "защитный контур бюджета. Другие объявления рекламного кабинета в "
            "эти CPL/CAC не попадают.",
            "Выручка и ROMI не показываются без подтверждённой денежной "
            "атрибуции конкретной оплаты — система не подставляет догадки.",
        ]
    )
    return "\n".join(lines)


async def _answer_feedback(
    callback: CallbackQuery,
    text: str,
    *,
    show_alert: bool = True,
) -> None:
    """Report analytics status even when interaction safety already acked the tap."""

    try:
        await callback.answer(text, show_alert=show_alert)
        return
    except TelegramAPIError:
        log.debug("Failed to answer Yandex analytics callback; using message fallback", exc_info=True)
    try:
        await control._callback_message(callback).answer(text)
    except TelegramAPIError:
        return


async def _answer_unavailable(callback: CallbackQuery) -> None:
    await _answer_feedback(
        callback,
        "Статистика Яндекса сейчас недоступна.",
        show_alert=True,
    )


async def _replace_panel(
    message: Message,
    *,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
        return
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).casefold():
            return
    except TelegramAPIError:
        log.debug("Failed to edit Yandex analytics panel; sending a new message", exc_info=True)
    await message.answer(text, reply_markup=reply_markup)


@router.callback_query(F.data.startswith("cpy:a:"))
async def open_yandex_analytics(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data).split(":")
    try:
        if len(parts) != 4:
            raise ValueError("malformed Yandex analytics callback")
        business_id = control._token_uuid(parts[2])
        period_days = int(parts[3])
        if period_days not in {7, 30}:
            raise ValueError("unsupported Yandex analytics period")
    except (IndexError, TypeError, ValueError):
        await _answer_unavailable(callback)
        return

    # Analytics period buttons are navigation, not a continuation of an ad-creation
    # wizard. Clear any stale/active FSM step before provider I/O so 7/30 always
    # gives the owner a deterministic escape from the wizard surface.
    await state.clear()

    try:
        actor = await control._actor(int(callback.from_user.id), business_id)
        snapshot = await asyncio.to_thread(
            get_yandex_growth_snapshot,
            actor=actor,
            period_days=period_days,
        )
    except YandexDirectError as exc:
        if exc.code == "analytics_report_pending":
            await _answer_feedback(
                callback,
                "Яндекс готовит отчёт. Нажмите эту кнопку ещё раз через несколько секунд.",
                show_alert=True,
            )
        else:
            await _answer_feedback(
                callback,
                "Не удалось прочитать статистику Яндекса. Проверьте подключение кабинета.",
                show_alert=True,
            )
        return
    except (PermissionError, RuntimeError, ValueError):
        await _answer_unavailable(callback)
        return

    connect_available = screen_code_configuration_available()
    # The global interaction-safety middleware acknowledges callback taps before
    # dispatch. Do not answer the same callback a second time after provider I/O.
    await _replace_panel(
        control._callback_message(callback),
        text=_format_snapshot(snapshot, connect_available=connect_available),
        reply_markup=_keyboard(
            business_id,
            period_days,
            connected_accounts=snapshot.connected_accounts,
            connect_available=connect_available,
        ),
    )


__all__ = ["open_yandex_analytics", "router"]
