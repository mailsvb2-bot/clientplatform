from __future__ import annotations

import asyncio
import logging
import sqlite3

from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, ReplyKeyboardRemove

from keyboards.inline import kb_main
from services.gift_store import set_target, get_target, clear_target
from services.payments.ui import kb, kb_back, kb_gift_tariffs, pick_user_keyboard
from services.pending import set_pending, peek_pending, pop_pending
from services.events import log_event
from services.promo_texts import get_gift_template
from services.messenger.links import build_gift_share_targets


logger = logging.getLogger(__name__)


def _callback_message(cb: CallbackQuery) -> Message | None:
    message = cb.message
    return message if isinstance(message, Message) else None


def _message_user_id(message: Message) -> int | None:
    user = message.from_user
    return int(user.id) if user else None


def _message_user_full_name(message: Message) -> str:
    user = message.from_user
    return (user.full_name or "").strip() if user else ""


_LEGACY_GIFT_PAYMENT_DISABLED = (
    "Этот старый способ оплаты подарка отключён. Откройте подарки заново и выберите актуальный пакет практик."
)


def _gift_share_keyboard(code: str, text: str):
    rows: list[list[InlineKeyboardButton]] = []
    for item in build_gift_share_targets(code, text=text):
        rows.append([InlineKeyboardButton(text=f"🎁 Отправить в {item['title']}", url=item['url'])])
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main")])
    return kb(rows)


async def gift_menu(cb: CallbackQuery) -> None:
    # Canonical flow: gift is a universal code/link. Recipient channel is selected after payment.
    message = _callback_message(cb)
    if message is None:
        return

    uid = int(cb.from_user.id)
    set_pending(uid, "gift_universal", {"from_name": (cb.from_user.full_name or "").strip()})
    log_event(uid, "gift_menu", {"mode": "universal_link"})

    await message.edit_text(
        "🎁 Подарить подписку — пакет практик\n\n"
        "Это разовая покупка пакета практик, а не автопродляемая подписка. "
        "Сначала выберите пакет и оплатите подарок. После оплаты проект даст ссылки для отправки подарка "
        "в Telegram, ВКонтакте или MAX.\n\n"
        "Получатель откроет подарок в выбранном мессенджере и войдёт в тот же маршрут Метротерапии.",
        reply_markup=kb_gift_tariffs(user_id=uid, back_cb="menu:main"),
    )


async def gift_pick_target(cb: CallbackQuery) -> None:
    # Legacy Telegram direct-target flow. Kept for old callback contracts, but no longer the main UX.
    message = _callback_message(cb)
    if message is None:
        return

    uid = int(cb.from_user.id)
    kb_r = pick_user_keyboard()
    if kb_r is None:
        await message.answer(
            "⚠️ Ваш клиент Telegram не поддерживает выбор пользователя кнопкой.\n"
            "Выберите пакет практик и после оплаты отправьте универсальную ссылку подарка в нужный мессенджер.",
            reply_markup=kb_gift_tariffs(user_id=uid, back_cb="menu:main"),
        )
        return
    set_pending(uid, "gift_target", {"from_name": (cb.from_user.full_name or "").strip()})
    await message.answer(
        "Выберите получателя в Telegram.\n"
        "После выбора откроется список пакетов практик.",
        reply_markup=kb_r,
    )


