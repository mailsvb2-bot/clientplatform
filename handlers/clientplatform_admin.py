from __future__ import annotations

"""Owner-facing administration panel for the central ClientPlatform bot."""

import asyncio
import importlib
from types import ModuleType

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from clientplatform.application.bookings import list_booking_slots
from clientplatform.application.control import business_delivery_summary
from clientplatform.application.tenancy import list_accessible_businesses
from clientplatform.domain.activity import CapabilityStatus
from clientplatform.domain.bookings import BookingSlotStatus

control = importlib.import_module(".clientplatform_control", __package__)

router = Router(name="clientplatform_admin")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())


def _admin_keyboard(business_id: str) -> InlineKeyboardMarkup:
    token = control._uuid_token(business_id)
    return control._keyboard(
        [
            [
                ("Клиенты", f"cp:clients:{token}"),
                ("Результаты", f"cp:results:{token}"),
            ],
            [("Форматы работы", f"cpa:formats:{token}")],
            [("Мой Telegram-бот", f"cpb:o:{token}")],
            [("Изменить название", f"cps:rename:{token}")],
            [("Обновить", f"cpa:home:{token}")],
            [("Вернуться в кабинет", f"cpa:back:{token}")],
        ]
    )


async def _admin_snapshot(
    *,
    user_id: int,
    business_id: str,
) -> tuple[object, object, list[object], list[object]]:
    actor = await control._actor(user_id, business_id)
    summary, capabilities, slots, accesses = await asyncio.gather(
        asyncio.to_thread(business_delivery_summary, actor=actor),
        asyncio.to_thread(control.list_business_capabilities, actor=actor),
        asyncio.to_thread(list_booking_slots, actor=actor),
        asyncio.to_thread(list_accessible_businesses, user_id=user_id),
    )
    access = next(
        item for item in accesses if str(item.business.id) == str(business_id)
    )
    return access, summary, list(capabilities), list(slots)


async def send_admin_panel(
    message: Message,
    *,
    user_id: int,
    business_id: str,
) -> None:
    access, summary, capabilities, slots = await _admin_snapshot(
        user_id=user_id,
        business_id=business_id,
    )
    active_capabilities = [
        item for item in capabilities if item.status == CapabilityStatus.ACTIVE
    ]
    open_slots = [
        item for item in slots if item.slot.status == BookingSlotStatus.OPEN
    ]
    await message.answer(
        f"Админка · {access.business.name}\n\n"
        f"Клиенты: {summary.customers}\n"
        f"Активные программы: {summary.programs}\n"
        f"Подключено форматов: {len(active_capabilities)}\n"
        f"Свободных времён: {len(open_slots)}\n\n"
        f"Ожидают отправки: {summary.dispatch_pending}\n"
        f"Успешно отправлено: {summary.dispatch_sent}\n"
        f"Требуют внимания: {summary.dispatch_attention}\n\n"
        "Здесь собраны основные инструменты управления бизнесом.",
        reply_markup=_admin_keyboard(business_id),
    )


async def open_admin_command(message: Message, state: FSMContext) -> None:
    user_id = control._user_id(message)
    accesses = await asyncio.to_thread(
        list_accessible_businesses,
        user_id=user_id,
    )
    await state.clear()
    if not accesses:
        await message.answer("Сначала создайте бизнес через /start.")
        return
    if len(accesses) == 1:
        await send_admin_panel(
            message,
            user_id=user_id,
            business_id=str(accesses[0].business.id),
        )
        return
    await message.answer(
        "Для какого бизнеса открыть админку?",
        reply_markup=control._keyboard(
            [
                [
                    (
                        access.business.name,
                        f"cpa:home:{control._uuid_token(access.business.id)}",
                    )
                ]
                for access in accesses
            ]
        ),
    )


@router.callback_query(F.data.startswith("cpa:home:"))
async def open_admin_panel(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = control._token_uuid(str(callback.data).split(":", 2)[2])
    await state.clear()
    await send_admin_panel(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


@router.callback_query(F.data.startswith("cpa:formats:"))
async def open_admin_formats(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = control._token_uuid(str(callback.data).split(":", 2)[2])
    await state.clear()
    await control._send_capability_setup(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


@router.callback_query(F.data.startswith("cpa:back:"))
async def leave_admin_panel(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = control._token_uuid(str(callback.data).split(":", 2)[2])
    await state.clear()
    await control._send_dashboard(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


def install_admin_dashboard_button(control_module: ModuleType) -> None:
    """Add an explicit admin entry point to every owner dashboard."""

    if bool(getattr(control_module, "_admin_dashboard_installed", False)):
        return
    original = control_module._dashboard_keyboard

    def dashboard_with_admin(
        business_id: str,
        capabilities: list[object],
    ) -> InlineKeyboardMarkup:
        markup = original(business_id, capabilities)
        button = InlineKeyboardButton(
            text="Админка",
            callback_data=f"cpa:home:{control_module._uuid_token(business_id)}",
        )
        return InlineKeyboardMarkup(
            inline_keyboard=[*markup.inline_keyboard, [button]]
        )

    control_module._dashboard_keyboard = dashboard_with_admin
    control_module._admin_dashboard_installed = True


__all__ = [
    "install_admin_dashboard_button",
    "open_admin_command",
    "router",
    "send_admin_panel",
]
