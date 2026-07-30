from __future__ import annotations

import asyncio
import importlib
import logging
from typing import Any

from aiogram import F, Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BotCommand, CallbackQuery, Message

from clientplatform.application.activity import claim_customer_invite
from clientplatform.application.bookings import list_customer_businesses
from clientplatform.application.tenancy import list_accessible_businesses

control = importlib.import_module(".clientplatform_control", __package__)

router = Router(name="clientplatform_entry")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())

log = logging.getLogger(__name__)


async def register_clientplatform_bot_commands(bot: Bot) -> bool:
    """Expose the canonical entry commands in Telegram's command menu.

    Telegram itself controls the large first-run START button. Registering
    commands makes `/start` visible after that first run as well. A temporary
    Bot API failure must not prevent the polling process from starting.
    """

    try:
        confirmed = await bot.set_my_commands(
            [
                BotCommand(command="start", description="Открыть ClientPlatform"),
                BotCommand(command="mybot", description="Управление моим Telegram-ботом"),
            ]
        )
    except TelegramAPIError:
        log.warning("Failed to register ClientPlatform Telegram commands", exc_info=True)
        return False
    return confirmed is True


def _entry_keyboard():
    return control._keyboard(
        [
            [("Мои бизнесы", "cp:entry:businesses")],
            [("Мои специалисты и программы", "cp:entry:clients")],
        ]
    )


async def _send_business_choice(
    message: Message,
    *,
    user_id: int,
    accesses: list[Any],
    state: FSMContext,
) -> None:
    if len(accesses) > 1:
        await state.clear()
        await message.answer(
            "Выберите бизнес, с которым хотите работать:",
            reply_markup=control._business_choice_keyboard(accesses),
        )
        return
    await control._resume_business(
        message,
        user_id=user_id,
        business_id=accesses[0].business.id,
        state=state,
    )


@router.message(CommandStart())
async def clientplatform_entry_start(
    message: Message,
    state: FSMContext,
    managed_bot_business_id: str | None = None,
) -> None:
    user_id = control._user_id(message)
    if managed_bot_business_id is not None:
        links = await asyncio.to_thread(
            list_customer_businesses,
            telegram_user_id=user_id,
        )
        managed_links = [
            link for link in links if link.business_id == managed_bot_business_id
        ]
        await state.clear()
        if not managed_links:
            await message.answer(
                "Не удалось открыть кабинет этого специалиста. "
                "Попробуйте ещё раз через несколько секунд."
            )
            return
        await control._send_client_portal(message, links=managed_links)
        return

    payload = control._start_payload(message)
    if payload.startswith("cpj_"):
        token = payload.removeprefix("cpj_")
        user = message.from_user
        claim = await asyncio.to_thread(
            claim_customer_invite,
            token=token,
            telegram_user_id=user_id,
            username=None if user is None else user.username,
            display_name=None if user is None else user.full_name,
        )
        await state.clear()
        detail = "Вы уже были подключены." if claim.already_connected else "Подключение завершено."
        await message.answer(
            f"Вы подключены к «{claim.business_name}». {detail}\n"
            "Материалы и сообщения этого специалиста будут приходить сюда.",
            reply_markup=control._client_portal_keyboard(claim.business_id),
        )
        return

    accesses, links = await asyncio.gather(
        asyncio.to_thread(list_accessible_businesses, user_id=user_id),
        asyncio.to_thread(list_customer_businesses, telegram_user_id=user_id),
    )
    if accesses and links:
        await state.clear()
        await message.answer(
            "У Вас есть два рабочих пространства. Выберите, куда перейти:",
            reply_markup=_entry_keyboard(),
        )
        return
    if accesses:
        await _send_business_choice(
            message,
            user_id=user_id,
            accesses=accesses,
            state=state,
        )
        return
    if links:
        await state.clear()
        await control._send_client_portal(message, links=links)
        return

    await state.set_state(control.ClientPlatformControlState.business_name)
    await message.answer(
        "Добро пожаловать в ClientPlatform.\n\n"
        "Сначала напишите название Вашего дела, проекта или практики."
    )


@router.callback_query(F.data == "cp:entry:businesses")
async def open_business_workspace(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = int(callback.from_user.id)
    accesses = await asyncio.to_thread(list_accessible_businesses, user_id=user_id)
    await callback.answer()
    if not accesses:
        await control._callback_message(callback).answer(
            "Активных бизнесов больше нет. Нажмите /start, чтобы обновить меню."
        )
        return
    await _send_business_choice(
        control._callback_message(callback),
        user_id=user_id,
        accesses=accesses,
        state=state,
    )


@router.callback_query(F.data == "cp:entry:clients")
async def open_customer_workspace(callback: CallbackQuery, state: FSMContext) -> None:
    links = await asyncio.to_thread(
        list_customer_businesses,
        telegram_user_id=int(callback.from_user.id),
    )
    await callback.answer()
    if not links:
        await control._callback_message(callback).answer(
            "Активных подключений к специалистам больше нет. Нажмите /start, чтобы обновить меню."
        )
        return
    await state.clear()
    await control._send_client_portal(control._callback_message(callback), links=links)


@router.errors()
async def clientplatform_entry_error(event: object) -> bool:
    return await control.clientplatform_control_error(event)


if not bool(getattr(control, "_dual_role_entry_composed", False)):
    original_router = control.router
    program_builder = importlib.import_module(
        ".clientplatform_program_builder",
        __package__,
    )
    lesson_editor = importlib.import_module(
        ".clientplatform_program_lesson_editor_composition",
        __package__,
    )
    router.include_router(lesson_editor.router)
    router.include_router(program_builder.router)
    router.include_router(original_router)
    control.router = router
    control._program_lesson_editor_composed = True
    control._multi_lesson_program_builder_composed = True
    control._dual_role_entry_composed = True
