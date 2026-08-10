from __future__ import annotations

"""Close the owner journey after a service time is published.

The module deliberately composes on top of the canonical booking, tenancy and
customer boundaries. It gives a non-technical owner a visible calendar,
customer preview, permanent public storefront and promotion links without
creating a second booking implementation.
"""

import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from types import ModuleType
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from clientplatform.application.pagination import paginate
from clientplatform.application.owner_booking_journey import (
    cancel_owner_booking_slot,
    connect_public_storefront_customer,
    get_owner_booking_slot,
    replace_owner_booking_slot,
)
from clientplatform.domain.bookings import BookingSlotStatus, BookingSlotView

control = importlib.import_module(".clientplatform_control", __package__)
simple = importlib.import_module(".clientplatform_simple_experience", __package__)
entry = importlib.import_module(".clientplatform_entry", __package__)

_PUBLIC_START_PREFIX = "cpsb_"
_PUBLIC_SLOT_PREFIX = "cpss_"
_CALENDAR_LIMIT = 12


def _business_token(business_id: str) -> str:
    return control._uuid_token(business_id)


def _slot_status(slot: BookingSlotView) -> tuple[str, str]:
    labels = {
        BookingSlotStatus.OPEN: ("🟢", "свободно"),
        BookingSlotStatus.BOOKED: ("👤", "клиент записан"),
        BookingSlotStatus.CANCELLED: ("⚪", "снято с публикации"),
        BookingSlotStatus.COMPLETED: ("✅", "завершено"),
    }
    return labels[slot.slot.status]


def _slot_button_text(slot: BookingSlotView) -> str:
    icon, _label = _slot_status(slot)
    return f"{icon} {slot.local_start} · {slot.offering_title[:22]}"


def _slot_text(slot: BookingSlotView, *, customer_preview: bool = False) -> str:
    icon, label = _slot_status(slot)
    heading = "Так карточку увидит клиент" if customer_preview else "Опубликованное время"
    return (
        f"{heading}\n\n"
        f"🧰 {slot.offering_title}\n"
        f"📅 {slot.local_start}\n"
        f"⏱ {slot.slot.duration_minutes} минут\n"
        f"{icon} Статус: {label}\n"
        f"🏠 {slot.business_name}"
    )


async def _all_offerings(actor: Any, capabilities: list[Any]) -> list[Any]:
    groups = await asyncio.gather(
        *[
            asyncio.to_thread(
                control.list_business_offerings,
                actor=actor,
                capability_id=capability.id,
            )
            for capability in capabilities
            if capability.connector_key != "programs"
            and capability.status == control.CapabilityStatus.ACTIVE
        ]
    )
    return [offering for group in groups for offering in group]


def _owner_keyboard(business_id: str) -> InlineKeyboardMarkup:
    token = _business_token(business_id)
    return control._keyboard(
        [
            [("✨ Сделать следующий шаг", f"cps:next:{token}")],
            [
                ("🧰 Мои услуги", f"cpj:services:{token}"),
                ("📅 Мой календарь", f"cpj:calendar:{token}:30"),
            ],
            [
                ("👥 Записи клиентов", f"cpj:bookings:{token}"),
                ("🔗 Моя страница", f"cpj:page:{token}"),
            ],
            [
                ("📢 Продвижение", f"cpj:promote:{token}"),
                ("⚙️ Настройки", f"cps:advanced:{token}"),
            ],
        ]
    )


async def send_owner_dashboard(
    message: Message,
    *,
    user_id: int,
    business_id: str,
) -> None:
    actor, access, profile, capabilities, customers, programs, _open_only = (
        await simple._business_snapshot(user_id=user_id, business_id=business_id)
    )
    offerings, slots = await asyncio.gather(
        _all_offerings(actor, capabilities),
        asyncio.to_thread(
            control.list_booking_slots,
            actor=actor,
            include_unavailable=True,
        ),
    )
    open_slots = [item for item in slots if item.slot.status == BookingSlotStatus.OPEN]
    booked_slots = [item for item in slots if item.slot.status == BookingSlotStatus.BOOKED]
    nearest = min(
        (item for item in slots if item.slot.status in {BookingSlotStatus.OPEN, BookingSlotStatus.BOOKED}),
        key=lambda item: item.slot.starts_at,
        default=None,
    )
    nearest_line = (
        "Ближайшее время: пока не опубликовано"
        if nearest is None
        else f"Ближайшее время: {nearest.local_start} · {nearest.offering_title}"
    )
    await message.answer(
        f"🏠 {access.business.name}\n\n"
        f"{profile.activity_description}\n\n"
        f"Услуг: {len(offerings)} · свободных времён: {len(open_slots)} · "
        f"записей клиентов: {len(booked_slots)}\n"
        f"Материалов и программ: {len(programs)} · клиентов: {len(customers)}\n\n"
        f"{nearest_line}\n\n"
        "Здесь виден весь путь: что Вы предлагаете, когда можно записаться, "
        "как это выглядит для клиента и какую ссылку отправлять людям.",
        reply_markup=_owner_keyboard(business_id),
    )


