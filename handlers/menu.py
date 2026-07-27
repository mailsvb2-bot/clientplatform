from __future__ import annotations

import asyncio
import copy
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from config.settings import settings
from core.callback_utils import safe_answer_callback
from keyboards.inline import kb_back_main, kb_demo_kind, kb_main
from services.db import db
from services.events import log_event
from services.jobs import add_job, cancel_jobs
from services.personalization import get_preface, set_funnel_stage
from services.admin import is_staff

router = Router()


def _callback_message(cb: CallbackQuery) -> Message | None:
    message = cb.message
    return message if isinstance(message, Message) else None


def _callback_user_id(cb: CallbackQuery) -> int:
    user = cb.from_user
    return int(user.id)


def _message_user_id(message: Message) -> int | None:
    user = message.from_user
    return int(user.id) if user is not None else None


def _log_funnel_safe(user_id: int, event: str, payload: dict | None = None) -> None:
    try:
        log_event(int(user_id), event, payload or {})
    except (sqlite3.Error, RuntimeError, OSError, TypeError, ValueError):
        logging.getLogger(__name__).debug("funnel event skipped", exc_info=True)


def _tz() -> ZoneInfo:
    return ZoneInfo(getattr(settings, "TIMEZONE", "Europe/Moscow"))


def _parse_hhmm(s: str) -> tuple[int, int] | None:
    s = (s or "").strip()
    if ":" not in s:
        return None
    hh, mm = s.split(":", 1)
    if not (hh.isdigit() and mm.isdigit()):
        return None
    h, m = int(hh), int(mm)
    if h < 0 or h > 23 or m < 0 or m > 59:
        return None
    return h, m


def _load_work_time_row(user_id: int) -> Any:
    with db() as conn:
        return conn.execute("SELECT work_time FROM users WHERE user_id=?", (int(user_id),)).fetchone()


async def safe_edit(message: Message, text: str, reply_markup=None, parse_mode=None):
    """Edit text when possible and fall back to a new message for media posts."""
    try:
        if not (getattr(message, "text", None) or getattr(message, "caption", None)):
            await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
            return

        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        if "no text in the message to edit" in str(exc):
            await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
            return
        raise


def _is_admin(uid: int) -> bool:
    """Backward-compatible immutable superadmin helper."""
    try:
        return int(uid) in set(settings.admin_id_list)
    except (TypeError, ValueError):
        logging.getLogger(__name__).exception("Unhandled exception")
        return False


def _main_menu_keyboard(user_id: int):
    """Return an isolated menu and expose the panel to every delegated staff role."""

    markup = copy.deepcopy(kb_main(user_id=int(user_id)))
    if not hasattr(markup, "inline_keyboard"):
        return markup
    has_panel = any(
        getattr(button, "callback_data", None) == "admin:menu"
        for row in markup.inline_keyboard
        for button in row
    )
    if not has_panel and is_staff(int(user_id)):
        markup.inline_keyboard.append(
            [InlineKeyboardButton(text="🛠 Панель", callback_data="admin:menu")]
        )
    return markup


async def send_main_menu(target: CallbackQuery | Message):
    if isinstance(target, CallbackQuery):
        user_id = _callback_user_id(target)
    else:
        message_user_id = _message_user_id(target)
        if message_user_id is None:
            return
        user_id = message_user_id

    await asyncio.to_thread(
        _log_funnel_safe,
        user_id,
        "funnel_main_menu_opened",
        {"source": type(target).__name__},
    )
    preface = await asyncio.to_thread(get_preface, user_id, "menu")
    text = (
        f"{preface}"
        "Главное меню\n\n"
        "Выберите маршрут: можно начать с бесплатной практики, открыть полный доступ или посмотреть свой прогресс."
    )

    if isinstance(target, CallbackQuery):
        try:
            await target.answer()
        except (TelegramAPIError, TelegramBadRequest):
            logging.getLogger(__name__).debug("Callback answer failed", exc_info=True)

        message = _callback_message(target)
        if message is None:
            return

        await safe_edit(
            message,
            text,
            reply_markup=_main_menu_keyboard(user_id),
            parse_mode=None,
        )
    else:
        await target.answer(text, reply_markup=_main_menu_keyboard(user_id))


