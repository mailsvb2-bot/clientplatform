import asyncio
import logging
import sqlite3

from aiogram import Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from clientplatform.runtime.platform_resource_monitor import (
    start_platform_resource_monitor,
    stop_platform_resource_monitor,
)
from core.callback_utils import safe_answer_callback
from handlers import admin_inline_tariffs
from handlers.admin_inline_common import (
    AdminCtx,
    admin_nav_back,
    get_staff_roles,
    is_superadmin,
    safe_edit_admin,
)
from handlers.admin_inline_copy import handle as handle_copy
from handlers.admin_inline_perms import handle as handle_perms
from handlers.admin_inline_reports import handle as handle_reports
from handlers.admin_inline_resources import handle as handle_resources
from handlers.admin_inline_roles import handle as handle_roles
from handlers.admin_inline_states import AdminManageState
from handlers.admin_inline_users import handle as handle_users
from keyboards.inline import kb_staff_menu
from services.admin import is_admin
from services.admin_permissions import admin_callback_allowed
from services.roles import ROLE_ADMIN, ROLE_MARKETING

router = Router()
router.startup.register(start_platform_resource_monitor)
router.shutdown.register(stop_platform_resource_monitor)


def _load_admin_ctx(uid: int) -> AdminCtx | None:
    if not is_admin(uid):
        return None

    roles = get_staff_roles(int(uid))
    if not roles:
        return None

    from services.admin_permissions import SALES_DESK_PERMISSION, get_allowed_perms

    superadmin = is_superadmin(int(uid))
    allowed = None
    if not superadmin:
        allowed = get_allowed_perms(int(uid))

    staff_kb = kb_staff_menu(
        roles,
        is_superadmin=superadmin,
        allowed_perms=allowed,
    )
    can_see_sales = (
        superadmin
        or allowed is None
        or SALES_DESK_PERMISSION in allowed
    ) and (ROLE_MARKETING in roles or ROLE_ADMIN in roles or superadmin)
    if can_see_sales and not any(
        button.callback_data == "admin:sales"
        for row in staff_kb.inline_keyboard
        for button in row
    ):
        insert_at = 0
        for index, row in enumerate(staff_kb.inline_keyboard):
            if row and row[0].callback_data == "admin:growth:autopilot":
                insert_at = index + 1
                break
        staff_kb.inline_keyboard.insert(
            insert_at,
            [
                InlineKeyboardButton(
                    text="🧑‍💼 Sales Desk",
                    callback_data="admin:sales",
                )
            ],
        )
    if superadmin and not any(
        button.callback_data == "admin:resources"
        for row in staff_kb.inline_keyboard
        for button in row
    ):
        insert_at = max(0, len(staff_kb.inline_keyboard) - 1)
        staff_kb.inline_keyboard.insert(
            insert_at,
            [
                InlineKeyboardButton(
                    text="🧯 Лимиты и ресурсы",
                    callback_data="admin:resources",
                )
            ],
        )
    return AdminCtx(
        uid=int(uid),
        roles=roles,
        staff_kb=staff_kb,
        is_superadmin=superadmin,
        allowed_perms=allowed,
    )


def _grant_admin_role_sync(target_id: int) -> None:
    from services.roles import grant_role

    grant_role(int(target_id), ROLE_ADMIN)


async def _admin_ctx_from_message(msg: Message) -> AdminCtx | None:
    uid = msg.from_user.id if msg.from_user else None
    if uid is None:
        return None
    return await asyncio.to_thread(_load_admin_ctx, int(uid))


