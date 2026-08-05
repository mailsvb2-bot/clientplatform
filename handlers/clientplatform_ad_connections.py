from __future__ import annotations

import asyncio
from typing import Any

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from clientplatform.application.ad_connections import (
    ad_connections_enabled,
    confirm_ad_publication,
    create_ad_publication_draft,
    list_ad_connections,
    list_ad_publications,
    list_yandex_direct_campaigns,
    start_yandex_direct_oauth,
    yandex_direct_provider_configured,
)
from clientplatform.application.promotions import (
    create_slot_promotion,
    promotion_start_payload,
)
from clientplatform.domain.ad_connections import (
    AdConnectionError,
    AdConnectionStatus,
    AdPublicationStatus,
    normalize_region_ids,
)
from clientplatform.domain.bookings import BookingSlotStatus
from clientplatform.domain.promotions import PromotionChannel
from clientplatform.integrations.yandex_direct import YandexDirectError

from . import clientplatform_control as control
from . import clientplatform_simple_experience as simple


class AdConnectionState(StatesGroup):
    selecting_connection = State()
    selecting_campaign = State()
    waiting_regions = State()
    confirming_publication = State()


_STATUS_LABELS = {
    AdConnectionStatus.PENDING: "⏳ подключается",
    AdConnectionStatus.ACTIVE: "✅ подключён",
    AdConnectionStatus.ATTENTION: "⚠️ требует внимания",
    AdConnectionStatus.DISABLED: "⏸ отключён",
    AdConnectionStatus.REVOKED: "⛔ доступ отозван",
}
_JOB_LABELS = {
    AdPublicationStatus.DRAFT: "черновик",
    AdPublicationStatus.QUEUED: "в очереди",
    AdPublicationStatus.PUBLISHING: "отправляется",
    AdPublicationStatus.RETRY: "повторная попытка",
    AdPublicationStatus.SUBMITTED: "передано в Яндекс",
    AdPublicationStatus.FAILED: "ошибка",
    AdPublicationStatus.CANCELLED: "отменено",
}


def _message(callback: CallbackQuery) -> Message:
    return control._callback_message(callback)


async def _bot_username(event: CallbackQuery | Message) -> str:
    bot = await event.bot.get_me()
    username = str(getattr(bot, "username", "") or "").strip()
    if not username:
        raise RuntimeError("ClientPlatform bot requires a public username")
    return username


def _promotion_link(username: str, source_token: str) -> str:
    return f"https://t.me/{username}?start={promotion_start_payload(source_token)}"


async def _workspace(callback: CallbackQuery, *, business_token: str) -> None:
    business_id = control._token_uuid(business_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    if not ad_connections_enabled() or not yandex_direct_provider_configured():
        await callback.answer()
        await _message(callback).answer(
            "📣 Личные рекламные кабинеты\n\n"
            "Интеграция подготовлена, но OAuth-приложение Яндекс Директа ещё не "
            "включено владельцем ClientPlatform. До включения реклама продолжает "
            "работать через готовые тексты и измеряемые ссылки.",
            reply_markup=control._keyboard(
                [[("⬅️ К клиентам", f"cpj:promote:{business_token}")]]
            ),
        )
        return

    connections, slots, jobs = await asyncio.gather(
        asyncio.to_thread(list_ad_connections, actor=actor),
        asyncio.to_thread(control.list_booking_slots, actor=actor),
        asyncio.to_thread(list_ad_publications, actor=actor),
    )
    open_slots = [item for item in slots if item.slot.status == BookingSlotStatus.OPEN]
    connection_lines = [
        f"• Яндекс Директ · {item.external_login} · {_STATUS_LABELS[item.status]}"
        for item in connections
    ] or ["• рекламный кабинет пока не подключён"]
    job_lines = [
        f"• {item.external_campaign_name or item.external_campaign_id}: "
        f"{_JOB_LABELS[item.status]}"
        for item in jobs[:5]
    ] or ["• отправок пока нет"]
    rows: list[list[tuple[str, str]]] = [
        [("➕ Подключить Яндекс Директ", f"cpa:connect:{business_token}")]
    ]
    if any(item.status == AdConnectionStatus.ACTIVE for item in connections):
        rows.extend(
            [
                [
                    (
                        f"🎯 {slot.local_start} · {slot.offering_title[:20]}",
                        f"cpa:slot:{business_token}:{control._uuid_token(slot.slot.id)}",
                    )
                ]
                for slot in open_slots[:10]
            ]
        )
    rows.extend(
        [
            [("🔄 Обновить", f"cpa:home:{business_token}")],
            [("⬅️ Получить клиентов", f"cpj:promote:{business_token}")],
        ]
    )
    await callback.answer()
    await _message(callback).answer(
        "📣 Личные рекламные кабинеты\n\n"
        "Каждый бизнес подключает собственный кабинет. ClientPlatform не получает "
        "доступ к кабинетам других пользователей и не запускает расходы без "
        "отдельного подтверждения.\n\n"
        "Подключения:\n"
        + "\n".join(connection_lines)
        + "\n\nПоследние отправки:\n"
        + "\n".join(job_lines)
        + ("\n\nВыберите свободное время для рекламы:" if open_slots else "\n\nСначала опубликуйте свободное время."),
        reply_markup=control._keyboard(rows),
    )


@simple.router.callback_query(F.data.startswith("cpa:home:"))
async def open_ad_connections(callback: CallbackQuery) -> None:
    await _workspace(
        callback,
        business_token=str(callback.data).split(":", 2)[2],
    )


@simple.router.callback_query(F.data.startswith("cpa:connect:"))
async def connect_yandex_direct(callback: CallbackQuery) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    business_id = control._token_uuid(business_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        start = await asyncio.to_thread(start_yandex_direct_oauth, actor=actor)
    except (AdConnectionError, YandexDirectError, RuntimeError, ValueError):
        await callback.answer("Не удалось начать подключение", show_alert=True)
        return
    await callback.answer()
    await _message(callback).answer(
        "🔐 Подключение Яндекс Директа\n\n"
        "Откроется официальный экран Яндекса. Выберите нужный аккаунт и разрешите "
        "доступ. Пароль ClientPlatform не получает. Ссылка действует 10 минут.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Открыть Яндекс и подключить кабинет",
                        url=start.authorization_url,
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Вернуться",
                        callback_data=f"cpa:home:{business_token}",
                    )
                ],
            ]
        ),
    )