async def gift_pick_cancel(message: Message) -> None:
    """Cancel only the active gift scenario and delegate every other cancel."""
    uid = _message_user_id(message)
    if uid is None:
        raise SkipHandler
    pending = peek_pending(uid)
    if pending is None or pending.kind not in {"gift_target", "gift_universal"}:
        # The payments router is registered before share and other routers. Explicitly
        # delegate so a generic reply-keyboard label cannot swallow another flow.
        raise SkipHandler

    pop_pending(uid)
    clear_target(uid)
    await message.answer(
        "✅ Хорошо. Выбор подарка отменён.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await message.answer('Главное меню:', reply_markup=kb_main(user_id=uid))


async def gift_users_shared(message: Message, state: FSMContext) -> None:
    uid = _message_user_id(message)
    if uid is None:
        return
    # выбор получателя не должен попадать в чужие FSM-сценарии
    try:
        await state.clear()
    except (sqlite3.Error, RuntimeError):
        logger.exception("gift_users_shared: failed to clear FSM state (user_id=%s)", uid)

    peek = peek_pending(uid)
    if not peek or peek.kind != "gift_target":
        return
    p = pop_pending(uid)
    if not p:
        return

    try:
        shared_user = getattr(message, "user_shared", None)
        if shared_user is not None:
            to_id = int(shared_user.user_id)
        else:
            shared_users = getattr(message, "users_shared", None)
            picked = (getattr(shared_users, "users", None) or [])[:1]
            to_id = int(picked[0].user_id) if picked else 0
    except sqlite3.Error:
        logger.exception("gift_users_shared: failed to parse shared user (user_id=%s)", uid)
        to_id = 0
    except (RuntimeError, AttributeError, TypeError):
        logger.exception("gift_users_shared: failed to parse shared user (user_id=%s)", uid)
        to_id = 0
    except (ValueError, IndexError):
        logger.exception("gift_users_shared: failed to parse shared user (user_id=%s)", uid)
        to_id = 0

    if not to_id:
        await message.answer(
            "❌ Не удалось получить пользователя. Попробуйте ещё раз.",
            reply_markup=kb([
                [InlineKeyboardButton(text="👤 Выбрать получателя", callback_data="gift:pick_target")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="gift:menu")],
            ]),
        )
        return

    await message.answer("✅ Получатель выбран.", reply_markup=ReplyKeyboardRemove())

    set_target(uid, to_id)
    log_event(uid, "gift_target_picked", {"to_id": to_id})

    await message.answer(
        "Теперь выберите пакет практик для подарка:",
        reply_markup=kb_gift_tariffs(user_id=uid, back_cb="gift:menu"),
    )


async def gift_buy(cb: CallbackQuery) -> None:
    """Legacy Telegram invoice gift callback: intentionally disabled."""
    message = _callback_message(cb)
    if message is None:
        return

    log_event(int(cb.from_user.id), "legacy_payment_callback_blocked", {"stage": "gift_buy"})
    await message.answer(_LEGACY_GIFT_PAYMENT_DISABLED, reply_markup=kb_back("gift:menu"))


async def deliver_gift_message(message: Message, code: str) -> None:
    """Show sender platform choices for a paid gift; legacy direct Telegram target is best-effort."""
    user_id = _message_user_id(message)
    if user_id is None:
        return

    from_name = _message_user_full_name(message) or "друг"
    template = get_gift_template()
    txt = template.format(link='', from_name=from_name).strip()

    sent_ok = 0
    tgt = get_target(user_id)
    bot = message.bot
    if tgt and bot is not None:
        me = await bot.get_me()
        telegram_link = f"https://t.me/{me.username}?start=gift_{code}"
        direct_txt = template.format(link=telegram_link, from_name=from_name)
        try:
            await bot.send_message(int(tgt.to_id), direct_txt)
            sent_ok = 1
        except (TelegramAPIError, asyncio.TimeoutError):
            logger.info("gift delivery send_message failed", exc_info=True)
            sent_ok = 0

    if sent_ok:
        log_event(user_id, "gift_delivered_ok", {"code": code, "to_id": int(tgt.to_id) if tgt else None})
        await message.answer(
            "✅ Оплата прошла. Пакет практик оплачен и отправлен получателю в Telegram.\n\n"
            "Также можно отправить универсальную ссылку в другой мессенджер:",
            reply_markup=_gift_share_keyboard(code, txt),
        )
    else:
        log_event(user_id, "gift_delivery_platform_choice", {"code": code, "to_id": int(tgt.to_id) if tgt else None})
        await message.answer(
            "✅ Оплата прошла. Подарочный пакет практик готов.\n\n"
            "Куда отправить подарок? Выберите мессенджер — получатель откроет ссылку именно там:",
            reply_markup=_gift_share_keyboard(code, txt),
        )
        await message.answer(
            "Главное меню:",
            reply_markup=kb_main(user_id=user_id),
        )

    clear_target(user_id)
