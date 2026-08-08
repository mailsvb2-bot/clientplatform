from __future__ import annotations

from types import ModuleType

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def install_sales_ui(simple_module: ModuleType) -> None:
    """Add one sales entry point without coupling the simple dashboard to sales code."""

    if bool(getattr(simple_module, "_sales_ui_installed", False)):
        return
    original_keyboard = simple_module._simple_keyboard

    def sales_keyboard(business_id: str) -> InlineKeyboardMarkup:
        current = original_keyboard(business_id)
        rows = [list(row) for row in current.inline_keyboard]
        token = simple_module.control._uuid_token(business_id)
        sales_row = [
            InlineKeyboardButton(
                text="💼 Продажи",
                callback_data=f"cps:s:{token}",
            )
        ]
        insertion = max(0, len(rows) - 1)
        rows.insert(insertion, sales_row)
        return InlineKeyboardMarkup(inline_keyboard=rows)

    simple_module._simple_keyboard = sales_keyboard
    simple_module._sales_ui_installed = True


__all__ = ["install_sales_ui"]
