from __future__ import annotations
import logging


from aiogram import Router, F
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext

import asyncio

from keyboards.inline import kb_back_main, kb_main, kb_weather
from services.weather import get_weather_text_async, set_location, set_city
from services.pending import consume_pending, set_pending, peek_pending
from services.events import log_event
from config.settings import settings


router = Router()


def _callback_message(cb: CallbackQuery) -> Message | None:
    message = cb.message
    return message if isinstance(message, Message) else None


def _message_user_id(message: Message) -> int | None:
    user = message.from_user
    return int(user.id) if user is not None else None


async def _to_thread(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


@router.callback_query(F.data == "weather:show")
async def weather_show(cb: CallbackQuery):
    message = _callback_message(cb)
    if message is None:
        return

    user_id = int(cb.from_user.id)
    # The message explicitly invites a location share; scope the next location
    # update to this flow instead of consuming arbitrary Telegram locations.
    await _to_thread(set_pending, user_id, "weather_place", {}, ttl_sec=600)
    txt = await get_weather_text_async(user_id)
    await message.edit_text(
        txt + "\n\nВы можете отправить геолокацию или указать город вручную.",
        reply_markup=kb_weather(),
    )


@router.callback_query(F.data == "weather:city")
async def weather_city(cb: CallbackQuery, state: FSMContext):
    message = _callback_message(cb)
    if message is None:
        return

    # Если пользователь был в другом сценарии со state (например, ввод времени),
    # сбрасываем state, чтобы ввод города не перехватился чужим обработчиком.
    try:
        await state.clear()
    except (TelegramAPIError, TelegramBadRequest):
        logging.getLogger(__name__).debug("Callback answer failed", exc_info=True)
    await _to_thread(set_pending, int(cb.from_user.id), "weather_city", {})
    await message.answer(
        "🏙 Пожалуйста, напишите название города (например: «Казань»).\n\n"
        "Город можно будет изменить в любой момент.",
        reply_markup=kb_back_main(),
    )


@router.message(F.location)
async def weather_location(message: Message):
    uid = _message_user_id(message)
    if uid is None:
        return

    pending = await _to_thread(peek_pending, uid)
    if not pending or pending.kind not in {"weather_place", "weather_city"}:
        raise SkipHandler

    loc = message.location
    if not loc:
        return
    ok, info = await _to_thread(set_location, uid, float(loc.latitude), float(loc.longitude))
    if not ok:
        await message.answer("❌ " + str(info), reply_markup=kb_back_main())
        return
    await _to_thread(consume_pending, uid, {"weather_place", "weather_city"})
    await message.answer(
        "✅ Спасибо! Я сохранил Вашу локацию. Теперь погода будет точнее.\n\n"
        + (await get_weather_text_async(uid)),
        reply_markup=kb_main(user_id=uid),
    )


@router.message(F.text)
async def weather_city_input(message: Message):
    """Приём текста города после команды/кнопки погоды.

    Без FSM: используем pending-хранилище (детерминированно и компактно).
    """
    # Диагностика конфликтов ввода HH:MM
    try:
        from services.time_trace import mark as _mark_time
        _mark_time("handlers.weather:weather_city_input")
    except (ImportError, AttributeError):
        logging.getLogger(__name__).debug("time_trace unavailable", exc_info=True)

    uid = _message_user_id(message)
    if uid is None:
        return

    p = await _to_thread(peek_pending, uid)
    if not p or p.kind not in {"weather_place", "weather_city"}:
        raise SkipHandler

    city_raw = (message.text or "").strip()
    if not city_raw:
        return await message.answer("Пожалуйста, напишите название города текстом.")

    ok, info = await asyncio.to_thread(set_city, uid, city_raw)
    if not ok:
        return await message.answer("❌ " + str(info), reply_markup=kb_back_main())

    await _to_thread(consume_pending, uid, {"weather_place", "weather_city"})
    await _to_thread(log_event, uid, "weather_city_set", {"city": str(info)})
    txt = await get_weather_text_async(uid, timeout_sec=1.5)
    await message.answer(
        f"✅ Город принят: {info}.\n\n{txt}\n\n"
        "Если Вы захотите изменить город — нажмите «Погода» и выберите «Изменить город».",
        reply_markup=kb_main(user_id=uid),
    )