async def _send_publish_receipt(
    message: Message,
    *,
    slot: BookingSlotView,
    changed: bool = False,
) -> None:
    business_token = _business_token(slot.slot.business_id)
    slot_token = control._uuid_token(slot.slot.id)
    offering_token = control._uuid_token(slot.slot.offering_id)
    title = "Время изменено" if changed else "Готово! Время опубликовано"
    await message.answer(
        f"✅ {title}\n\n"
        f"🧰 {slot.offering_title}\n"
        f"📅 {slot.local_start}\n"
        f"⏱ {slot.slot.duration_minutes} минут\n"
        "🟢 Доступно для записи\n\n"
        "Теперь проверьте карточку глазами клиента или сразу отправьте ссылку людям.",
        reply_markup=control._keyboard(
            [
                [("👀 Посмотреть глазами клиента", f"cpj:preview:{business_token}:{slot_token}")],
                [("📅 Открыть мой календарь", f"cpj:calendar:{business_token}:30")],
                [
                    ("📨 Отправить клиенту", f"cpj:share:{business_token}:{slot_token}"),
                    ("📢 Рекламировать", f"cpj:share:{business_token}:{slot_token}"),
                ],
                [
                    ("✏️ Изменить", f"cpj:edit:{business_token}:{slot_token}"),
                    ("➕ Ещё время", f"cpj:add:{business_token}:{offering_token}"),
                ],
                [("🏠 В кабинет", f"cpj:home:{business_token}")],
            ]
        ),
    )


