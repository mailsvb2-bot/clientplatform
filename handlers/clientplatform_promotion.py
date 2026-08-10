from __future__ import annotations

"""Owner-facing promotion workspace over canonical ClientPlatform bookings."""

import asyncio
from typing import Any
from urllib.parse import quote

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from clientplatform.application.booking_reminders import schedule_booking_reminders
from clientplatform.application.pagination import paginate
from clientplatform.application.promotions import (
    book_promoted_slot,
    create_slot_promotion,
    list_promotable_slots,
    list_promotion_campaigns,
    open_promotion_link,
    parse_promotion_start_payload,
    promotion_start_payload,
    promotion_stats,
)
from clientplatform.domain.booking_calendar import (
    booking_calendar_filename,
    booking_calendar_ics,
    google_calendar_url,
)
from clientplatform.domain.promotions import PromotionChannel

from . import clientplatform_control as control
from . import clientplatform_simple_experience as simple


_CHANNEL_LABELS = {
    PromotionChannel.TELEGRAM: "Telegram",
    PromotionChannel.VK: "ВКонтакте",
    PromotionChannel.WHATSAPP: "WhatsApp",
    PromotionChannel.WEBSITE: "Сайт и объявления",
    PromotionChannel.OFFLINE: "Офлайн-материалы",
}
_CHANNEL_BUTTONS = (
    (PromotionChannel.TELEGRAM, "✈️ Telegram"),
    (PromotionChannel.VK, "🔵 ВКонтакте"),
    (PromotionChannel.WHATSAPP, "🟢 WhatsApp"),
    (PromotionChannel.WEBSITE, "🌐 Сайт/объявление"),
    (PromotionChannel.OFFLINE, "📄 Офлайн-материалы"),
)
_SLOT_LIMIT = 12


def _campaign_link(username: str, source_token: str) -> str:
    return f"https://t.me/{username}?start={promotion_start_payload(source_token)}"


async def _bot_username(event: CallbackQuery | Message) -> str:
    bot = await event.bot.get_me()
    username = str(getattr(bot, "username", "") or "").strip()
    if not username:
        raise RuntimeError("ClientPlatform bot requires a public username")
    return username


def _creative_text(view: Any, link: str) -> str:
    creative = view.campaign.creative
    return (
        f"{creative.headline}\n\n"
        f"{creative.primary_text}\n\n"
        f"{creative.description}\n\n"
        f"Записаться: {link}"
    )


