from __future__ import annotations

"""A result-first ClientPlatform surface for non-technical owners."""

import asyncio
import importlib
import os
from types import ModuleType
from typing import Any
from urllib.parse import urlencode

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from clientplatform.application.capability_parity import (
    CapabilityAvailability,
    get_business_capability_projection,
)
from clientplatform.application.managed_bot_onboarding import (
    has_active_telegram_managed_bot,
)
from clientplatform.domain.activity import CapabilityStatus
from clientplatform.domain.bookings import BookingSlotStatus
from clientplatform.presentation import owner_navigation as nav

control = importlib.import_module(".clientplatform_control", __package__)
builder = importlib.import_module(".clientplatform_program_builder", __package__)

router = Router(name="clientplatform_simple_experience")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())

_ADVANCED_KEYBOARD: Any = None


_CAPABILITY_STATE_LABELS = {
    CapabilityAvailability.ACTIVE: "✅ работает",
    CapabilityAvailability.ATTENTION: "⚠️ требует внимания",
    CapabilityAvailability.CONFIGURING: "⏳ настройка",
    CapabilityAvailability.CONNECTABLE: "○ можно подключить",
    CapabilityAvailability.CONNECTED_UNAVAILABLE: "⏸ подключено, но сейчас выключено",
    CapabilityAvailability.UNAVAILABLE: "⏸ сейчас недоступно",
}


def _capability_state_label(value: CapabilityAvailability) -> str:
    return _CAPABILITY_STATE_LABELS[value]


def _routed_callback(callback: CallbackQuery, data: str) -> CallbackQuery:
    copier = getattr(callback, "model_copy", None)
    if callable(copier):
        return copier(update={"data": data})
    callback.data = data
    return callback


def _managed_bot_auto_enabled() -> bool:
    return (
        os.getenv("CLIENTPLATFORM_MANAGED_BOT_AUTO_PROVISIONING_ENABLED") or ""
    ).strip().lower() in {"1", "true", "yes", "on"}


def welcome_keyboard():
    return control._keyboard(
        [[("🚀 Запустить мой бизнес", "cps:start")]]
    )


def welcome_text() -> str:
    return (
        "Здравствуйте! Я ClientPlatform — цифровой помощник для Вашего дела.\n\n"
        "Я помогу без сложных настроек:\n"
        "• подключать клиентов;\n"
        "• записывать их на встречи и напоминать;\n"
        "• выдавать аудио, видео, документы и программы;\n"
        "• показывать, кто получил материал и что требует внимания.\n\n"
        "Сначала понадобится только название и одно простое описание. "
        "Остальное я подготовлю сам."
    )


def _simple_keyboard(business_id: str):
    token = control._uuid_token(business_id)
    return control._keyboard(
        [
            [("✨ Помочь выбрать первый шаг", f"cps:firstgoal:{token}")],
            [(nav.CUSTOMERS.label, f"cp:clients:{token}")],
            [(nav.PROGRAMS.label, f"cps:programs:{token}")],
            [(nav.BOOKINGS.label, f"cps:booking:{token}")],
            [(nav.TODAY.label, f"cp:results:{token}")],
            [(nav.ALL.label, f"cps:advanced:{token}")],
        ]
    )


def _telegram_share_url(url: str, text: str) -> str:
    """Build Telegram's documented share URL with encoded values."""

    return "https://t.me/share/url?" + urlencode({"url": url, "text": text})


async def _business_snapshot(*, user_id: int, business_id: str):
    actor = await control._actor(user_id, business_id)
    profile, capabilities, customers, programs, slots, accesses = await asyncio.gather(
        asyncio.to_thread(control.get_business_profile, actor=actor),
        asyncio.to_thread(control.list_business_capabilities, actor=actor),
        asyncio.to_thread(control.list_customers, actor=actor),
        asyncio.to_thread(control.list_programs, actor=actor),
        asyncio.to_thread(control.list_booking_slots, actor=actor),
        asyncio.to_thread(control.list_accessible_businesses, user_id=user_id),
    )
    access = next(item for item in accesses if item.business.id == business_id)
    return actor, access, profile, capabilities, customers, programs, slots


async def send_simple_dashboard(
    message: Message,
    *,
    user_id: int,
    business_id: str,
) -> None:
    _actor, access, profile, _capabilities, customers, programs, slots = (
        await _business_snapshot(user_id=user_id, business_id=business_id)
    )
    open_slots = sum(item.slot.status == BookingSlotStatus.OPEN for item in slots)
    await message.answer(
        f"🏠 {access.business.name}\n\n"
        f"Чем Вы занимаетесь: {profile.activity_description}\n\n"
        "Что можно сделать прямо сейчас:\n"
        "• подключить клиента;\n"
        "• создать и выдать материалы;\n"
        "• открыть время для записи;\n"
        "• посмотреть результат.\n\n"
        f"Клиентов: {len(customers)} · программ: {len(programs)} · "
        f"свободных времён: {open_slots}\n\n"
        "Совсем не знаете, с чего начать? Нажмите «✨ Помочь выбрать первый шаг» — "
        "ClientPlatform проведёт по минимальному числу действий.",
        reply_markup=_simple_keyboard(business_id),
    )