@simple.router.message(control.ClientPlatformControlState.booking_start)
async def receive_owner_booking_start(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.update_data(booking_start=str(message.text or ""))
    await state.set_state(control.ClientPlatformControlState.booking_duration)
    prefix = "Новое время принято." if data.get("replacing_slot_id") else "Дата и время приняты."
    await message.answer(f"{prefix} Сколько минут длится встреча или услуга? Например: 60")


@simple.router.message(control.ClientPlatformControlState.booking_duration)
async def receive_owner_booking_duration(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data["business_id"])
    actor = await control._actor(control._user_id(message), business_id)
    duration = int(str(message.text or "").strip())
    replacing_slot_id = str(data.get("replacing_slot_id") or "").strip()
    if replacing_slot_id:
        slot = await asyncio.to_thread(
            replace_owner_booking_slot,
            actor=actor,
            slot_id=replacing_slot_id,
            local_start=str(data["booking_start"]),
            duration_minutes=duration,
        )
    else:
        slot = await asyncio.to_thread(
            control.create_booking_slot,
            actor=actor,
            offering_id=str(data["offering_id"]),
            local_start=str(data["booking_start"]),
            duration_minutes=duration,
        )
    await state.clear()
    await _send_publish_receipt(message, slot=slot, changed=bool(replacing_slot_id))


async def _owner_slot(callback: CallbackQuery, business_token: str, slot_token: str) -> BookingSlotView:
    business_id = control._token_uuid(business_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    return await asyncio.to_thread(
        get_owner_booking_slot,
        actor=actor,
        slot_id=control._token_uuid(slot_token),
    )


async def _bot_username(callback: CallbackQuery) -> str:
    bot = await callback.bot.get_me()
    username = str(getattr(bot, "username", "") or "").strip()
    if not username:
        raise RuntimeError("ClientPlatform bot requires a public username")
    return username


def _public_business_payload(business_id: str) -> str:
    return f"{_PUBLIC_START_PREFIX}{_business_token(business_id)}"


def _public_slot_payload(slot: BookingSlotView) -> str:
    return (
        f"{_PUBLIC_SLOT_PREFIX}{_business_token(slot.slot.business_id)}_"
        f"{control._uuid_token(slot.slot.id)}"
    )


def _public_link(username: str, *, business_id: str, slot: BookingSlotView | None = None) -> str:
    payload = _public_business_payload(business_id) if slot is None else _public_slot_payload(slot)
    return f"https://t.me/{username}?start={payload}"


def _promotion_text(slot: BookingSlotView, link: str) -> str:
    return (
        f"{slot.offering_title}\n\n"
        f"Свободное время: {slot.local_start}.\n"
        f"Продолжительность: {slot.slot.duration_minutes} минут.\n\n"
        f"Записаться: {link}"
    )


@simple.router.callback_query(F.data.startswith("cpj:home:"))
async def open_owner_home(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = control._token_uuid(str(callback.data).split(":", 2)[2])
    await state.clear()
    await callback.answer()
    await send_owner_dashboard(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


async def _render_calendar(
    callback: CallbackQuery,
    *,
    business_id: str,
    days: int,
    page: object = 0,
) -> None:
    actor = await control._actor(int(callback.from_user.id), business_id)
    slots = await asyncio.to_thread(
        control.list_booking_slots,
        actor=actor,
        include_unavailable=True,
    )
    horizon = datetime.now(timezone.utc) + timedelta(days=days)
    visible = [
        slot
        for slot in slots
        if datetime.fromisoformat(slot.slot.starts_at).astimezone(timezone.utc) <= horizon
    ]
    current = paginate(visible, page, page_size=_CALENDAR_LIMIT)
    business_token = _business_token(business_id)
    if current.items:
        lines = "\n".join(
            f"{_slot_status(slot)[0]} {slot.local_start} — {slot.offering_title} "
            f"({_slot_status(slot)[1]})"
            for slot in current.items
        )
        remaining = max(0, current.total_items - ((current.index + 1) * _CALENDAR_LIMIT))
        if remaining:
            lines += f"\n…и ещё {remaining}"
        slot_rows = [
            [
                (
                    _slot_button_text(slot),
                    f"cpj:slot:{business_token}:{control._uuid_token(slot.slot.id)}",
                )
            ]
            for slot in current.items
        ]
    else:
        lines = "В выбранном периоде времени пока нет."
        slot_rows = []
    navigation: list[tuple[str, str]] = []
    if current.has_previous:
        navigation.append(("⬅️ Назад", f"cpj:calendar:{business_token}:{days}:{current.index - 1}"))
    if current.has_next:
        navigation.append(("Вперёд ➡️", f"cpj:calendar:{business_token}:{days}:{current.index + 1}"))
    rows = [
        [
            ("7 дней", f"cpj:calendar:{business_token}:7"),
            ("30 дней", f"cpj:calendar:{business_token}:30"),
            ("Все", f"cpj:calendar:{business_token}:3650"),
        ],
        *slot_rows,
    ]
    if navigation:
        rows.append(navigation)
    rows.extend(
        [
            [("➕ Добавить время", f"cpj:services:{business_token}")],
            [("🏠 В кабинет", f"cpj:home:{business_token}")],
        ]
    )
    await callback.answer()
    await control._callback_message(callback).answer(
        f"📅 Мой календарь\n\n{lines}\n\nСтраница {current.index + 1}/{current.count}\n\n"
        "Нажмите на время, чтобы проверить, изменить или снять его.",
        reply_markup=control._keyboard(rows),
    )


@simple.router.callback_query(F.data.startswith("cpj:calendar:"))
async def open_owner_calendar(callback: CallbackQuery) -> None:
    parts = str(callback.data or "").split(":")
    if len(parts) not in {4, 5}:
        await callback.answer("Кнопка устарела. Откройте календарь заново.", show_alert=True)
        return
    _, _, business_token, raw_days, *raw_page = parts
    try:
        days = max(1, min(int(raw_days), 3650))
    except ValueError:
        await callback.answer("Кнопка устарела. Откройте календарь заново.", show_alert=True)
        return
    await _render_calendar(
        callback,
        business_id=control._token_uuid(business_token),
        days=days,
        page=raw_page[0] if raw_page else 0,
    )


@simple.router.callback_query(F.data.startswith("cpj:slot:"))
async def open_owner_slot(callback: CallbackQuery) -> None:
    _, _, business_token, slot_token = str(callback.data).split(":", 3)
    slot = await _owner_slot(callback, business_token, slot_token)
    rows: list[list[tuple[str, str]]] = []
    if slot.slot.status == BookingSlotStatus.OPEN:
        rows.extend(
            [
                [("👀 Посмотреть глазами клиента", f"cpj:preview:{business_token}:{slot_token}")],
                [("📨 Получить ссылку", f"cpj:share:{business_token}:{slot_token}")],
                [
                    ("✏️ Изменить", f"cpj:edit:{business_token}:{slot_token}"),
                    ("🗑 Снять", f"cpj:cancel:{business_token}:{slot_token}"),
                ],
            ]
        )
    rows.append([("⬅️ К календарю", f"cpj:calendar:{business_token}:30")])
    await callback.answer()
    await control._callback_message(callback).answer(
        _slot_text(slot),
        reply_markup=control._keyboard(rows),
    )


@simple.router.callback_query(F.data.startswith("cpj:preview:"))
async def preview_owner_slot(callback: CallbackQuery) -> None:
    _, _, business_token, slot_token = str(callback.data).split(":", 3)
    slot = await _owner_slot(callback, business_token, slot_token)
    await callback.answer()
    await control._callback_message(callback).answer(
        _slot_text(slot, customer_preview=True)
        + "\n\nКлиент нажмёт кнопку ниже и получит подтверждение записи и напоминания.",
        reply_markup=control._keyboard(
            [
                [("✅ Записаться · предпросмотр", "cpj:previewnoop")],
                [("📨 Получить настоящую ссылку", f"cpj:share:{business_token}:{slot_token}")],
                [("⬅️ Назад", f"cpj:slot:{business_token}:{slot_token}")],
            ]
        ),
    )


@simple.router.callback_query(F.data == "cpj:previewnoop")
async def preview_noop(callback: CallbackQuery) -> None:
    await callback.answer("Это предпросмотр. Настоящая запись доступна клиенту по ссылке.", show_alert=True)


@simple.router.callback_query(F.data.startswith("cpj:share:"))
async def share_owner_slot(callback: CallbackQuery) -> None:
    _, _, business_token, slot_token = str(callback.data).split(":", 3)
    slot = await _owner_slot(callback, business_token, slot_token)
    if slot.slot.status != BookingSlotStatus.OPEN:
        await callback.answer("Это время уже нельзя рекламировать", show_alert=True)
        return
    username = await _bot_username(callback)
    link = _public_link(username, business_id=slot.slot.business_id, slot=slot)
    advert = _promotion_text(slot, link)
    telegram_share = (
        "https://t.me/share/url?url="
        + quote(link, safe="")
        + "&text="
        + quote(
            f"{slot.offering_title} — {slot.local_start}, "
            f"{slot.slot.duration_minutes} минут",
            safe="",
        )
    )
    await callback.answer("Ссылка готова")
    await control._callback_message(callback).answer(
        "📢 Готово к отправке и рекламе\n\n"
        f"{advert}\n\n"
        "Эта ссылка постоянная для данного опубликованного времени. Её можно "
        "разместить в Telegram, VK, WhatsApp, на сайте или в объявлении.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📨 Отправить в Telegram", url=telegram_share)],
                [InlineKeyboardButton(text="🔗 Открыть страницу записи", url=link)],
                [InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=f"cpj:slot:{business_token}:{slot_token}",
                )],
            ]
        ),
    )


