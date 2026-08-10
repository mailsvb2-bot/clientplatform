from __future__ import annotations

import asyncio
from typing import Any

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from core.callback_utils import safe_answer_callback
from handlers.admin_inline_common import safe_edit_admin
from services.platform_resource_limits import (
    get_platform_resource_snapshot,
    render_platform_resource_status,
)


_CALLBACKS = {"admin:resources", "admin:resources:refresh"}


def _keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Обновить лимиты",
                    callback_data="admin:resources:refresh",
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


async def handle(
    cb: CallbackQuery,
    state: Any,
    data: str,
    ctx: Any,
) -> bool:
    if data not in _CALLBACKS:
        return False
    if not bool(getattr(ctx, "is_superadmin", False)):
        await safe_answer_callback(cb, "Только для супер-админа.", show_alert=True)
        return True

    snapshot = await asyncio.to_thread(get_platform_resource_snapshot)
    await safe_edit_admin(
        cb,
        state,
        render_platform_resource_status(snapshot),
        reply_markup=_keyboard(),
        push=data == "admin:resources",
    )
    return True


__all__ = ["handle"]