async def send_advanced_dashboard(
    message: Message,
    *,
    user_id: int,
    business_id: str,
) -> None:
    actor = await control._actor(user_id, business_id)
    profile, capabilities, accesses, external = await asyncio.gather(
        asyncio.to_thread(control.get_business_profile, actor=actor),
        asyncio.to_thread(control.list_business_capabilities, actor=actor),
        asyncio.to_thread(control.list_accessible_businesses, user_id=user_id),
        asyncio.to_thread(get_business_capability_projection, actor=actor),
    )
    access = next(item for item in accesses if item.business.id == business_id)
    module_lines = "\n".join(f"• {item.title}" for item in capabilities) or "• пока не выбраны"
    messenger_names = {
        "telegram": "Telegram",
        "vk": "ВКонтакте",
        "max": "MAX",
    }
    messenger_lines = "\n".join(
        f"• {messenger_names[item.platform.value]} — {_capability_state_label(item.availability)}"
        for item in external.messengers
    )
    advertising_lines = ""
    if external.yandex_direct is not None:
        advertising_lines = (
            "\n\nПродвижение:\n"
            f"• Яндекс Директ — {_capability_state_label(external.yandex_direct.availability)}"
        )

    base_keyboard = _ADVANCED_KEYBOARD(business_id, capabilities)
    token = control._uuid_token(business_id)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=nav.MESSENGERS.label, callback_data=f"cpa:{token}:messengers")],
            [InlineKeyboardButton(text="📣 Реклама", callback_data=f"cpo:ads:{token}")],
            *base_keyboard.inline_keyboard,
            [InlineKeyboardButton(text="🎨 Фирменный стиль", callback_data=f"cpb:open:{token}")],
        ]
    )
    await message.answer(
        f"🧩 Бизнес и возможности · {access.business.name}\n\n"
        f"Чем Вы занимаетесь:\n{profile.activity_description}\n\n"
        f"Каналы:\n{messenger_lines}"
        f"{advertising_lines}\n\n"
        f"Рабочие возможности:\n{module_lines}\n\n"
        "Статусы показывают фактическую доступность в этой установке ClientPlatform. "
        "Если канал технически ещё не готов, кнопка подключения не показывается.",
        reply_markup=keyboard,
    )


def install_simple_experience(control_module: ModuleType) -> None:
    global _ADVANCED_KEYBOARD
    if bool(getattr(control_module, "_simple_experience_installed", False)):
        return
    _ADVANCED_KEYBOARD = control_module._dashboard_keyboard
    control_module._send_dashboard = send_simple_dashboard
    control_module._simple_experience_installed = True


@router.callback_query(F.data == "cps:start")
async def start_simple_onboarding(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(control.ClientPlatformControlState.business_name)
    await callback.answer()
    await control._callback_message(callback).answer(
        "Как называется Ваше дело, проект или практика?\n\n"
        "Например: «Практика Анны», «Автосервис Мотор» или «Школа английского»."
    )


async def _invite_customer(callback: CallbackQuery, *, actor: Any, business_id: str) -> None:
    issued = await asyncio.to_thread(control.issue_customer_invite, actor=actor)
    bot = await callback.bot.get_me()
    if not bot.username:
        raise RuntimeError("clientplatform control bot requires a public username")
    link = f"https://t.me/{bot.username}?start=cpj_{issued.token}"
    share_url = _telegram_share_url(
        link,
        "Подключитесь ко мне в ClientPlatform — здесь можно записываться и получать материалы.",
    )
    await control._callback_message(callback).answer(
        "Первый полезный шаг — подключить клиента.\n\n"
        "Нажмите «Отправить клиенту» и выберите нужный чат — Telegram сам подставит "
        "персональное приглашение. Ссылку ниже можно скопировать вручную, если удобнее:\n\n"
        f"{link}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📨 Отправить клиенту",
                        url=share_url,
                    )
                ]
            ]
        ),
    )


