from __future__ import annotations

import asyncio
import importlib
from types import ModuleType

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from clientplatform.domain.activity import CapabilityStatus
from clientplatform.domain.bookings import BookingSlotStatus

control = importlib.import_module(".clientplatform_control", __package__)
simple = importlib.import_module(".clientplatform_simple_experience", __package__)
builder = importlib.import_module(".clientplatform_program_builder", __package__)

router = Router(name="clientplatform_first_result")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())

_ORIGINAL_OWNER_KEYBOARD = None


def _business_token(business_id: str) -> str:
    return control._uuid_token(business_id)


def _replace_next_action(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for row in markup.inline_keyboard:
        buttons: list[InlineKeyboardButton] = []
        for button in row:
            callback_data = str(button.callback_data or "")
            if callback_data.startswith("cps:next:"):
                token = callback_data.split(":", 2)[2]
                buttons.append(
                    InlineKeyboardButton(
                        text="✨ Что настроить первым?",
                        callback_data=f"cpx:goal:{token}",
                    )
                )
            else:
                buttons.append(button)
        rows.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def install_first_result(owner_module: ModuleType) -> None:
    global _ORIGINAL_OWNER_KEYBOARD
    if bool(getattr(owner_module, "_first_result_installed", False)):
        return
    _ORIGINAL_OWNER_KEYBOARD = owner_module._owner_keyboard

    def owner_keyboard(business_id: str) -> InlineKeyboardMarkup:
        return _replace_next_action(_ORIGINAL_OWNER_KEYBOARD(business_id))

    owner_module._owner_keyboard = owner_keyboard
    owner_module._first_result_installed = True


@router.callback_query(F.data.startswith("cpx:goal:"))
async def choose_first_result(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    business_id = control._token_uuid(business_token)
    await control._actor(int(callback.from_user.id), business_id)
    await state.clear()
    await callback.answer()
    await control._callback_message(callback).answer(
        "Что Вы хотите получить первым?\n\n"
        "Выберите результат — ClientPlatform сам проведёт по минимальному числу шагов.",
        reply_markup=control._keyboard(
            [
                [("📅 Принимать записи", f"cpx:booking:{business_token}")],
                [("📚 Выдавать материалы", f"cpx:materials:{business_token}")],
                [("👥 Подключить клиента", f"cpx:client:{business_token}")],
                [("🤖 Настроить Telegram-бота", f"cpb:o:{business_token}")],
                [("🏠 В кабинет", f"cpj:home:{business_token}")],
            ]
        ),
    )


async def _active_service_capability(actor):
    capabilities = await asyncio.to_thread(
        control.list_business_capabilities,
        actor=actor,
    )
    return next(
        (
            item
            for item in capabilities
            if item.status == CapabilityStatus.ACTIVE
            and item.connector_key in {"consultations", "services"}
        ),
        None,
    )


@router.callback_query(F.data.startswith("cpx:booking:"))
async def setup_first_booking(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    business_id = control._token_uuid(business_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    capability = await _active_service_capability(actor)
    await callback.answer()
    message = control._callback_message(callback)
    if capability is None:
        await message.answer(
            "Для записи сначала нужно включить консультации или услуги. "
            "Откройте «Все возможности» и включите нужный формат.",
            reply_markup=control._keyboard(
                [[("⚙️ Все возможности", f"cps:advanced:{business_token}")]]
            ),
        )
        return

    offerings, slots = await asyncio.gather(
        asyncio.to_thread(
            control.list_business_offerings,
            actor=actor,
            capability_id=capability.id,
        ),
        asyncio.to_thread(control.list_booking_slots, actor=actor),
    )
    if not offerings:
        await state.set_state(control.ClientPlatformControlState.offering_title)
        await state.update_data(
            business_id=business_id,
            capability_id=capability.id,
        )
        await message.answer(
            "Сначала добавим то, на что человек сможет записаться.\n\n"
            "Как называется Ваша встреча или услуга? Например: «Консультация 60 минут».\n\n"
            "Отменить можно командой /cancel."
        )
        return

    open_slots = [item for item in slots if item.slot.status == BookingSlotStatus.OPEN]
    if not open_slots:
        offering = offerings[0]
        await state.set_state(control.ClientPlatformControlState.booking_start)
        await state.update_data(
            business_id=business_id,
            offering_id=offering.id,
        )
        await message.answer(
            f"Услуга «{offering.title}» уже есть. Теперь откроем первое время.\n\n"
            "Напишите дату и время, например: 10.08.2026 15:00.\n\n"
            "Отменить можно командой /cancel."
        )
        return

    await message.answer(
        "✅ Запись уже настроена: у Вас есть опубликованное свободное время.",
        reply_markup=control._keyboard(
            [
                [("📅 Мой календарь", f"cpj:calendar:{business_token}:30")],
                [("🔗 Моя страница", f"cpj:page:{business_token}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("cpx:materials:"))
async def setup_first_material(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    business_id = control._token_uuid(business_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    programs, customers = await asyncio.gather(
        asyncio.to_thread(control.list_programs, actor=actor),
        asyncio.to_thread(control.list_customers, actor=actor),
    )
    await callback.answer()
    message = control._callback_message(callback)
    if not programs:
        await state.clear()
        await state.set_state(builder.ClientPlatformProgramBuilderState.program_title)
        await state.update_data(business_id=business_id)
        await message.answer(
            "Создадим первый материал или программу.\n\n"
            "Напишите название. Например: «Подготовка к первой встрече».\n\n"
            "Отменить можно командой /cancel."
        )
        return
    if not customers:
        await state.clear()
        await simple._invite_customer(
            callback,
            actor=actor,
            business_id=business_id,
        )
        return

    routed = getattr(callback, "model_copy", None)
    if callable(routed):
        delivery_callback = callback.model_copy(
            update={"data": f"cp:deliver:{business_token}"}
        )
    else:
        callback.data = f"cp:deliver:{business_token}"
        delivery_callback = callback
    await control.choose_program_for_delivery(delivery_callback, state)


@router.callback_query(F.data.startswith("cpx:client:"))
async def setup_first_client(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    business_id = control._token_uuid(business_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    await state.clear()
    await callback.answer()
    await simple._invite_customer(
        callback,
        actor=actor,
        business_id=business_id,
    )


__all__ = [
    "choose_first_result",
    "install_first_result",
    "router",
]
