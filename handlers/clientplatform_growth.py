from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from clientplatform.application.growth_cockpit import get_growth_cockpit
from clientplatform.application.sales_ui import list_sales_handoff_work, list_sales_work
from clientplatform.application.tenancy import list_accessible_businesses, resolve_tenant_context
from clientplatform.application.control_callbacks import token_uuid, uuid_token
from clientplatform.runtime.control_bot import control_bot_enabled
from dashboard.growth_cockpit import telegram_growth_summary

router = Router(name="clientplatform_growth")


class ClientPlatformGrowthEnabled(BaseFilter):
    async def __call__(self, _event: object) -> bool:
        return control_bot_enabled()


router.message.filter(ClientPlatformGrowthEnabled())
router.callback_query.filter(ClientPlatformGrowthEnabled())


def _keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


def _message(callback: CallbackQuery) -> Message:
    if not isinstance(callback.message, Message):
        raise ValueError("growth cockpit callback has no accessible message")
    return callback.message


async def _actor(user_id: int, business_id: str):
    return await asyncio.to_thread(
        resolve_tenant_context,
        user_id=user_id,
        business_id=business_id,
    )


def _cockpit_keyboard(*, business_id: str, period_days: int, action_key: str) -> InlineKeyboardMarkup:
    token = uuid_token(business_id)
    rows = [
        [
            ("7 дней" if period_days != 7 else "✓ 7 дней", f"cpg:period:{token}:7"),
            ("30 дней" if period_days != 30 else "✓ 30 дней", f"cpg:period:{token}:30"),
        ]
    ]
    if action_key != "none":
        rows.append([("Что требует моего действия", f"cpg:attention:{token}")])
    rows.append([("Клиенты", f"cp:clients:{token}"), ("Результаты", f"cp:results:{token}")])
    return _keyboard(rows)


async def _send_cockpit(
    message: Message,
    *,
    user_id: int,
    business_id: str,
    period_days: int,
) -> None:
    actor = await _actor(user_id, business_id)
    snapshot = await asyncio.to_thread(
        get_growth_cockpit,
        actor=actor,
        period_days=period_days,
    )
    await message.answer(
        telegram_growth_summary(snapshot),
        reply_markup=_cockpit_keyboard(
            business_id=snapshot.business_id,
            period_days=snapshot.period_days,
            action_key=snapshot.next_action.action_key,
        ),
    )


@router.message(Command("today"))
async def growth_today(message: Message) -> None:
    if message.from_user is None:
        raise ValueError("growth cockpit requires a Telegram user")
    user_id = int(message.from_user.id)
    accesses = await asyncio.to_thread(list_accessible_businesses, user_id=user_id)
    if not accesses:
        await message.answer("Сначала создайте бизнес в ClientPlatform через /start.")
        return
    if len(accesses) == 1:
        await _send_cockpit(
            message,
            user_id=user_id,
            business_id=accesses[0].business.id,
            period_days=7,
        )
        return
    await message.answer(
        "Для какого бизнеса показать, что происходит сегодня?",
        reply_markup=_keyboard(
            [
                [(access.business.name, f"cpg:business:{uuid_token(access.business.id)}")]
                for access in accesses
            ]
        ),
    )


@router.callback_query(F.data.startswith("cpg:business:"))
async def growth_choose_business(callback: CallbackQuery) -> None:
    business_id = token_uuid(str(callback.data).split(":", 2)[2])
    await callback.answer()
    await _send_cockpit(
        _message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
        period_days=7,
    )


@router.callback_query(F.data.startswith("cpg:period:"))
async def growth_change_period(callback: CallbackQuery) -> None:
    _, _, business_token, raw_period = str(callback.data).split(":", 3)
    try:
        period_days = int(raw_period)
    except ValueError as exc:
        raise ValueError("growth cockpit period is invalid") from exc
    if period_days not in {7, 30}:
        raise ValueError("growth cockpit period must be 7 or 30 days")
    await callback.answer()
    await _send_cockpit(
        _message(callback),
        user_id=int(callback.from_user.id),
        business_id=token_uuid(business_token),
        period_days=period_days,
    )


@router.callback_query(F.data.startswith("cpg:attention:"))
async def growth_attention(callback: CallbackQuery) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    business_id = token_uuid(business_token)
    actor = await _actor(int(callback.from_user.id), business_id)
    handoffs, sales_work = await asyncio.gather(
        asyncio.to_thread(list_sales_handoff_work, actor=actor, limit=12),
        asyncio.to_thread(list_sales_work, actor=actor, limit=12),
    )

    lines: list[str] = []
    if handoffs:
        lines.append("Нужен ответ или решение владельца:")
        for item in handoffs[:8]:
            lines.append(f"• {item.get('customer_name') or 'Клиент'}")
    planned = [item for item in sales_work if str(item.get("next_action_kind") or "").strip()]
    if planned:
        if lines:
            lines.append("")
        lines.append("Уже подготовлен следующий шаг:")
        for item in planned[:8]:
            lines.append(
                f"• {item.get('customer_name') or 'Клиент'} — "
                "ClientPlatform продолжит по существующему плану продаж."
            )
    if not lines:
        lines.append("Сейчас нет клиентских ситуаций, требующих отдельного действия.")

    await callback.answer()
    await _message(callback).answer(
        "Что требует решения\n\n" + "\n".join(lines),
        reply_markup=_keyboard(
            [[("Открыть клиентов", f"cp:clients:{business_token}")]]
        ),
    )


@router.errors()
async def clientplatform_growth_error(event: object) -> bool:
    exception = getattr(event, "exception", None)
    update = getattr(event, "update", None)
    if not isinstance(exception, (ValueError, RuntimeError)):
        return False
    message = getattr(update, "message", None)
    callback = getattr(update, "callback_query", None)
    if isinstance(message, Message):
        await message.answer(f"Не получилось показать состояние бизнеса: {exception}")
        return True
    if isinstance(callback, CallbackQuery):
        await callback.answer("Не получилось обновить данные бизнеса.", show_alert=True)
        return True
    return False


__all__ = ["router"]
