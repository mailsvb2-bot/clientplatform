from __future__ import annotations

import asyncio
import logging

import sqlite3
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestUser,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from handlers.admin_inline_common import AdminCtx, safe_edit_admin
from handlers.admin_inline_states import AdminManageState


from core.callback_utils import safe_answer_callback


def _callback_message(cb: CallbackQuery) -> Message | None:
    message = cb.message
    return message if isinstance(message, Message) else None


def _list_admin_ids_sync() -> list[int]:
    from services.admin_permissions import list_admin_ids

    return list(list_admin_ids())


def _allowed_perms_sync(target_id: int):
    from services.admin_permissions import get_allowed_perms

    return get_allowed_perms(int(target_id))


def _toggle_perm_sync(target_id: int, perm: str, updated_by: int) -> None:
    from services.admin_permissions import PERMS, get_allowed_perms, set_perm, toggle_perm

    # If no explicit restrictions exist, persist "all allowed" first, then toggle the specific one.
    if get_allowed_perms(int(target_id)) is None:
        for item in PERMS:
            set_perm(int(target_id), item.perm, True, updated_by=int(updated_by))
    toggle_perm(int(target_id), str(perm), updated_by=int(updated_by))


def _grant_role_sync(target_id: int, role: str) -> None:
    from services.roles import grant_role

    grant_role(int(target_id), str(role))


async def handle(cb: CallbackQuery, state: FSMContext, data: str, ctx: AdminCtx) -> bool:
    log = logging.getLogger(__name__)

    if data == "admin:perms":
        if not ctx.is_superadmin:
            await safe_answer_callback(cb, "", show_alert=False)
            return True

        admin_ids = await asyncio.to_thread(_list_admin_ids_sync)
        if not admin_ids:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")]]
            )
            await safe_edit_admin(cb, state, "🔐 Доступы админов\n\nПока нет администраторов с ролями.", reply_markup=kb)
            return True

        rows: list[list[InlineKeyboardButton]] = []
        for uid in admin_ids:
            rows.append([InlineKeyboardButton(text=str(uid), callback_data=f"admin:perms:user:{uid}")])
        rows.append([InlineKeyboardButton(text="⬅️ Админ-меню", callback_data="admin:menu")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await safe_edit_admin(cb, state, "🔐 Выберите администратора:", reply_markup=kb)
        return True

    if data.startswith("admin:perms:user:"):
        if not ctx.is_superadmin:
            await safe_answer_callback(cb, "", show_alert=False)
            return True
        try:
            target_id = int(data.split(":")[-1])
        except ValueError:
            await safe_answer_callback(cb, "", show_alert=False)
            return True

        from services.admin_permissions import PERMS

        allowed = await asyncio.to_thread(_allowed_perms_sync, target_id)
        allowed_set = allowed or set()

        buttons: list[list[InlineKeyboardButton]] = []
        for item in PERMS:
            mark = "✅" if (allowed is None or item.perm in allowed_set) else "⬜️"
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"{mark} {item.title}",
                        callback_data=f"admin:perms:toggle:{target_id}:{item.perm}",
                    )
                ]
            )
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:perms")])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await safe_edit_admin(
            cb,
            state,
            f"🔐 Права администратора {target_id}\n\nНажимайте, чтобы включить/выключить.",
            reply_markup=kb,
        )
        return True

    if data.startswith("admin:perms:toggle:"):
        if not ctx.is_superadmin:
            await safe_answer_callback(cb, "", show_alert=False)
            return True
        parts = data.split(":", 4)
        if len(parts) < 5:
            await safe_answer_callback(cb, "", show_alert=False)
            return True
        try:
            target_id = int(parts[3])
        except ValueError:
            await safe_answer_callback(cb, "", show_alert=False)
            return True
        perm = parts[4]

        await asyncio.to_thread(_toggle_perm_sync, target_id, perm, int(ctx.uid))

        # Re-render the same screen (no recursion to admin_inline()).
        return await handle(cb, state, f"admin:perms:user:{target_id}", ctx)

    if data == "admin:add_admin":
        if not ctx.is_superadmin:
            await safe_answer_callback(cb, "", show_alert=False)
            return True

        await state.clear()
        await state.set_state(AdminManageState.waiting_admin_user)

        reply_kb = ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(
                        text="Выбрать пользователя",
                        request_user=KeyboardButtonRequestUser(request_id=1, user_is_bot=False),
                    ),
                    KeyboardButton(text="Отмена"),
                ]
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        message = _callback_message(cb)
        if message is None:
            return True
        await message.answer(
            "👥 Добавить администратора\n\n"
            "Можно любым способом:\n"
            "• Нажмите «Выбрать пользователя» (пикер Telegram)\n"
            "• Или просто перешлите сюда сообщение от нужного человека\n"
            "• Или отправьте @username\n"
            "• Или отправьте числом его user_id\n\n"
            "Отмена — кнопкой ниже.",
            reply_markup=reply_kb,
        )
        return True

    if data.startswith("admin:add_admin_role:"):
        if not ctx.is_superadmin:
            await safe_answer_callback(cb, "", show_alert=False)
            return True
        parts = data.split(":")
        if len(parts) != 4:
            await safe_answer_callback(cb, "", show_alert=False)
            return True
        role = parts[2]
        try:
            target_id = int(parts[3])
        except ValueError:
            await safe_answer_callback(cb, "", show_alert=False)
            return True

        try:
            await asyncio.to_thread(_grant_role_sync, target_id, role)
        except ImportError:
            log.exception("Не удалось выдать роль")
            await safe_answer_callback(cb, "Ошибка", show_alert=True)
            return True
        except (sqlite3.Error, ValueError, TypeError):
            log.exception("Не удалось выдать роль")
            await safe_answer_callback(cb, "Ошибка", show_alert=True)
            return True

        await state.clear()
        message = _callback_message(cb)
        if message is None:
            return True
        await message.answer(
            f"✅ Пользователю {target_id} назначена роль: {role}",
            reply_markup=ReplyKeyboardRemove(),
        )
        return True

    return False