@router.callback_query(F.data.startswith("cps:next:"))
async def next_best_action(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = control._token_uuid(str(callback.data).split(":", 2)[2])
    user_id = int(callback.from_user.id)
    actor, _access, _profile, capabilities, customers, programs, slots = (
        await _business_snapshot(user_id=user_id, business_id=business_id)
    )
    await callback.answer()
    message = control._callback_message(callback)

    if not programs:
        await state.clear()
        await state.update_data(business_id=business_id)
        await state.set_state(builder.ClientPlatformProgramBuilderState.program_title)
        await message.answer(
            "Давайте создадим первый материал или программу.\n\n"
            "Напишите её название. Например: «Подготовка к первой встрече» "
            "или «Мини-курс из трёх уроков»."
        )
        return

    if _managed_bot_auto_enabled() and not await asyncio.to_thread(
        has_active_telegram_managed_bot,
        actor=actor,
    ):
        await state.clear()
        token = control._uuid_token(business_id)
        await message.answer(
            "Материалы готовы. Теперь создадим Вашего персонального бота — через "
            "него клиенты будут получать программы и общаться с ClientPlatform.\n\n"
            "Нажмите кнопку ниже: Telegram откроет встроенное создание, а всё "
            "техническое ClientPlatform выполнит сам.",
            reply_markup=control._keyboard(
                [[("✨ Создать моего бота", f"cpb:o:{token}")]]
            ),
        )
        return

    if not customers:
        await state.clear()
        await _invite_customer(callback, actor=actor, business_id=business_id)
        return

    active = [item for item in capabilities if item.status == CapabilityStatus.ACTIVE]
    offerings: list[Any] = []
    for capability in active:
        if capability.connector_key == "programs":
            continue
        offerings.extend(
            await asyncio.to_thread(
                control.list_business_offerings,
                actor=actor,
                capability_id=capability.id,
            )
        )
    if not offerings:
        capability = next(
            (item for item in active if item.connector_key in {"consultations", "services"}),
            None,
        )
        if capability is not None:
            await state.clear()
            await state.set_state(control.ClientPlatformControlState.offering_title)
            await state.update_data(business_id=business_id, capability_id=capability.id)
            await message.answer(
                "Теперь добавим то, на что клиент сможет записаться.\n\n"
                "Как называется Ваша встреча или услуга? Например: «Консультация 60 минут»."
            )
            return
    if offerings and not any(item.slot.status == BookingSlotStatus.OPEN for item in slots):
        offering = offerings[0]
        await state.clear()
        await state.set_state(control.ClientPlatformControlState.booking_start)
        await state.update_data(business_id=business_id, offering_id=offering.id)
        await message.answer(
            f"Откроем первое время для записи на «{offering.title}».\n\n"
            "Напишите дату и время: ДД.ММ.ГГГГ ЧЧ:ММ. Например: 10.08.2026 15:00"
        )
        return

    await message.answer(
        "✅ Основной путь уже настроен.\n\n"
        "У Вас есть материалы, клиенты и возможность записи. Теперь можно выдать "
        "программу клиенту или посмотреть результат.",
        reply_markup=control._keyboard(
            [[("📚 Выдать программу", f"cp:deliver:{control._uuid_token(business_id)}")],
             [("📊 Посмотреть результат", f"cp:results:{control._uuid_token(business_id)}")]]
        ),
    )


@router.callback_query(F.data.startswith("cps:programs:"))
async def open_simple_programs(callback: CallbackQuery, state: FSMContext) -> None:
    token = str(callback.data).split(":", 2)[2]
    routed = _routed_callback(callback, f"cp:cap:{token}:programs")
    await builder.open_programs(routed, state)


@router.callback_query(F.data.startswith("cps:booking:"))
async def open_simple_booking(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = control._token_uuid(str(callback.data).split(":", 2)[2])
    actor = await control._actor(int(callback.from_user.id), business_id)
    capabilities = await asyncio.to_thread(control.list_business_capabilities, actor=actor)
    capability = next(
        (item for item in capabilities if item.connector_key in {"consultations", "services"} and item.status == CapabilityStatus.ACTIVE),
        None,
    )
    if capability is None:
        token = control._uuid_token(business_id)
        await callback.answer()
        await control._callback_message(callback).answer(
            "Запись пока не подключена. Нажмите «Помочь выбрать первый шаг» — "
            "я проведу по нужным шагам.",
            reply_markup=control._keyboard(
                [[("✨ Помочь выбрать первый шаг", f"cps:firstgoal:{token}")]]
            ),
        )
        return
    routed = _routed_callback(
        callback,
        f"cp:cap:{control._uuid_token(business_id)}:{capability.connector_key}",
    )
    await control.open_capability(routed, state)


@router.callback_query(F.data.startswith("cps:advanced:"))
async def open_advanced_dashboard(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = control._token_uuid(str(callback.data).split(":", 2)[2])
    await state.clear()
    await callback.answer()
    await send_advanced_dashboard(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


__all__ = [
    "install_simple_experience",
    "router",
    "send_simple_dashboard",
    "welcome_keyboard",
    "welcome_text",
]