@simple.router.callback_query(F.data.startswith("cpa:slot:"))
async def choose_ad_connection(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    _, _, business_token, slot_token = str(callback.data).split(":", 3)
    business_id = control._token_uuid(business_token)
    slot_id = control._token_uuid(slot_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    connections = await asyncio.to_thread(list_ad_connections, actor=actor)
    active = [item for item in connections if item.status == AdConnectionStatus.ACTIVE]
    if not active:
        await callback.answer("Сначала подключите рекламный кабинет", show_alert=True)
        return
    view = await asyncio.to_thread(
        create_slot_promotion,
        actor=actor,
        slot_id=slot_id,
        channel=PromotionChannel.WEBSITE,
    )
    username = await _bot_username(callback)
    await state.set_state(AdConnectionState.selecting_connection)
    await state.set_data(
        {
            "business_id": business_id,
            "business_token": business_token,
            "promotion_campaign_id": view.campaign.id,
            "source_url": _promotion_link(username, view.campaign.source_token),
            "connection_ids": [item.id for item in active],
        }
    )
    rows = [
        [(f"Яндекс · {item.external_login}", f"cpa:conn:{index}")]
        for index, item in enumerate(active)
    ]
    rows.append([("Отмена", f"cpa:home:{business_token}")])
    await callback.answer()
    await _message(callback).answer(
        "Какой личный рекламный кабинет использовать?",
        reply_markup=control._keyboard(rows),
    )


@simple.router.callback_query(
    AdConnectionState.selecting_connection,
    F.data.startswith("cpa:conn:"),
)
async def choose_yandex_campaign(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    try:
        index = int(str(callback.data).split(":", 2)[2])
        connection_id = list(data["connection_ids"])[index]
        actor = await control._actor(
            int(callback.from_user.id),
            str(data["business_id"]),
        )
        campaigns = await asyncio.to_thread(
            list_yandex_direct_campaigns,
            actor=actor,
            connection_id=connection_id,
        )
    except (IndexError, KeyError, TypeError, ValueError, AdConnectionError, YandexDirectError):
        await callback.answer("Не удалось получить кампании Яндекса", show_alert=True)
        return
    eligible = [item for item in campaigns if item.state != "ARCHIVED"][:20]
    if not eligible:
        await callback.answer("В кабинете нет подходящей текстовой кампании", show_alert=True)
        return
    await state.update_data(
        connection_id=connection_id,
        yandex_campaigns=[
            {"id": item.campaign_id, "name": item.name}
            for item in eligible
        ],
    )
    await state.set_state(AdConnectionState.selecting_campaign)
    rows = [
        [(item.name[:45], f"cpa:campaign:{index}")]
        for index, item in enumerate(eligible)
    ]
    rows.append([("Отмена", f"cpa:home:{data['business_token']}")])
    await callback.answer()
    await _message(callback).answer(
        "В какую существующую кампанию Яндекс Директа добавить объявление?\n\n"
        "ClientPlatform не меняет бюджет и стратегию выбранной кампании.",
        reply_markup=control._keyboard(rows),
    )


@simple.router.callback_query(
    AdConnectionState.selecting_campaign,
    F.data.startswith("cpa:campaign:"),
)
async def request_ad_regions(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        index = int(str(callback.data).split(":", 2)[2])
        selected = list(data["yandex_campaigns"])[index]
    except (IndexError, KeyError, TypeError, ValueError):
        await callback.answer("Кампания больше не найдена", show_alert=True)
        return
    await state.update_data(
        external_campaign_id=str(selected["id"]),
        external_campaign_name=str(selected["name"]),
    )
    await state.set_state(AdConnectionState.waiting_regions)
    await callback.answer()
    await _message(callback).answer(
        "Укажите регион показа — один или несколько ID через запятую.\n\n"
        "Частые варианты:\n"
        "• Нижний Новгород — 47\n"
        "• Москва — 213\n"
        "• Санкт-Петербург — 2\n\n"
        "Показы по всей стране автоматически не включаются: география должна быть "
        "задана явно."
    )


@simple.router.message(AdConnectionState.waiting_regions)
async def prepare_ad_publication(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        regions = normalize_region_ids(str(message.text or ""))
        actor = await control._actor(
            control._user_id(message),
            str(data["business_id"]),
        )
        draft = await asyncio.to_thread(
            create_ad_publication_draft,
            actor=actor,
            promotion_campaign_id=str(data["promotion_campaign_id"]),
            connection_id=str(data["connection_id"]),
            external_campaign_id=str(data["external_campaign_id"]),
            external_campaign_name=str(data["external_campaign_name"]),
            region_ids=regions,
            source_url=str(data["source_url"]),
        )
    except (KeyError, TypeError, ValueError, AdConnectionError):
        await message.answer(
            "Не удалось распознать регион. Введите положительный ID, например 47, "
            "или несколько ID через запятую."
        )
        return
    await state.update_data(job_id=draft.job.id)
    await state.set_state(AdConnectionState.confirming_publication)
    await message.answer(
        "Проверьте перед отправкой:\n\n"
        f"Кампания: {draft.campaign_name}\n"
        f"Регионы: {', '.join(str(item) for item in draft.job.region_ids)}\n"
        f"Заголовок: {draft.job.title}\n"
        f"Текст: {draft.job.text}\n"
        f"Ссылка: {draft.job.source_url}\n\n"
        "После подтверждения ClientPlatform создаст группу и объявление в Вашем "
        "кабинете. Бюджет и стратегия существующей кампании не изменяются.",
        reply_markup=control._keyboard(
            [
                [("✅ Отправить в Яндекс Директ", "cpa:confirm")],
                [("Отмена", f"cpa:home:{data['business_token']}")],
            ]
        ),
    )


@simple.router.callback_query(
    AdConnectionState.confirming_publication,
    F.data == "cpa:confirm",
)
async def confirm_yandex_publication(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    try:
        actor = await control._actor(
            int(callback.from_user.id),
            str(data["business_id"]),
        )
        job = await asyncio.to_thread(
            confirm_ad_publication,
            actor=actor,
            job_id=str(data["job_id"]),
        )
    except (KeyError, AdConnectionError, RuntimeError, ValueError):
        await callback.answer("Не удалось поставить объявление в очередь", show_alert=True)
        return
    await state.clear()
    await callback.answer("Объявление принято")
    await _message(callback).answer(
        "✅ Объявление поставлено в защищённую очередь\n\n"
        f"Статус: {_JOB_LABELS[job.status]}\n"
        "ClientPlatform отправит его в личный кабинет идемпотентно: повторное "
        "нажатие не создаст второе расходующее деньги объявление.",
        reply_markup=control._keyboard(
            [[("📣 Открыть рекламные кабинеты", f"cpa:home:{data['business_token']}")]]
        ),
    )


__all__ = [
    "AdConnectionState",
    "confirm_yandex_publication",
    "connect_yandex_direct",
    "open_ad_connections",
    "prepare_ad_publication",
]
