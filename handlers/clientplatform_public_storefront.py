from __future__ import annotations

"""Parse permanent public ClientPlatform links without ambiguous token splitting."""

import asyncio
from types import ModuleType
from typing import Awaitable, Callable

from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from clientplatform.application.owner_booking_journey import (
    connect_public_storefront_customer,
    is_public_storefront_staff,
)

_TOKEN_LENGTH = 22


async def dispatch_public_start(
    original: Callable[..., Awaitable[None]],
    message: Message,
    state: FSMContext,
    *,
    user_id: int,
    managed_bot_business_id: str | None,
) -> None:
    owner = __import__(
        "handlers.clientplatform_owner_journey",
        fromlist=["clientplatform_owner_journey"],
    )
    control = owner.control
    payload = control._start_payload(message)
    business_token = ""
    slot_token = ""
    if managed_bot_business_id is None and payload.startswith(owner._PUBLIC_START_PREFIX):
        candidate = payload.removeprefix(owner._PUBLIC_START_PREFIX)
        if len(candidate) == _TOKEN_LENGTH:
            business_token = candidate
    elif managed_bot_business_id is None and payload.startswith(owner._PUBLIC_SLOT_PREFIX):
        encoded = payload.removeprefix(owner._PUBLIC_SLOT_PREFIX)
        if (
            len(encoded) == (_TOKEN_LENGTH * 2 + 1)
            and encoded[_TOKEN_LENGTH] == "_"
        ):
            business_token = encoded[:_TOKEN_LENGTH]
            slot_token = encoded[_TOKEN_LENGTH + 1 :]
    if not business_token:
        await original(
            message,
            state,
            user_id=user_id,
            managed_bot_business_id=managed_bot_business_id,
        )
        return

    business_id = control._token_uuid(business_token)
    if await asyncio.to_thread(
        is_public_storefront_staff,
        business_id=business_id,
        telegram_user_id=user_id,
    ):
        # Public deep links are deliberately customer acquisition surfaces. Staff
        # must not become customers of their own tenant merely by testing a link.
        # Route them back to the existing safe preview/dashboard callbacks before
        # the customer-connect transaction is even attempted.
        await state.clear()
        rows: list[list[tuple[str, str]]] = []
        if slot_token:
            rows.append(
                [
                    (
                        "👀 Посмотреть глазами клиента",
                        f"cpj:preview:{business_token}:{slot_token}",
                    )
                ]
            )
        rows.append([("🏠 В мой кабинет", f"cpj:home:{business_token}")])
        await message.answer(
            "Это Ваша публичная ссылка для клиентов. Вы открыли её как сотрудник "
            "этого бизнеса, поэтому ClientPlatform не создаёт для Вас клиентскую "
            "карточку. Используйте безопасный предпросмотр или вернитесь в кабинет.",
            reply_markup=control._keyboard(rows),
        )
        return

    user = message.from_user
    link = await asyncio.to_thread(
        connect_public_storefront_customer,
        business_id=business_id,
        telegram_user_id=user_id,
        username=None if user is None else user.username,
        display_name=None if user is None else user.full_name,
    )
    await state.clear()
    slots = await asyncio.to_thread(
        control.list_customer_booking_slots,
        telegram_user_id=user_id,
        business_id=business_id,
    )
    focused_slot_id = control._token_uuid(slot_token) if slot_token else None
    await owner._send_public_storefront(
        message,
        business_id=business_id,
        business_name=link.business_name,
        slots=slots,
        focused_slot_id=focused_slot_id,
    )


def install_public_storefront(owner_module: ModuleType) -> None:
    if bool(getattr(owner_module, "_public_storefront_parser_installed", False)):
        return
    owner_module._dispatch_public_start = dispatch_public_start
    owner_module._public_storefront_parser_installed = True


__all__ = ["dispatch_public_start", "install_public_storefront"]