@simple.router.callback_query(F.data.startswith("cpj:add:"))
async def add_another_slot(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, business_token, offering_token = str(callback.data).split(":", 3)
    business_id = control._token_uuid(business_token)
    offering_id = control._token_uuid(offering_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    offerings = []
    capabilities = await asyncio.to_thread(control.list_business_capabilities, actor=actor)
    for capability in capabilities:
        if capability.connector_key == "programs":
            continue
        offerings.extend(
            await asyncio.to_thread(
                control.list_business_offerings,
                actor=actor,
                capability_id=capability.id,
            )
        )
    if not any(item.id == offering_id for item in offerings):
        await callback.answer("Услуга больше недоступна", show_alert=True)
        return
    await state.clear()
    await state.set_state(control.ClientPlatformControlState.booking_start)
    await state.update_data(business_id=business_id, offering_id=offering_id)
    await callback.answer()
    await control._callback_message(callback).answer(
        "Напишите новое свободное время в формате ДД.ММ.ГГГГ ЧЧ:ММ.\n"
        "Например: 15.08.2026 18:30"
    )


@simple.router.callback_query(F.data.startswith("cpj:edit:"))
async def edit_owner_slot(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, business_token, slot_token = str(callback.data).split(":", 3)
    slot = await _owner_slot(callback, business_token, slot_token)
    if slot.slot.status != BookingSlotStatus.OPEN:
        await callback.answer("Изменять можно только свободное время", show_alert=True)
        return
    await state.clear()
    await state.set_state(control.ClientPlatformControlState.booking_start)
    await state.update_data(
        business_id=slot.slot.business_id,
        offering_id=slot.slot.offering_id,
        replacing_slot_id=slot.slot.id,
    )
    await callback.answer()
    await control._callback_message(callback).answer(
        f"Сейчас: {slot.local_start}, {slot.slot.duration_minutes} минут.\n\n"
        "Напишите новые дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ."
    )


@simple.router.callback_query(F.data.startswith("cpj:cancel:"))
async def confirm_cancel_owner_slot(callback: CallbackQuery) -> None:
    _, _, business_token, slot_token = str(callback.data).split(":", 3)
    slot = await _owner_slot(callback, business_token, slot_token)
    if slot.slot.status != BookingSlotStatus.OPEN:
        await callback.answer("Снять можно только свободное время", show_alert=True)
        return
    await callback.answer()
    await control._callback_message(callback).answer(
        f"Снять с публикации «{slot.offering_title}» {slot.local_start}?\n\n"
        "После этого клиенты больше не увидят это время.",
        reply_markup=control._keyboard(
            [
                [("Да, снять", f"cpj:cancelok:{business_token}:{slot_token}")],
                [("Нет, оставить", f"cpj:slot:{business_token}:{slot_token}")],
            ]
        ),
    )


@simple.router.callback_query(F.data.startswith("cpj:cancelok:"))
async def cancel_owner_slot(callback: CallbackQuery) -> None:
    _, _, business_token, slot_token = str(callback.data).split(":", 3)
    business_id = control._token_uuid(business_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    await asyncio.to_thread(
        cancel_owner_booking_slot,
        actor=actor,
        slot_id=control._token_uuid(slot_token),
    )
    await callback.answer("Время снято с публикации")
    await _render_calendar(callback, business_id=business_id, days=30)


@simple.router.callback_query(F.data.startswith("cpj:services:"))
async def open_owner_services(callback: CallbackQuery) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    business_id = control._token_uuid(business_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    capabilities = await asyncio.to_thread(control.list_business_capabilities, actor=actor)
    offerings, slots = await asyncio.gather(
        _all_offerings(actor, capabilities),
        asyncio.to_thread(control.list_booking_slots, actor=actor, include_unavailable=True),
    )
    open_counts: dict[str, int] = {}
    for slot in slots:
        if slot.slot.status == BookingSlotStatus.OPEN:
            open_counts[slot.slot.offering_id] = open_counts.get(slot.slot.offering_id, 0) + 1
    lines = "\n".join(
        f"• {offering.title} — {offering.description}\n"
        f"  Свободных времён: {open_counts.get(offering.id, 0)}"
        for offering in offerings
    ) or "Вы ещё не добавили ни одной услуги."
    rows = [
        [
            (
                f"🕒 Добавить время · {offering.title[:22]}",
                f"cpj:add:{business_token}:{control._uuid_token(offering.id)}",
            )
        ]
        for offering in offerings
    ]
    first_capability = next(
        (
            item
            for item in capabilities
            if item.connector_key != "programs"
            and item.status == control.CapabilityStatus.ACTIVE
        ),
        None,
    )
    if first_capability is not None:
        rows.append(
            [
                (
                    "➕ Добавить новую услугу",
                    f"cp:offeradd:{business_token}:{control._uuid_token(first_capability.id)}",
                )
            ]
        )
    rows.extend(
        [
            [("📅 Мой календарь", f"cpj:calendar:{business_token}:30")],
            [("🏠 В кабинет", f"cpj:home:{business_token}")],
        ]
    )
    await callback.answer()
    await control._callback_message(callback).answer(
        f"🧰 Мои услуги\n\n{lines}",
        reply_markup=control._keyboard(rows),
    )


@simple.router.callback_query(F.data.startswith("cpj:bookings:"))
async def open_customer_bookings(callback: CallbackQuery) -> None:
    parts = str(callback.data or "").split(":")
    if len(parts) not in {3, 4}:
        await callback.answer("Кнопка устарела. Откройте записи заново.", show_alert=True)
        return
    _, _, business_token, *raw_page = parts
    business_id = control._token_uuid(business_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    slots, customers = await asyncio.gather(
        asyncio.to_thread(control.list_booking_slots, actor=actor, include_unavailable=True),
        asyncio.to_thread(control.list_customers, actor=actor),
    )
    names = {item.id: item.display_name or "Клиент" for item in customers}
    booked = [item for item in slots if item.slot.status == BookingSlotStatus.BOOKED]
    current = paginate(booked, raw_page[0] if raw_page else 0, page_size=_CALENDAR_LIMIT)
    lines = "\n".join(
        f"👤 {slot.local_start} — {slot.offering_title}\n"
        f"   {names.get(slot.slot.booked_customer_id or '', 'Клиент')}"
        for slot in current.items
    ) or "Будущих записей клиентов пока нет."
    rows = [
        [
            (
                _slot_button_text(slot),
                f"cpj:slot:{business_token}:{control._uuid_token(slot.slot.id)}",
            )
        ]
        for slot in current.items
    ]
    navigation: list[tuple[str, str]] = []
    if current.has_previous:
        navigation.append(("⬅️ Назад", f"cpj:bookings:{business_token}:{current.index - 1}"))
    if current.has_next:
        navigation.append(("Вперёд ➡️", f"cpj:bookings:{business_token}:{current.index + 1}"))
    if navigation:
        rows.append(navigation)
    rows.extend(
        [
            [("📅 Весь календарь", f"cpj:calendar:{business_token}:30")],
            [("🏠 В кабинет", f"cpj:home:{business_token}")],
        ]
    )
    await callback.answer()
    await control._callback_message(callback).answer(
        f"👥 Записи клиентов\n\n{lines}\n\nСтраница {current.index + 1}/{current.count}",
        reply_markup=control._keyboard(rows),
    )


@simple.router.callback_query(F.data.startswith("cpj:page:"))
async def open_public_page_for_owner(callback: CallbackQuery) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    business_id = control._token_uuid(business_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    capabilities = await asyncio.to_thread(control.list_business_capabilities, actor=actor)
    offerings, slots = await asyncio.gather(
        _all_offerings(actor, capabilities),
        asyncio.to_thread(control.list_booking_slots, actor=actor),
    )
    username = await _bot_username(callback)
    link = _public_link(username, business_id=business_id)
    offering_lines = "\n".join(f"• {item.title}" for item in offerings) or "• услуги пока не добавлены"
    slot_lines = "\n".join(
        f"• {item.local_start} — {item.offering_title}" for item in slots[:8]
    ) or "• свободного времени пока нет"
    await callback.answer()
    await control._callback_message(callback).answer(
        "🔗 Ваша публичная страница\n\n"
        f"Услуги:\n{offering_lines}\n\n"
        f"Свободное время:\n{slot_lines}\n\n"
        f"Постоянная ссылка бизнеса:\n{link}\n\n"
        "Отправьте её людям или разместите в профиле, на сайте и в рекламе. "
        "Посетитель сразу увидит актуальное свободное время.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔗 Открыть публичную страницу", url=link)],
                [InlineKeyboardButton(
                    text="📢 Продвижение",
                    callback_data=f"cpj:promote:{business_token}",
                )],
                [InlineKeyboardButton(
                    text="🏠 В кабинет",
                    callback_data=f"cpj:home:{business_token}",
                )],
            ]
        ),
    )


@simple.router.callback_query(F.data.startswith("cpj:promote:"))
async def open_promotion(callback: CallbackQuery) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    business_id = control._token_uuid(business_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    slots = await asyncio.to_thread(control.list_booking_slots, actor=actor)
    rows = [
        [
            (
                f"📢 {slot.local_start} · {slot.offering_title[:20]}",
                f"cpj:share:{business_token}:{control._uuid_token(slot.slot.id)}",
            )
        ]
        for slot in slots[:_CALENDAR_LIMIT]
    ]
    rows.extend(
        [
            [("🔗 Постоянная страница бизнеса", f"cpj:page:{business_token}")],
            [("🏠 В кабинет", f"cpj:home:{business_token}")],
        ]
    )
    text = (
        "Выберите время — я подготовлю готовый рекламный текст и прямую ссылку на запись."
        if slots
        else "Сначала опубликуйте хотя бы одно свободное время."
    )
    await callback.answer()
    await control._callback_message(callback).answer(
        f"📢 Продвижение\n\n{text}",
        reply_markup=control._keyboard(rows),
    )


async def _send_public_storefront(
    message: Message,
    *,
    business_id: str,
    business_name: str,
    slots: list[BookingSlotView],
    focused_slot_id: str | None,
) -> None:
    business_token = _business_token(business_id)
    selected = next(
        (slot for slot in slots if slot.slot.id == focused_slot_id),
        None,
    )
    if selected is not None:
        await message.answer(
            f"{selected.business_name}\n\n"
            f"🧰 {selected.offering_title}\n"
            f"📅 {selected.local_start}\n"
            f"⏱ {selected.slot.duration_minutes} минут\n"
            "🟢 Можно записаться",
            reply_markup=control._keyboard(
                [[(
                    "✅ Записаться",
                    f"cp:book:{business_token}:{control._uuid_token(selected.slot.id)}",
                )], [("Посмотреть другое время", f"cp:client:{business_token}")]]
            ),
        )
        return
    await control._send_client_booking_page(
        message,
        business_token=business_token,
        slots=slots,
        title=f"{business_name}\n\nДоступно для записи",
        empty_text=(
            f"{business_name}\n\n"
            "Публичная страница открыта, но свободного времени сейчас нет. "
            "Вы уже подключены и увидите новые варианты при следующем открытии страницы."
        ),
    )


async def _dispatch_public_start(
    original: Callable[..., Awaitable[None]],
    message: Message,
    state: FSMContext,
    *,
    user_id: int,
    managed_bot_business_id: str | None,
) -> None:
    payload = control._start_payload(message)
    business_token = ""
    slot_token = ""
    if managed_bot_business_id is None and payload.startswith(_PUBLIC_START_PREFIX):
        business_token = payload.removeprefix(_PUBLIC_START_PREFIX)
    elif managed_bot_business_id is None and payload.startswith(_PUBLIC_SLOT_PREFIX):
        encoded = payload.removeprefix(_PUBLIC_SLOT_PREFIX)
        business_token, separator, slot_token = encoded.partition("_")
        if not separator:
            business_token = ""
    if not business_token:
        await original(
            message,
            state,
            user_id=user_id,
            managed_bot_business_id=managed_bot_business_id,
        )
        return
    business_id = control._token_uuid(business_token)
    user = message.from_user
    link = await asyncio.to_thread(
        connect_public_storefront_customer,
        business_id=business_id,
        telegram_user_id=user_id,
        username=None if user is None else user.username,
        display_name=None if user is None else user.full_name,
    )
    await state.clear()
    slots = await asyncio.to_thread(
        control.list_customer_booking_slots,
        telegram_user_id=user_id,
        business_id=business_id,
    )
    focused_slot_id = control._token_uuid(slot_token) if slot_token else None
    await _send_public_storefront(
        message,
        business_id=business_id,
        business_name=link.business_name,
        slots=slots,
        focused_slot_id=focused_slot_id,
    )


def install_owner_journey(
    entry_module: ModuleType,
    control_module: ModuleType,
    simple_module: ModuleType,
) -> None:
    """Install dashboard/start overrides once; router handlers are registered above."""

    if bool(getattr(control_module, "_owner_journey_installed", False)):
        return
    original_dispatch = entry_module._dispatch_clientplatform_start

    async def dispatch(
        message: Message,
        state: FSMContext,
        *,
        user_id: int,
        managed_bot_business_id: str | None,
    ) -> None:
        await _dispatch_public_start(
            original_dispatch,
            message,
            state,
            user_id=user_id,
            managed_bot_business_id=managed_bot_business_id,
        )

    entry_module._dispatch_clientplatform_start = dispatch
    simple_module.send_simple_dashboard = send_owner_dashboard
    control_module._send_dashboard = send_owner_dashboard
    control_module._owner_journey_installed = True


__all__ = [
    "install_owner_journey",
    "receive_owner_booking_duration",
    "receive_owner_booking_start",
    "send_owner_dashboard",
]
