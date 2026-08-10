from __future__ import annotations

import asyncio
import importlib
import logging
import sqlite3
from typing import Any

from aiogram import F, Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BotCommand, CallbackQuery, Message

from clientplatform.application.activity import claim_customer_invite
from clientplatform.application.bookings import list_customer_businesses
from clientplatform.application.tenancy import list_accessible_businesses
from clientplatform.domain.activity import ActivityInvariantViolation
from services.db.core import db_operation_deadline

control = importlib.import_module(".clientplatform_control", __package__)

router = Router(name="clientplatform_entry")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())

log = logging.getLogger(__name__)
_START_TIMEOUT_SECONDS = 12.0
_START_STORAGE_DEADLINE_SECONDS = 8.0


async def register_clientplatform_bot_commands(bot: Bot) -> bool:
    """Expose the canonical entry commands in Telegram's command menu."""

    try:
        confirmed = await bot.set_my_commands(
            [
                BotCommand(command="start", description="Открыть ClientPlatform"),
                BotCommand(command="admin", description="Открыть админку бизнеса"),
                BotCommand(command="mybot", description="Управление моим Telegram-ботом"),
                BotCommand(command="cancel", description="Отменить текущий шаг"),
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


async def _safe_edit_start_status(status_message: Message | None, text: str) -> None:
    if status_message is None:
        return
    try:
        await status_message.edit_text(text)
    except TelegramAPIError:
        log.warning("Failed to edit ClientPlatform /start status", exc_info=True)


async def _safe_delete_start_status(status_message: Message | None) -> None:
    if status_message is None:
        return
    try:
        await status_message.delete()
    except TelegramAPIError:
        log.debug("Failed to delete ClientPlatform /start status", exc_info=True)


async def _dispatch_clientplatform_start(
    message: Message,
    state: FSMContext,
    *,
    user_id: int,
    managed_bot_business_id: str | None,
) -> None:
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
        try:
            claim = await asyncio.to_thread(
                claim_customer_invite,
                token=token,
                telegram_user_id=user_id,
                username=None if user is None else user.username,
                display_name=None if user is None else user.full_name,
            )
        except ActivityInvariantViolation as exc:
            await state.clear()
            await message.answer(str(exc))
            return
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

    simple = importlib.import_module(
        ".clientplatform_simple_experience",
        __package__,
    )
    await state.clear()
    await message.answer(
        simple.welcome_text(),
        reply_markup=simple.welcome_keyboard(),
    )


@router.message(CommandStart())
async def clientplatform_entry_start(
    message: Message,
    state: FSMContext,
    managed_bot_business_id: str | None = None,
) -> None:
    """Acknowledge `/start` before storage work and fail visibly on stalls."""

    user_id = control._user_id(message)
    status_message = await message.answer("Открываю…")
    try:
        with db_operation_deadline(_START_STORAGE_DEADLINE_SECONDS):
            await asyncio.wait_for(
                _dispatch_clientplatform_start(
                    message,
                    state,
                    user_id=user_id,
                    managed_bot_business_id=managed_bot_business_id,
                ),
                timeout=_START_TIMEOUT_SECONDS,
            )
    except TimeoutError:
        log.error(
            "ClientPlatform /start timed out user_id=%s timeout_seconds=%s",
            user_id,
            _START_TIMEOUT_SECONDS,
        )
        await _safe_edit_start_status(
            status_message,
            "ClientPlatform отвечает дольше обычного. "
            "Нажмите «Старт» ещё раз через несколько секунд.",
        )
        return
    except sqlite3.DatabaseError:
        log.exception("ClientPlatform /start storage failed user_id=%s", user_id)
        await _safe_edit_start_status(
            status_message,
            "Не удалось открыть ClientPlatform. "
            "Нажмите «Старт» ещё раз — сохранённые данные не потеряны.",
        )
        return

    await _safe_delete_start_status(status_message)


@router.message(Command("admin"))
async def clientplatform_admin_command(message: Message, state: FSMContext) -> None:
    """Open the owner administration panel before generic FSM handlers."""

    admin = importlib.import_module(".clientplatform_admin", __package__)
    await admin.open_admin_command(message, state)


@router.message(Command("mybot"))
async def clientplatform_mybot_command(message: Message, state: FSMContext) -> None:
    """Route `/mybot` before generic FSM text handlers can persist it as data."""

    bot_setup = importlib.import_module(".clientplatform_bot_setup", __package__)
    await bot_setup.open_my_bot_command(message, state)


@router.message(Command("cancel"))
async def clientplatform_cancel_command(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    await state.clear()
    if current_state:
        await message.answer("Текущий шаг отменён. Нажмите /start, чтобы открыть кабинет.")
        return
    await message.answer("Сейчас нет незавершённого шага. Нажмите /start.")


@router.message(F.text.startswith("/"))
async def clientplatform_unknown_command(message: Message, state: FSMContext) -> None:
    """Never allow a Telegram command to become a business/user field value."""

    await state.clear()
    await message.answer(
        "Команда не была сохранена как данные. "
        "Доступны /start, /admin, /mybot и /cancel."
    )


@router.callback_query(F.data == "cp:entry:businesses")
async def open_business_workspace(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = int(callback.from_user.id)
    accesses = await asyncio.to_thread(list_accessible_businesses, user_id=user_id)
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
    if not links:
        await control._callback_message(callback).answer(
            "Активных подключений к специалистам больше нет. Нажмите /start, чтобы обновить меню."
        )
        return
    await state.clear()
    await control._send_client_portal(control._callback_message(callback), links=links)


@router.errors()
async def clientplatform_entry_error(event: object) -> bool:
    if await control.clientplatform_control_error(event):
        return True

    exception = getattr(event, "exception", None)
    update = getattr(event, "update", None)
    if not isinstance(exception, Exception):
        return False

    log.error(
        "Unhandled ClientPlatform interaction failure",
        exc_info=(type(exception), exception, exception.__traceback__),
    )
    message = getattr(update, "message", None)
    callback = getattr(update, "callback_query", None)
    try:
        if isinstance(message, Message):
            await message.answer(
                "Не удалось продолжить настройку ClientPlatform. "
                "Отправьте /start — сохранённые данные не потеряны."
            )
            return True
        if isinstance(callback, CallbackQuery):
            await callback.answer(
                "Не удалось выполнить действие. Откройте ClientPlatform через /start.",
                show_alert=True,
            )
            return True
    except TelegramAPIError:
        log.warning("Failed to report ClientPlatform interaction failure", exc_info=True)
    return False


if not bool(getattr(control, "_dual_role_entry_composed", False)):
    original_router = control.router
    interaction_safety = importlib.import_module(
        ".clientplatform_interaction_safety",
        __package__,
    )
    interaction_safety.install_interaction_safety(router, control)
    admin = importlib.import_module(
        ".clientplatform_admin",
        __package__,
    )
    admin.install_admin_dashboard_button(control)
    admin_callback_guard = importlib.import_module(
        ".clientplatform_admin_callback_guard",
        __package__,
    )
    admin_callback_guard.install_admin_callback_namespace_guard(admin, control)
    dashboard_dispatch = importlib.import_module(
        ".clientplatform_dashboard_dispatch",
        __package__,
    )
    dashboard_dispatch.install_dynamic_dashboard_dispatch(control)
    onboarding_recovery = importlib.import_module(
        ".clientplatform_onboarding_recovery",
        __package__,
    )
    program_media = importlib.import_module(
        ".clientplatform_program_media_router",
        __package__,
    )
    program_builder = importlib.import_module(
        ".clientplatform_program_builder",
        __package__,
    )
    simple_experience = importlib.import_module(
        ".clientplatform_simple_experience",
        __package__,
    )
    booking_wizard_ux = importlib.import_module(
        ".clientplatform_booking_wizard_ux",
        __package__,
    )
    cloud_media = importlib.import_module(
        ".clientplatform_cloud_media",
        __package__,
    )
    lesson_editor = importlib.import_module(
        ".clientplatform_program_lesson_editor_composition",
        __package__,
    )
    router.include_router(admin.router)
    router.include_router(interaction_safety.router)
    router.include_router(onboarding_recovery.router)
    # Booking wizard UX must precede the legacy/simple router because it owns
    # the same booking_start FSM state and intentionally replaces only that
    # prompt with one-click duration choices.
    router.include_router(booking_wizard_ux.router)
    router.include_router(simple_experience.router)
    router.include_router(cloud_media.router)
    router.include_router(program_media.router)
    router.include_router(lesson_editor.router)
    router.include_router(program_builder.router)
    router.include_router(original_router)
    control.router = router
    control._admin_router_composed = True
    control._interaction_safety_router_composed = True
    control._onboarding_recovery_router_composed = True
    control._booking_wizard_ux_router_composed = True
    control._simple_experience_router_composed = True
    control._cloud_media_router_composed = True
    control._program_media_router_composed = True
    control._program_lesson_editor_composed = True
    control._multi_lesson_program_builder_composed = True
    control._dual_role_entry_composed = True