@router.callback_query(lambda c: (c.data or "").startswith("admin:"))
async def admin_gate(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id if cb.from_user else None
    ctx = (
        await asyncio.to_thread(_load_admin_ctx, int(uid))
        if uid is not None
        else None
    )
    if ctx is None:
        try:
            await safe_answer_callback(cb, "Недоступно.", show_alert=True)
        except (TelegramAPIError, asyncio.TimeoutError):
            logging.getLogger(__name__).exception("Unhandled exception")
        return

    roles = ctx.roles
    staff_kb = ctx.staff_kb
    data = cb.data or ""

    if await admin_nav_back(cb, state):
        return

    if not admin_callback_allowed(
        callback_data=data,
        roles=roles,
        is_superadmin=ctx.is_superadmin,
        allowed_perms=ctx.allowed_perms,
    ):
        logging.getLogger(__name__).warning(
            "Blocked unauthorized or stale admin callback: uid=%s callback=%s roles=%s",
            uid,
            data,
            sorted(roles),
        )
        await safe_answer_callback(
            cb,
            "Доступ к этому разделу отозван или не назначен.",
            show_alert=True,
        )
        return

    tariffs_ctx = admin_inline_tariffs.TariffsCtx(
        is_superadmin=ctx.is_superadmin,
        can_manage_tariffs=(ctx.is_superadmin or ROLE_ADMIN in roles),
        staff_kb=staff_kb,
    )
    if await admin_inline_tariffs.handle_tariffs_callback(
        cb,
        state,
        data,
        tariffs_ctx,
    ):
        return

    if await handle_copy(cb, state, data, ctx):
        return
    if await handle_perms(cb, state, data, ctx):
        return
    if await handle_roles(cb, state, data, ctx):
        return
    if await handle_users(cb, state, data, ctx):
        return
    if await handle_resources(cb, state, data, ctx):
        return
    if await handle_reports(cb, state, data, ctx):
        return

    if data == "admin:menu":
        await safe_edit_admin(
            cb,
            state,
            "🛠 Админ-панель\n\nВыберите раздел:",
            reply_markup=staff_kb,
            push=False,
            reset_stack=True,
        )
        return
    await safe_answer_callback(cb, "", show_alert=False)


@router.message(AdminManageState.waiting_tariffs_text)
async def admin_tariffs_input(msg: Message, state: FSMContext):
    uid = msg.from_user.id if msg.from_user else None
    await admin_inline_tariffs.admin_tariffs_input(
        msg,
        state,
        admin_id=uid,
    )


@router.message(AdminManageState.waiting_tariff_single_price)
async def admin_tariff_single_price_input(msg: Message, state: FSMContext):
    uid = msg.from_user.id if msg.from_user else None
    await admin_inline_tariffs.admin_tariff_single_price_input(
        msg,
        state,
        admin_id=uid,
    )


@router.message(AdminManageState.waiting_sales_note)
async def admin_sales_note_input(msg: Message, state: FSMContext):
    ctx = await _admin_ctx_from_message(msg)
    if ctx is None:
        await state.clear()
        await msg.answer("Недоступно.")
        return

    from handlers.admin_reports import sales_desk

    await sales_desk.handle_note_input(msg, state, ctx)


@router.message(AdminManageState.waiting_sales_message)
async def admin_sales_message_input(msg: Message, state: FSMContext):
    ctx = await _admin_ctx_from_message(msg)
    if ctx is None:
        await state.clear()
        await msg.answer("Недоступно.")
        return

    from handlers.admin_reports import sales_desk

    await sales_desk.handle_message_input(msg, state, ctx)


@router.message(AdminManageState.waiting_admin_user)
async def admin_add_admin_input(msg: Message, state: FSMContext):
    """Add admin by Telegram picker, forwarded message, username, or user_id."""
    uid = msg.from_user.id if msg.from_user else None
    if uid is None or not is_superadmin(int(uid)):
        logging.getLogger(__name__).warning(
            "Blocked non-superadmin admin grant state: uid=%s",
            uid,
        )
        await state.clear()
        return
    text = (msg.text or "").strip()

    if text.lower() in {"отмена", "cancel", "/cancel"}:
        await state.clear()
        await msg.answer("Ок, отменено.", reply_markup=ReplyKeyboardRemove())
        return

    target_id: int | None = None

    user_shared = getattr(msg, "user_shared", None)
    if user_shared and getattr(user_shared, "user_id", None):
        try:
            target_id = int(user_shared.user_id)
        except (TypeError, ValueError):
            target_id = None

    if target_id is None:
        forwarded = getattr(msg, "forward_from", None)
        if forwarded and getattr(forwarded, "id", None):
            try:
                target_id = int(forwarded.id)
            except (TypeError, ValueError):
                target_id = None

    if target_id is None and text.startswith("@") and len(text) > 1:
        try:
            chat = await msg.bot.get_chat(text)
            target_id = int(chat.id)
        except (TelegramAPIError, TelegramBadRequest):
            target_id = None
        except (ValueError, TypeError):
            target_id = None

    if target_id is None and text:
        try:
            if text.isdigit():
                target_id = int(text)
        except (TelegramAPIError, TelegramBadRequest):
            target_id = None
        except (ValueError, TypeError):
            target_id = None

    if target_id is None:
        await msg.answer(
            "Не понял кого добавить.\n\n"
            "Варианты:\n"
            "• нажмите «Выбрать пользователя»\n"
            "• перешлите сообщение от человека\n"
            "• отправьте @username\n"
            "• или отправьте числом user_id\n\n"
            "Отмена — напишите «Отмена».",
        )
        return

    assigned_roles: set[str] = set()
    try:
        await asyncio.to_thread(_grant_admin_role_sync, int(target_id))
        assigned_roles = await asyncio.to_thread(get_staff_roles, int(target_id))
    except RuntimeError:
        logging.getLogger(__name__).exception("Failed to add admin")
    except OSError:
        logging.getLogger(__name__).exception("Failed to add admin")
    except sqlite3.Error:
        logging.getLogger(__name__).exception("Failed to add admin")
    except (ValueError, TypeError):
        logging.getLogger(__name__).exception("Failed to add admin")

    if ROLE_ADMIN not in assigned_roles:
        if assigned_roles:
            logging.getLogger(__name__).error(
                "Admin role was not persisted: target_id=%s roles=%s",
                target_id,
                sorted(assigned_roles),
            )
        await msg.answer(
            "Не удалось добавить администратора. Изменения не подтверждены — попробуйте ещё раз.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.clear()
        return

    await state.clear()

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👥 Роли команды",
                    callback_data="admin:roles:list",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔐 Доступы админов",
                    callback_data="admin:perms",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Админ-меню",
                    callback_data="admin:menu",
                )
            ],
        ]
    )
    await msg.answer(
        f"✅ Добавил администратора: {int(target_id)}\n\n"
        "Теперь можно назначить роли и настроить доступы.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await msg.answer("Куда дальше?", reply_markup=keyboard)