async def _render_promotion_workspace(
    callback: CallbackQuery,
    *,
    business_token: str,
    page: object = 0,
) -> None:
    business_id = control._token_uuid(business_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    open_slots, stats, campaigns = await asyncio.gather(
        asyncio.to_thread(list_promotable_slots, actor=actor),
        asyncio.to_thread(promotion_stats, actor=actor),
        asyncio.to_thread(list_promotion_campaigns, actor=actor),
    )
    active_campaigns = sum(
        1 for item in campaigns if item.campaign.status.value == "active"
    )
    current = paginate(open_slots, page, page_size=_SLOT_LIMIT)
    lines = (
        f"Рекламных ссылок: {stats.campaigns}\n"
        f"Сейчас активны: {active_campaigns}\n"
        f"Уникальных людей: {stats.people_opened}\n"
        f"Записались: {stats.bookings}\n"
        f"Конверсия в запись: {stats.conversion_percent:.1f}%"
    )
    rows = [
        [
            (
                f"🚀 {slot.local_start} · {slot.offering_title[:20]}",
                f"cpp:slot:{business_token}:{control._uuid_token(slot.slot.id)}",
            )
        ]
        for slot in current.items
    ]
    navigation: list[tuple[str, str]] = []
    if current.has_previous:
        navigation.append(("⬅️ Назад", f"cpj:promote:{business_token}:{current.index - 1}"))
    if current.has_next:
        navigation.append(("Вперёд ➡️", f"cpj:promote:{business_token}:{current.index + 1}"))
    if navigation:
        rows.append(navigation)
    rows.extend(
        [
            [("📊 Обновить результат", f"cpp:stats:{business_token}")],
            [("📅 Мой календарь", f"cpj:calendar:{business_token}:30")],
            [("🏠 В кабинет", f"cpj:home:{business_token}")],
        ]
    )
    instruction = (
        "Выберите свободное время. ClientPlatform подготовит безопасное "
        "объявление и отдельную ссылку, по которой будут считаться переходы и записи."
        if open_slots
        else "Для новой рекламы сначала опубликуйте свободное время. "
        "Статистика ранее созданных ссылок сохранена ниже."
    )
    await callback.answer()
    await control._callback_message(callback).answer(
        f"🚀 Получить клиентов\n\n{instruction}\n\n"
        f"Страница {current.index + 1}/{current.count}\n\n📊 Результат\n{lines}",
        reply_markup=control._keyboard(rows),
    )


async def open_promotion_workspace(callback: CallbackQuery) -> None:
    parts = str(callback.data or "").split(":")
    if len(parts) not in {3, 4}:
        await callback.answer("Кнопка устарела. Откройте продвижение заново.", show_alert=True)
        return
    _, _, business_token, *raw_page = parts
    await _render_promotion_workspace(
        callback,
        business_token=business_token,
        page=raw_page[0] if raw_page else 0,
    )


@simple.router.callback_query(F.data.startswith("cpp:stats:"))
async def refresh_promotion_stats(callback: CallbackQuery) -> None:
    await _render_promotion_workspace(
        callback,
        business_token=str(callback.data).split(":", 2)[2],
    )


@simple.router.callback_query(F.data.startswith("cpp:slot:"))
async def choose_promotion_channel(callback: CallbackQuery) -> None:
    _, _, business_token, slot_token = str(callback.data).split(":", 3)
    business_id = control._token_uuid(business_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    slots = await asyncio.to_thread(
        list_promotable_slots,
        actor=actor,
    )
    selected_slot_id = control._token_uuid(slot_token)
    selected = next(
        (item for item in slots if item.slot.id == selected_slot_id),
        None,
    )
    if selected is None:
        await callback.answer("Свободное время больше не найдено", show_alert=True)
        return
    rows = [
        [
            (
                label,
                f"cpp:make:{business_token}:{slot_token}:{channel.value}",
            )
        ]
        for channel, label in _CHANNEL_BUTTONS
    ]
    rows.append([("⬅️ Назад", f"cpj:promote:{business_token}")])
    await callback.answer()
    await control._callback_message(callback).answer(
        "Куда Вы хотите вывести это предложение?\n\n"
        f"🧰 {selected.offering_title}\n"
        f"📅 {selected.local_start}\n"
        f"⏱ {selected.slot.duration_minutes} минут\n\n"
        "Для каждого канала создаётся отдельная ссылка. Так будет видно, "
        "откуда действительно пришёл клиент.",
        reply_markup=control._keyboard(rows),
    )


@simple.router.callback_query(F.data.startswith("cpp:make:"))
async def create_promotion_material(callback: CallbackQuery) -> None:
    _, _, business_token, slot_token, raw_channel = str(callback.data).split(":", 4)
    business_id = control._token_uuid(business_token)
    slot_id = control._token_uuid(slot_token)
    channel = PromotionChannel(raw_channel)
    actor = await control._actor(int(callback.from_user.id), business_id)
    view = await asyncio.to_thread(
        create_slot_promotion,
        actor=actor,
        slot_id=slot_id,
        channel=channel,
    )
    username = await _bot_username(callback)
    link = _campaign_link(username, view.campaign.source_token)
    advert = _creative_text(view, link)
    rows: list[list[InlineKeyboardButton]] = []
    if channel == PromotionChannel.TELEGRAM:
        telegram_share = (
            "https://t.me/share/url?url="
            + quote(link, safe="")
            + "&text="
            + quote(
                f"{view.campaign.creative.headline}\n\n"
                f"{view.campaign.creative.primary_text}",
                safe="",
            )
        )
        rows.append(
            [InlineKeyboardButton(text="📨 Опубликовать/отправить в Telegram", url=telegram_share)]
        )
    rows.extend(
        [
            [InlineKeyboardButton(text="👀 Открыть рекламную ссылку", url=link)],
            [
                InlineKeyboardButton(
                    text="Другой канал",
                    callback_data=f"cpp:slot:{business_token}:{slot_token}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 Смотреть результат",
                    callback_data=f"cpp:stats:{business_token}",
                )
            ],
        ]
    )
    await callback.answer("Рекламный комплект готов")
    await control._callback_message(callback).answer(
        f"✅ Готово для канала «{_CHANNEL_LABELS[channel]}»\n\n"
        f"{advert}\n\n"
        "Ссылка помечена этим каналом. ClientPlatform посчитает уникальных людей, "
        "которые её открыли, и записи, совершённые через неё. Повторное создание "
        "для того же времени и канала обновляет текст, но сохраняет ссылку.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def dispatch_promotion_start(
    message: Message,
    state: FSMContext,
    *,
    user_id: int,
    managed_bot_business_id: str | None,
) -> bool:
    if managed_bot_business_id is not None:
        return False
    token = parse_promotion_start_payload(control._start_payload(message))
    if token is None:
        return False
    user = message.from_user
    landing = await asyncio.to_thread(
        open_promotion_link,
        source_token=token,
        telegram_user_id=user_id,
        username=None if user is None else user.username,
        display_name=None if user is None else user.full_name,
    )
    await state.clear()
    creative = landing.campaign.creative
    await message.answer(
        f"{creative.headline}\n\n"
        f"{creative.primary_text}\n\n"
        f"{creative.description}\n\n"
        f"🧰 {landing.slot.offering_title}\n"
        f"📅 {landing.slot.local_start}\n"
        f"⏱ {landing.slot.slot.duration_minutes} минут\n"
        "🟢 Время свободно",
        reply_markup=control._keyboard(
            [[("✅ Записаться", f"cpp:book:{landing.campaign.source_token}")]]
        ),
    )
    return True


@simple.router.callback_query(F.data.startswith("cpp:book:"))
async def book_from_promotion(callback: CallbackQuery) -> None:
    source_token = str(callback.data).split(":", 2)[2]
    claim, _campaign = await asyncio.to_thread(
        book_promoted_slot,
        source_token=source_token,
        telegram_user_id=int(callback.from_user.id),
    )
    await callback.answer("Запись подтверждена")
    message = control._callback_message(callback)
    await message.answer(
        f"✅ Вы записаны: {claim.slot.offering_title} — {claim.slot.local_start}, "
        f"{claim.slot.slot.duration_minutes} мин.\n"
        f"Бизнес: {claim.slot.business_name}.\n\n"
        "ClientPlatform сохранил источник записи и пришлёт напоминания. "
        "Ниже можно добавить встречу в календарь телефона."
    )
    await asyncio.to_thread(
        schedule_booking_reminders,
        telegram_user_id=int(callback.from_user.id),
        claim=claim,
    )
    document_sender = getattr(message, "answer_document", None)
    if callable(document_sender):
        calendar = BufferedInputFile(
            booking_calendar_ics(claim.slot),
            filename=booking_calendar_filename(claim.slot),
        )
        await document_sender(
            calendar,
            caption=(
                "📅 Нажмите на файл — телефон предложит добавить встречу "
                "с напоминаниями за 24 часа и за 1 час."
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Добавить в Google Календарь",
                            url=google_calendar_url(claim.slot),
                        )
                    ]
                ]
            ),
        )


__all__ = [
    "book_from_promotion",
    "create_promotion_material",
    "dispatch_promotion_start",
    "open_promotion_workspace",
]
