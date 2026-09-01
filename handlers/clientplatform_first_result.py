from __future__ import annotations

import asyncio
import importlib
from types import ModuleType

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from clientplatform.application.business_profile import get_business_profile_details
from clientplatform.domain.activity import BusinessProfileStatus, CapabilityStatus
from clientplatform.domain.bookings import BookingSlotStatus

control = importlib.import_module(".clientplatform_control", __package__)
simple = importlib.import_module(".clientplatform_simple_experience", __package__)
builder = importlib.import_module(".clientplatform_program_builder", __package__)

router = Router(name="clientplatform_first_result")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())

_ORIGINAL_OWNER_KEYBOARD = None


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
                        text="✨ Помочь выбрать первый шаг",
                        callback_data=f"cps:firstgoal:{token}",
                    )
                )
            else:
                buttons.append(button)
        rows.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _cancel_setup_keyboard(business_token: str) -> InlineKeyboardMarkup:
    return control._keyboard(
        [[("✖️ Отмена", f"cps:cancelsetup:{business_token}")]]
    )


def install_first_result(owner_module: ModuleType) -> None:
    """Install or repair the result-first owner dashboard wrapper.

    Test tooling and hot-reload environments may reload the owner module while
    the package-level composition marker survives. Checking the actual callable
    keeps composition idempotent in production and self-healing after a reload.
    """

    global _ORIGINAL_OWNER_KEYBOARD
    current = owner_module._owner_keyboard
    if bool(getattr(current, "_clientplatform_first_result_wrapper", False)):
        owner_module._first_result_installed = True
        return
    _ORIGINAL_OWNER_KEYBOARD = current

    def owner_keyboard(business_id: str) -> InlineKeyboardMarkup:
        return _replace_next_action(current(business_id))

    owner_keyboard._clientplatform_first_result_wrapper = True  # type: ignore[attr-defined]
    owner_module._owner_keyboard = owner_keyboard
    owner_module._first_result_installed = True


async def _prepare_first_result(actor, *, connector_key: str) -> None:
    """Activate only the explicitly chosen capability and complete a confirmed draft."""

    profile = await asyncio.to_thread(control.get_business_profile, actor=actor)
    if profile.status == BusinessProfileStatus.DRAFT:
        structured = await asyncio.to_thread(get_business_profile_details, actor=actor)
        if not structured.confirmed:
            raise ValueError("Сначала подтвердите данные о бизнесе.")
    capabilities = await asyncio.to_thread(
        control.list_business_capabilities,
        actor=actor,
        include_disabled=True,
    )
    selected = next((item for item in capabilities if item.connector_key == connector_key), None)
    if selected is None or selected.status != CapabilityStatus.ACTIVE:
        await asyncio.to_thread(
            control.enable_business_capability,
            actor=actor,
            connector_key=connector_key,
        )
    if profile.status == BusinessProfileStatus.DRAFT:
        await asyncio.to_thread(control.complete_business_profile, actor=actor)


@router.callback_query(F.data.startswith("cps:firstgoal:"))
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
                [("📅 Принимать записи", f"cps:firstbook:{business_token}")],
                [("📚 Выдавать материалы", f"cps:firstmat:{business_token}")],
                [("👥 Подключить клиента", f"cps:firstclient:{business_token}")],
                [("🤖 Настроить Telegram-бота", f"cpb:o:{business_token}")],
                [("🏠 В кабинет", f"cpj:home:{business_token}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("cps:cancelsetup:"))
async def cancel_first_result_setup(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    business_id = control._token_uuid(business_token)
    await control._actor(int(callback.from_user.id), business_id)
    await state.clear()
    await callback.answer("Настройка отменена")
    await simple.send_simple_dashboard(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
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


@router.callback_query(F.data.startswith("cps:firstbook:"))
async def setup_first_booking(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    business_id = control._token_uuid(business_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    capability = await _active_service_capability(actor)
    if capability is None:
        await _prepare_first_result(actor, connector_key="services")
        capability = await _active_service_capability(actor)
    else:
        profile = await asyncio.to_thread(control.get_business_profile, actor=actor)
        if profile.status == BusinessProfileStatus.DRAFT:
            structured = await asyncio.to_thread(get_business_profile_details, actor=actor)
            if not structured.confirmed:
                raise ValueError("Сначала подтвердите данные о бизнесе.")
            await asyncio.to_thread(control.complete_business_profile, actor=actor)
    await callback.answer()
    message = control._callback_message(callback)
    if capability is None:  # pragma: no cover - repository invariant
        raise RuntimeError("first booking capability activation was lost")

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
            "Как называется Ваша встреча или услуга? "
            "Например: «Консультация 60 минут».",
            reply_markup=_cancel_setup_keyboard(business_token),
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
            "Напишите дату и время. Можно коротко: 10.08 15:00. "
            "Если нужен другой год: 10.08.27 15:00.",
            reply_markup=_cancel_setup_keyboard(business_token),
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


@router.callback_query(F.data.startswith("cps:firstmat:"))
async def setup_first_material(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    business_id = control._token_uuid(business_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    await _prepare_first_result(actor, connector_key="programs")
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
            "Напишите название. Например: «Подготовка к первой встрече».",
            reply_markup=_cancel_setup_keyboard(business_token),
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

    delivery_callback = simple._routed_callback(
        callback,
        f"cp:deliver:{business_token}",
    )
    await control.choose_program_for_delivery(delivery_callback, state)


@router.callback_query(F.data.startswith("cps:firstclient:"))
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
    "cancel_first_result_setup",
    "choose_first_result",
    "install_first_result",
    "router",
]
