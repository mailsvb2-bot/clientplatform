from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Protocol

from aiogram import Router
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


class _ControlModule(Protocol):
    _uuid_token: Callable[[str], str]


class _SimpleExperienceModule(Protocol):
    _simple_keyboard: Callable[[str], InlineKeyboardMarkup]
    _sales_ui_installed: bool
    _sales_operations_composed: bool
    control: _ControlModule
    router: Router


def install_sales_ui(simple_module: _SimpleExperienceModule) -> None:
    """Add the canonical sales entry and mutation surface to the owner UI."""

    if bool(getattr(simple_module, "_sales_ui_installed", False)):
        return
    original_keyboard = simple_module._simple_keyboard

    def sales_keyboard(business_id: str) -> InlineKeyboardMarkup:
        current = original_keyboard(business_id)
        rows = [list(row) for row in current.inline_keyboard]
        token = simple_module.control._uuid_token(business_id)
        sales_row = [
            InlineKeyboardButton(
                text="💬 Обращения и продажи",
                callback_data=f"cps:s:{token}",
            )
        ]
        insertion = max(0, len(rows) - 1)
        rows.insert(insertion, sales_row)
        return InlineKeyboardMarkup(inline_keyboard=rows)

    simple_module._simple_keyboard = sales_keyboard

    # U-008 mutations are a presentation adapter over the existing canonical
    # sales application/repository. Compose them before the read-only sales
    # router so the owner can actually execute assignment, stage, next-action,
    # due and note operations without creating another CRM/business brain.
    operations = importlib.import_module(
        ".clientplatform_sales_operations",
        __package__,
    )
    sales = importlib.import_module(".clientplatform_sales", __package__)
    operations.install_sales_operations(sales)
    if not bool(getattr(simple_module, "_sales_operations_composed", False)):
        simple_module.router.include_router(operations.router)
        simple_module._sales_operations_composed = True

    simple_module._sales_ui_installed = True


__all__ = ["install_sales_ui"]
