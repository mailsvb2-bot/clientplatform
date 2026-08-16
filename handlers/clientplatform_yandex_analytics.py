from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from clientplatform.application.yandex_campaign_diagnostics import (
    YandexCampaignDiagnosticsSnapshot,
    get_yandex_campaign_diagnostics,
)
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
    campaign_mode: bool = False,
) -> InlineKeyboardMarkup:
    token = control._uuid_token(business_id)
    rows: list[list[tuple[str, str]]] = []
    if connected_accounts <= 0:
        if connect_available:
            rows.append(
                [("➕ Подключить Яндекс Директ", f"cpa:connect:{token}")]
            )
    else:
        prefix = "c" if campaign_mode else "a"
        rows.append(
            [
                (
                    "7 дней" if period_days != 7 else "✅ 7 дней",
                    f"cpy:{prefix}:{token}:7",
                ),
                (
                    "30 дней" if period_days != 30 else "✅ 30 дней",
                    f"cpy:{prefix}:{token}:30",
                ),
            ]
        )
        if campaign_mode:
            rows.append(
                [("🎯 Exact AdId + результаты", f"cpy:a:{token}:{period_days}")]
            )
        else:
            rows.append(
                [("📈 Кампании по CampaignId", f"cpy:c:{token}:{period_days}")]
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


def _format_campaign_snapshot(snapshot: YandexCampaignDiagnosticsSnapshot) -> str:
    if snapshot.connected_accounts == 0:
        return (
            "📈 Яндекс Директ — кампании\n\n"
            "Рекламный кабинет ещё не подключён."
        )
    if snapshot.managed_campaigns == 0:
        return (
            "📈 Яндекс Директ — кампании\n\n"
            f"Подключённых кабинетов: {snapshot.connected_accounts}\n\n"
            "Пока нет кампаний, которыми ClientPlatform уже управляет или для "
            "которых готовил публикацию."
        )

    lines = [
        "📈 Яндекс Директ — CampaignId диагностика",
        "",
        f"Период: {snapshot.date_from} — {snapshot.date_to}",
        "Read-only статистика Yandex Reports по управляемым CampaignId.",
        "Это диагностика рекламной кампании, а не атрибуция лидов, записей или выручки.",
        "",
        f"Кампаний: {snapshot.managed_campaigns}",
        f"Показы: {snapshot.impressions}",
        f"Клики: {snapshot.clicks}",
        f"CTR: {snapshot.ctr_percent:.1f}%",
        f"Расход: {_money(snapshot.cost_micros)} в валюте кабинета",
        _metric("Средний CPC", snapshot.cpc_micros),
        "",
        "По CampaignId:",
    ]
    for campaign in snapshot.campaigns[:12]:
        provider_note = "" if campaign.has_provider_row else " · пока 0 строк в отчёте"
        lines.append(
            f"• {campaign.campaign_name[:34]} [{campaign.campaign_id}] — "
            f"{campaign.impressions} показов · {campaign.clicks} кликов · "
            f"{_money(campaign.cost_micros)} в валюте кабинета{provider_note}"
        )
    if snapshot.cost_micros is None:
        lines.extend(
            [
                "",
                "Общий денежный итог скрыт: кампании относятся к нескольким "
                "рекламным кабинетам, а подтверждённая валюта подключения пока "
                "не хранится. Показы и клики можно суммировать; деньги между "
                "кабинетами — нельзя.",
            ]
        )
    lines.extend(
        [
            "",
            "Кампания остаётся видимой даже до появления AdId: отсутствие строки "
            "в отчёте означает нулевую наблюдаемую доставку, а не исчезновение "
            "управляемого CampaignId.",
            "Для CPL/CAC и бизнес-результатов используется отдельный точный AdId "
            "контур; CampaignId-расход к выручке автоматически не приписывается.",
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


def _parse_callback(callback: CallbackQuery, *, expected_mode: str) -> tuple[str, int]:
    parts = str(callback.data).split(":")
    if len(parts) != 4 or parts[1] != expected_mode:
        raise ValueError("malformed Yandex analytics callback")
    business_id = control._token_uuid(parts[2])
    period_days = int(parts[3])
    if period_days not in {7, 30}:
        raise ValueError("unsupported Yandex analytics period")
    return business_id, period_days


@router.callback_query(F.data.startswith("cpy:a:"))
async def open_yandex_analytics(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        business_id, period_days = _parse_callback(callback, expected_mode="a")
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


@router.callback_query(F.data.startswith("cpy:c:"))
async def open_yandex_campaign_diagnostics(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    try:
        business_id, period_days = _parse_callback(callback, expected_mode="c")
    except (IndexError, TypeError, ValueError):
        await _answer_unavailable(callback)
        return

    await state.clear()
    try:
        actor = await control._actor(int(callback.from_user.id), business_id)
        snapshot = await asyncio.to_thread(
            get_yandex_campaign_diagnostics,
            actor=actor,
            period_days=period_days,
        )
    except YandexDirectError as exc:
        if exc.code == "analytics_report_pending":
            await _answer_feedback(
                callback,
                "Яндекс готовит отчёт по кампаниям. Нажмите ещё раз через несколько секунд.",
                show_alert=True,
            )
        else:
            await _answer_feedback(
                callback,
                "Не удалось прочитать CampaignId-статистику Яндекса. Проверьте подключение кабинета.",
                show_alert=True,
            )
        return
    except (PermissionError, RuntimeError, ValueError):
        await _answer_unavailable(callback)
        return

    connect_available = screen_code_configuration_available()
    await _replace_panel(
        control._callback_message(callback),
        text=_format_campaign_snapshot(snapshot),
        reply_markup=_keyboard(
            business_id,
            period_days,
            connected_accounts=snapshot.connected_accounts,
            connect_available=connect_available,
            campaign_mode=True,
        ),
    )


__all__ = [
    "open_yandex_analytics",
    "open_yandex_campaign_diagnostics",
    "router",
]
