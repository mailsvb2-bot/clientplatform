from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from clientplatform.application.customer_activity import platform_customer_activity
from handlers.admin_inline_common import AdminCtx, safe_edit_admin
from handlers.text_input import AdminInputState


def _callback_message(cb: CallbackQuery) -> Message | None:
    message = cb.message
    return message if isinstance(message, Message) else None


def _short_time(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "—"
    return raw.replace("T", " ")[:16]


def _platform_label(value: str) -> str:
    return {"telegram": "Telegram", "vk": "VK", "max": "MAX"}.get(value, value.upper())


def _activity_text(summary) -> str:
    channels = " · ".join(
        f"{_platform_label(platform)} {summary.by_platform.get(platform, 0)}"
        for platform in ("telegram", "vk", "max")
    )
    lines = [
        "👥 Активность пользователей ClientPlatform",
        "",
        f"Всего клиентов: {summary.total}",
        f"Новые сегодня: {summary.new_today}",
        f"Новые за 7 дней: {summary.new_7d}",
        f"Активны сегодня: {summary.active_today}",
        f"Каналы: {channels}",
        "",
        "Последние контакты:",
    ]
    if not summary.recent:
        lines.append("• Пока нет клиентской активности.")
        return "\n".join(lines)
    for row in summary.recent:
        handle = f" @{row.username}" if row.username else ""
        name = row.display_name or "Клиент"
        platforms = "/".join(_platform_label(item) for item in row.platforms) or "—"
        business = f" · {row.business_name}" if row.business_name else ""
        lines.append(
            f"• {name}{handle}{business}\n"
            f"  {platforms} · первый {_short_time(row.first_contact_at)} · "
            f"последний {_short_time(row.last_contact_at)}"
        )
    return "\n".join(lines)


async def handle(cb: CallbackQuery, state: FSMContext, data: str, ctx: AdminCtx) -> bool:
    if data in {"admin:users:today", "admin:users:activity"}:
        summary = platform_customer_activity(
            requester_user_id=int(cb.from_user.id),
            limit=25,
        )
        await safe_edit_admin(
            cb,
            state,
            _activity_text(summary),
            reply_markup=ctx.staff_kb,
        )
        return True

    if data == "admin:user:card":
        await state.set_state(AdminInputState.user_card)
        message = _callback_message(cb)
        if message is None:
            return True
        await message.answer(
            "🔎 Карточка пользователя\n\n"
            "Пожалуйста, отправьте user_id (числом).\n"
            "Например: 123456789",
        )
        return True

    return False