@router.callback_query(lambda c: c.data == "menu_main")
async def cb_menu_main(cb: CallbackQuery, state: FSMContext | None = None):
    await safe_answer_callback(cb)
    try:
        if state is not None:
            await state.clear()
    except (TypeError, ValueError, RuntimeError):
        logging.getLogger(__name__).debug("Failed to clear FSM state", exc_info=True)
    await send_main_menu(cb)


@router.callback_query(lambda c: c.data == "menu:main")
async def cb_menu_main_v2(cb: CallbackQuery, state: FSMContext | None = None):
    await safe_answer_callback(cb)
    try:
        if state is not None:
            await state.clear()
    except (TypeError, ValueError, RuntimeError):
        logging.getLogger(__name__).debug("Failed to clear FSM state", exc_info=True)
    await send_main_menu(cb)


@router.callback_query(lambda c: c.data in ("demo_menu", "demo", "demo:menu"))
async def cb_demo_menu(cb: CallbackQuery):
    await safe_answer_callback(cb)
    message = _callback_message(cb)
    if message is None:
        return

    user_id = _callback_user_id(cb)
    await asyncio.to_thread(
        _log_funnel_safe,
        user_id,
        "funnel_demo_clicked",
        {"source": "main_menu"},
    )
    await asyncio.to_thread(set_funnel_stage, user_id, "d0")
    preface = await asyncio.to_thread(get_preface, user_id, "demo")
    text = (
        f"{preface}"
        "🌿 Бесплатная практика\n\n"
        "Выберите короткий маршрут. Бот пришлёт аудиопрактику и поможет зафиксировать состояние до/после, "
        "чтобы Вы увидели личный эффект, а не просто послушали файл.\n\n"
        "🚗 Утро / дорога — мягко включиться в день.\n"
        "🌙 Вечер / домой — снять напряжение и завершить день спокойнее.\n\n"
        "После выбора я попрошу указать удобное время отправки."
    )
    await safe_edit(message, text, reply_markup=kb_demo_kind(), parse_mode=None)


@router.callback_query(lambda c: c.data in ("back_main", "back"))
async def cb_back_main(cb: CallbackQuery, state: FSMContext | None = None):
    await safe_answer_callback(cb)
    try:
        if state is not None:
            await state.clear()
    except (TypeError, ValueError, RuntimeError):
        logging.getLogger(__name__).debug("Failed to clear FSM state", exc_info=True)
    await send_main_menu(cb)


@router.callback_query(lambda c: c.data == "remind:continue_tomorrow")
async def cb_remind_continue_tomorrow(cb: CallbackQuery):
    """Schedule one deterministic next-morning reminder for a free user."""
    await safe_answer_callback(cb)
    message = _callback_message(cb)
    if message is None:
        return

    user_id = _callback_user_id(cb)
    tz = _tz()

    row = await asyncio.to_thread(_load_work_time_row, user_id)
    hhmm = (row["work_time"] if row and row["work_time"] else "") or getattr(settings, "MORNING_TIME", "08:30")
    parsed = _parse_hhmm(str(hhmm)) or _parse_hhmm(getattr(settings, "MORNING_TIME", "08:30"))
    h, m = parsed if parsed else (8, 30)

    now_local = datetime.now(tz)
    run_local = (now_local + timedelta(days=1)).replace(hour=h, minute=m, second=0, microsecond=0)
    run_utc = run_local.astimezone(ZoneInfo("UTC")).replace(microsecond=0).isoformat()

    await asyncio.to_thread(cancel_jobs, user_id, prefix="remind_")
    await asyncio.to_thread(
        add_job,
        user_id,
        "remind_continue",
        run_utc,
        {"src": "full_access", "hhmm": f"{h:02d}:{m:02d}"},
    )

    tz_name = getattr(settings, "TIMEZONE", "Europe/Moscow")
    await message.answer(
        f"✅ Хорошо. Я напомню Вам завтра в {run_local.strftime('%H:%M')} ({tz_name}).",
        reply_markup=kb_back_main(),
    )
