from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from core.callback_utils import safe_answer_callback

KeyboardButtonRequestUser: Any
try:
    from aiogram.types import KeyboardButtonRequestUser as _KeyboardButtonRequestUser
    KeyboardButtonRequestUser = _KeyboardButtonRequestUser
except (ImportError, AttributeError):  # pragma: no cover
    KeyboardButtonRequestUser = None

from keyboards.inline import kb_main
from services.bg import tm
from services.events import log_event
from services.messenger.links import build_share_targets
from services.pending import consume_pending, peek_pending, pop_pending, set_pending
from services.promo_texts import get_share_template

router = Router()


def _callback_message(cb: CallbackQuery) -> Message | None:
    msg = cb.message
    return msg if isinstance(msg, Message) else None


def _callback_identity(cb: CallbackQuery) -> tuple[int, str, str] | None:
    user = cb.from_user
    if user is None:
        return None
    uid = int(user.id)
    from_name = (user.full_name or "").strip() or "друг"
    share_text = get_share_template().format(link="", from_name=from_name).strip()
    return uid, from_name, share_text


def _message_user_id(message: Message) -> int | None:
    user = message.from_user
    if user is None:
        return None
    return int(user.id)


def _share_kb(referrer_user_id: int, text: str, back_cb: str = "menu:main") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in build_share_targets(referrer_user_id, text=text):
        rows.append([InlineKeyboardButton(text=f"📨 Поделиться в {item['title']}", url=item["url"])])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _pick_user_keyboard() -> ReplyKeyboardMarkup:
    rows = []
    if KeyboardButtonRequestUser is not None:
        try:
            rows.append([KeyboardButton(text="👤 Выбрать друга", request_user=KeyboardButtonRequestUser(request_id=1))])
        except TypeError:
            return ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="❌ Отмена")]],
                resize_keyboard=True,
                one_time_keyboard=True,
            )
    rows.append([KeyboardButton(text="❌ Отмена")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, one_time_keyboard=True)


@router.callback_query(F.data == "share:menu")
async def share_menu(cb: CallbackQuery) -> None:
    await safe_answer_callback(cb)

    msg = _callback_message(cb)
    identity = _callback_identity(cb)
    if msg is None or identity is None:
        return

    uid, _from_name, share_text = identity
    # Platform share uses inline URL buttons and must not open a stale user-pick mode.
    log_event(uid, "share_menu", {"mode": "platform_choice"})
    await msg.answer(
        "📣 Куда хотите посоветовать «Метротерапию»?\n\n"
        "Выберите мессенджер — ссылка откроется именно там. Если канал не отображается, проверьте настройки TELEGRAM_BOT_USERNAME, MAX_BOT_LINK_BASE/MAX_BOT_NAME и VK_GROUP_ID.",
        reply_markup=_share_kb(uid, share_text),
    )


@router.callback_query(F.data == "share:pick")
async def share_pick(cb: CallbackQuery) -> None:
    # Backward-compatible Telegram-only direct delivery. Kept for old callback contracts,
    # but the main UX now routes through share:menu platform choice.
    await safe_answer_callback(cb)

    msg = _callback_message(cb)
    identity = _callback_identity(cb)
    if msg is None or identity is None:
        return

    uid, from_name, _share_text = identity
    bot_username = str(getattr(cb.bot, "username", "") or "").strip()
    link = f"https://t.me/{bot_username}?start=ref_{uid}" if bot_username else ""
    share_text = get_share_template().format(link=link, from_name=from_name)
    set_pending(uid, "share", {"link": link, "text": share_text, "from_name": from_name})
    log_event(uid, "share_pick", {"mode": "telegram_user_picker_legacy"})
    await msg.answer(
        "Выберите друга в Telegram, чтобы отправить ему рекомендацию.",
        reply_markup=_pick_user_keyboard(),
    )


async def _deliver_share_messages(bot, uid: int, picked_ids: list[int], final: str) -> int:
    ok = 0
    for to_id in picked_ids[:1]:
        try:
            await asyncio.wait_for(bot.send_message(int(to_id), final), timeout=2.5)
            ok += 1
        except asyncio.TimeoutError:
            logging.getLogger(__name__).warning("share: send timeout", extra={"from_id": uid, "to_id": int(to_id)})
        except (TelegramForbiddenError, TelegramNotFound):
            logging.getLogger(__name__).debug("share: cannot deliver", extra={"from_id": uid, "to_id": int(to_id)})
        except (TelegramRetryAfter, TelegramNetworkError) as e:
            logging.getLogger(__name__).warning(
                "share: temporary telegram/network error",
                extra={"from_id": uid, "to_id": int(to_id), "err": str(e)},
            )
        except TelegramAPIError:
            logging.getLogger(__name__).exception("share: telegram api error", extra={"from_id": uid, "to_id": int(to_id)})
    return ok


async def _finalize_share_delivery(message: Message, uid: int, picked_ids: list[int], final: str, link: str) -> None:
    ok = await _deliver_share_messages(message.bot, uid, picked_ids, final)
    if ok:
        await message.answer("✅ Рекомендация отправлена.")
        log_event(uid, "share_sent_ok", {"picked": len(picked_ids), "sent": ok})
        return

    await message.answer(
        "⚠️ Telegram не дал отправить сообщение выбранному пользователю автоматически.\n"
        "Скорее всего, этот человек ещё не запускал бота.\n\n"
        "Отправьте ссылку вручную:\n" + (link or "(ссылка недоступна — проверьте username бота)"),
    )
    log_event(uid, "share_sent_fail", {"picked": len(picked_ids), "sent": ok})


@router.message(F.users_shared)
async def users_shared(message: Message) -> None:
    uid = _message_user_id(message)
    if uid is None:
        return

    peek = peek_pending(uid)
    if not peek or peek.kind != "share":
        return
    p = pop_pending(uid)
    if not p:
        return

    try:
        shared = message.users_shared
        picked_ids = [int(u.user_id) for u in ((shared.users if shared is not None else []) or [])]
    except (AttributeError, TypeError, ValueError):
        picked_ids = []

    data = p.data or {}
    link = str(data.get("link") or "")
    from_name = str(data.get("from_name") or (message.from_user.full_name if message.from_user else "") or "друг").strip() or "друг"
    txt = str(data.get("text") or "")
    final = txt if "{link}" not in txt else txt.format(link=link, from_name=from_name)

    if not picked_ids:
        await message.answer(
            "Не удалось получить выбранного пользователя. Отправьте ссылку вручную:\n"
            + (link or "(ссылка недоступна — проверьте username бота)"),
            reply_markup=ReplyKeyboardRemove(),
        )
        await message.answer("Главное меню:", reply_markup=kb_main(user_id=uid))
        log_event(uid, "share_pick_empty", {"picked": 0})
        return

    tm().create(_finalize_share_delivery(message, uid, picked_ids[:1], final, link), name="share-delivery")
    await message.answer("✅ Друг выбран. Возвращаю в меню.", reply_markup=ReplyKeyboardRemove())
    await message.answer("Главное меню:", reply_markup=kb_main(user_id=uid))


@router.message(F.text == "❌ Отмена")
async def cancel(message: Message) -> None:
    uid = _message_user_id(message)
    if uid is None:
        return

    if consume_pending(uid, "share") is None:
        return
    await message.answer("Ок.", reply_markup=ReplyKeyboardRemove())
    await message.answer(
        "Главное меню:",
        reply_markup=kb_main(user_id=uid),
    )
