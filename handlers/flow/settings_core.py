from __future__ import annotations
import logging



from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.exceptions import TelegramAPIError
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import CallbackQuery, Message

import asyncio
from core.time_utils import today_tz

from keyboards.inline import (
    kb_settings_menu,
    kb_back_main,
    kb_main,
    kb_ref_bonus_actions,
    kb_settings_locked,
    kb_messenger_platforms,
    kb_delivery_channel_slots,
    kb_delivery_channel_select,
)
from services.db import db
from services.subscription import has_access
from services.events import log_event
# (ленивый импорт графиков внутри хендлеров)
from services.bonuses import compute_bonus_stats, paid_referrals_count, gift_grants_count, gift_days_granted
from services.pending import consume_pending, set_pending, peek_pending
from services.messenger.links import build_messenger_targets
from services.messenger.platforms import platform_title
from services.messenger.preferences import get_channel_snapshot, set_preferred_platform
from services.delivery_preferences import (
    describe_delivery_preferences,
    set_user_timezone,
    set_quiet_hours,
    clear_quiet_hours,
    set_slot_channel,
    get_delivery_preferences,
    build_delivery_policy_decision,
)


from core.callback_utils import safe_answer_callback
router = Router()


async def _to_thread(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


def _callback_message(cb: CallbackQuery) -> Message | None:
    message = cb.message
    return message if isinstance(message, Message) else None


def _message_user_id(message: Message) -> int | None:
    user = message.from_user
    return int(user.id) if user else None


async def safe_edit(message: Message, text: str, reply_markup=None, parse_mode=None, **kwargs):
    """Безопасный edit_text.

    Telegram может вернуть `message is not modified`, если пользователь нажал кнопку повторно.
    По Установке F/UX: не падаем и не спамим.
    """
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        raise


@router.callback_query(F.data == "settings:menu")
async def settings_menu(cb: CallbackQuery):
    await safe_answer_callback(cb)
    message = _callback_message(cb)
    if message is None:
        return
    await safe_edit(
        message,
        "⚙️ Мои настройки Метротерапии\n\n"
        "Здесь Вы можете настроить погоду, время отправки трансов, часовой пояс, тихие часы и каналы по времени дня.",
        reply_markup=kb_settings_menu(),
    )




@router.callback_query(F.data == "settings:platform:menu")
async def settings_platform_menu(cb: CallbackQuery):
    await safe_answer_callback(cb)
    message = _callback_message(cb)
    if message is None:
        return
    uid = int(cb.from_user.id)
    snapshot, targets = await asyncio.gather(
        _to_thread(get_channel_snapshot, uid),
        _to_thread(build_messenger_targets, uid),
    )
    current = platform_title(snapshot.get('preferred_platform'))
    connected = []
    for item in snapshot.get('identities') or []:
        title = platform_title(item.get('platform'))
        if title not in connected:
            connected.append(title)
    connected_line = ', '.join(connected) if connected else 'пока только Telegram / входные ссылки'
    text = (
        "💬 Предпочтительный мессенджер\n\n"
        f"Сейчас приоритетный канал: {current}.\n"
        f"Подключённые каналы: {connected_line}.\n\n"
        "Выберите, куда проект должен вести пользователя в первую очередь и какой канал считать основным для доставки при наличии нескольких идентичностей."
    )
    await safe_edit(message, text, reply_markup=kb_messenger_platforms(snapshot, targets))


@router.callback_query(F.data.startswith("settings:platform:set:"))
async def settings_platform_set(cb: CallbackQuery):
    await safe_answer_callback(cb)
    message = _callback_message(cb)
    if message is None:
        return
    data = cb.data
    if data is None:
        return
    uid = int(cb.from_user.id)
    platform = data.rsplit(':', 1)[-1]
    await _to_thread(set_preferred_platform, uid, platform)
    snapshot, targets = await asyncio.gather(
        _to_thread(get_channel_snapshot, uid),
        _to_thread(build_messenger_targets, uid),
    )
    current = platform_title(snapshot.get('preferred_platform'))
    connected = []
    for item in snapshot.get('identities') or []:
        title = platform_title(item.get('platform'))
        if title not in connected:
            connected.append(title)
    connected_line = ", ".join(connected) if connected else "пока только Telegram / входные ссылки"
    text = (
        "💬 Предпочтительный мессенджер\n\n"
        f"Сейчас приоритетный канал: {current}.\n"
        f"Подключённые каналы: {connected_line}.\n\n"
        "Выберите, куда проект должен вести пользователя в первую очередь и какой канал считать основным для доставки при наличии нескольких идентичностей."
    )
    await safe_edit(message, text, reply_markup=kb_messenger_platforms(snapshot, targets))
    await _to_thread(log_event, uid, "settings_platform_set", {"platform": platform})

@router.callback_query(F.data == "settings:time:work")
async def settings_time_work(cb: CallbackQuery):
    await safe_answer_callback(cb)
    message = _callback_message(cb)
    if message is None:
        return
    uid = int(cb.from_user.id)
    # Полный доступ к настройке времени — только по подписке (scope: morning)
    if not await _to_thread(has_access, uid, "morning"):
        await message.answer(
            "🔐 Полный доступ доступен по подписке.\n\n"            "Нажмите «💳 Подписка / тарифы».",
            reply_markup=kb_settings_locked(),
        )
        return

    await _to_thread(set_pending, uid, "set_time", {"slot": "work"}, ttl_sec=600)
    await message.answer(
        "⏰ Время «Дорога на работу»\n\n"
        "Напишите желаемое время в формате HH:MM (например, 11:03).\n\n"
        "Я сохраню время — и утренний транс будет приходить ровно в него.",
        reply_markup=kb_back_main(),
    )
    await _to_thread(log_event, uid, "settings_time_prompt", {"slot": "work"})


@router.callback_query(F.data == "settings:time:home")
async def settings_time_home(cb: CallbackQuery):
    await safe_answer_callback(cb)
    message = _callback_message(cb)
    if message is None:
        return
    uid = int(cb.from_user.id)
    # Полный доступ к настройке времени — только по подписке (scope: evening)
    if not await _to_thread(has_access, uid, "evening"):
        await message.answer(
            "🔐 Полный доступ доступен по подписке.\n\n"            "Нажмите «💳 Подписка / тарифы».",
            reply_markup=kb_settings_locked(),
        )
        return

    await _to_thread(set_pending, uid, "set_time", {"slot": "home"}, ttl_sec=600)
    await message.answer(
        "⏰ Время «Дорога домой»\n\n"
        "Напишите желаемое время в формате HH:MM (например, 19:47).\n\n"
        "Я сохраню время — и вечерний транс будет приходить ровно в него.",
        reply_markup=kb_back_main(),
    )
    await _to_thread(log_event, uid, "settings_time_prompt", {"slot": "home"})


@router.callback_query(F.data == "settings:delivery:tz")
async def settings_delivery_tz(cb: CallbackQuery):
    await safe_answer_callback(cb)
    message = _callback_message(cb)
    if message is None:
        return
    uid = int(cb.from_user.id)
    await _to_thread(set_pending, uid, "set_timezone", ttl_sec=600)
    await message.answer("🌍 Укажите свой timezone, например Europe/Amsterdam.", reply_markup=kb_back_main())


@router.callback_query(F.data == "settings:delivery:quiet")
async def settings_delivery_quiet(cb: CallbackQuery):
    await safe_answer_callback(cb)
    message = _callback_message(cb)
    if message is None:
        return
    uid = int(cb.from_user.id)
    await _to_thread(set_pending, uid, "set_quiet_hours", ttl_sec=600)
    await message.answer("🌙 Укажите quiet hours в формате HH:MM-HH:MM, например 22:00-08:00, или off.", reply_markup=kb_back_main())




@router.callback_query(F.data == "settings:delivery:menu")
async def settings_delivery_menu(cb: CallbackQuery):
    await safe_answer_callback(cb)
    message = _callback_message(cb)
    if message is None:
        return
    uid = int(cb.from_user.id)
    await safe_edit(
        message,
        "🕒 Время и правила отправки\n\n"
        + await _to_thread(describe_delivery_preferences, uid)
        + "\n\nПримеры: timezone Europe/Amsterdam, quiet 22:00-08:00, channel morning max",
        reply_markup=kb_back_main(),
    )


@router.callback_query(F.data == "settings:delivery:channels")
async def settings_delivery_channels(cb: CallbackQuery):
    await safe_answer_callback(cb)
    message = _callback_message(cb)
    if message is None:
        return
    uid = int(cb.from_user.id)
    prefs, snapshot = await asyncio.gather(
        _to_thread(get_delivery_preferences, uid),
        _to_thread(get_channel_snapshot, uid),
    )
    payload = {**snapshot, 'morning_channel': prefs.morning_channel, 'evening_channel': prefs.evening_channel}
    await safe_edit(
        message,
        "📨 Каналы по времени дня\n\n" + await _to_thread(describe_delivery_preferences, uid) + "\n\nВыберите, куда сначала пытаться доставлять утренние и вечерние касания.",
        reply_markup=kb_delivery_channel_slots(payload),
    )


@router.callback_query(F.data.startswith("settings:delivery:slot:") & ~F.data.startswith("settings:delivery:slot:set:"))
async def settings_delivery_slot_menu(cb: CallbackQuery):
    await safe_answer_callback(cb)
    message = _callback_message(cb)
    if message is None:
        return
    data = cb.data
    if data is None:
        return
    uid = int(cb.from_user.id)
    slot = data.rsplit(':', 1)[-1]
    prefs, snapshot = await asyncio.gather(
        _to_thread(get_delivery_preferences, uid),
        _to_thread(get_channel_snapshot, uid),
    )
    payload = {**snapshot, 'morning_channel': prefs.morning_channel, 'evening_channel': prefs.evening_channel}
    await safe_edit(
        message,
        f"📨 Канал для {'утренних' if slot == 'morning' else 'вечерних'} отправок\n\n" + await _to_thread(describe_delivery_preferences, uid),
        reply_markup=kb_delivery_channel_select(slot, payload),
    )


@router.callback_query(F.data.startswith("settings:delivery:slot:set:"))
async def settings_delivery_slot_set(cb: CallbackQuery):
    await safe_answer_callback(cb)
    message = _callback_message(cb)
    if message is None:
        return
    data = cb.data
    if data is None:
        return
    parts = data.split(':')
    if len(parts) < 6:
        return
    uid = int(cb.from_user.id)
    slot = parts[4]
    platform = parts[5]
    await _to_thread(set_slot_channel, uid, slot, None if platform == 'auto' else platform)
    prefs, snapshot = await asyncio.gather(
        _to_thread(get_delivery_preferences, uid),
        _to_thread(get_channel_snapshot, uid),
    )
    payload = {**snapshot, 'morning_channel': prefs.morning_channel, 'evening_channel': prefs.evening_channel}
    decision = await _to_thread(build_delivery_policy_decision, uid, slot)
    note = ''
    if decision.fallback_used:
        note = f"\nСейчас фактическая доставка fallback-нется на {platform_title(decision.resolved_channel)}."
    await safe_edit(
        message,
        f"✅ Канал обновлён: {platform}.\n\n" + await _to_thread(describe_delivery_preferences, uid) + note,
        reply_markup=kb_delivery_channel_select(slot, payload),
    )


def _parse_hhmm(s: str) -> str | None:
    s = (s or "").strip()
    if ":" not in s:
        return None
    hh, mm = s.split(":", 1)
    if not (hh.isdigit() and mm.isdigit()):
        return None
    h, m = int(hh), int(mm)
    if h < 0 or h > 23 or m < 0 or m > 59:
        return None
    return f"{h:02d}:{m:02d}"


def _persist_user_time(uid: int, slot: str, hhmm: str) -> None:
    if slot == "work":
        update_sql = "UPDATE users SET work_time=? WHERE user_id=?"
    elif slot == "home":
        update_sql = "UPDATE users SET home_time=? WHERE user_id=?"
    else:
        raise ValueError(f"unsupported time slot: {slot}")
    with db() as conn:
        conn.execute(
            "INSERT INTO users(user_id, joined_at) VALUES(?, COALESCE((SELECT joined_at FROM users WHERE user_id=?), datetime('now'))) "
            "ON CONFLICT(user_id) DO NOTHING",
            (int(uid), int(uid)),
        )
        conn.execute(update_sql, (str(hhmm), int(uid)))


def _load_user_times(uid: int):
    with db() as conn:
        return conn.execute(
            "SELECT work_time, home_time FROM users WHERE user_id=?",
            (int(uid),),
        ).fetchone()


@router.message(F.text)
async def settings_time_input(message: Message):
    # Диагностика конфликтов ввода HH:MM
    try:
        from services.time_trace import mark as _mark_time
        _mark_time("handlers.settings:settings_time_input")
    except ImportError:
        # time_trace is optional; keep silent if absent
        pass
    except (TelegramAPIError, TelegramBadRequest, asyncio.TimeoutError):
        logging.getLogger(__name__).debug("time_trace mark failed", exc_info=True)
    except (ValueError, KeyError, AttributeError):
        logging.getLogger(__name__).debug("time_trace mark failed", exc_info=True)
    except OSError:
        logging.getLogger(__name__).debug("time_trace mark failed", exc_info=True)

    uid = _message_user_id(message)
    if uid is None:
        raise SkipHandler
    p = await _to_thread(peek_pending, uid)
    if not p or p.kind not in {"set_time", "set_timezone", "set_quiet_hours"}:
        raise SkipHandler
    if p.kind == "set_timezone":
        try:
            tz_name = await _to_thread(set_user_timezone, uid, (message.text or "").strip())
        except (ValueError, KeyError):
            return await message.answer("Пожалуйста, укажите корректный timezone, например Europe/Amsterdam.", reply_markup=kb_back_main())
        await _to_thread(consume_pending, uid, "set_timezone")
        await _to_thread(log_event, uid, "settings_timezone_set", {"timezone": tz_name})
        prefs_text = await _to_thread(describe_delivery_preferences, uid)
        return await message.answer(f"✅ Часовой пояс сохранён: {tz_name}.\n\n{prefs_text}", reply_markup=kb_back_main())

    if p.kind == "set_quiet_hours":
        raw = (message.text or "").strip().lower()
        if raw in {"off", "none", "disable", "выкл", "отключить"}:
            await _to_thread(clear_quiet_hours, uid)
            await _to_thread(consume_pending, uid, "set_quiet_hours")
            await _to_thread(log_event, uid, "settings_quiet_hours_cleared", {})
            prefs_text = await _to_thread(describe_delivery_preferences, uid)
            return await message.answer(f"✅ Тихие часы выключены.\n\n{prefs_text}", reply_markup=kb_back_main())
        if "-" not in raw:
            return await message.answer("Используйте формат HH:MM-HH:MM, например 22:00-08:00.", reply_markup=kb_back_main())
        start_hhmm, end_hhmm = [part.strip() for part in raw.split("-", 1)]
        try:
            start_hhmm, end_hhmm = await _to_thread(set_quiet_hours, uid, start_hhmm, end_hhmm)
        except (ValueError, KeyError):
            return await message.answer("Не смог распознать quiet hours. Пример: 22:00-08:00.", reply_markup=kb_back_main())
        await _to_thread(consume_pending, uid, "set_quiet_hours")
        await _to_thread(log_event, uid, "settings_quiet_hours_set", {"start": start_hhmm, "end": end_hhmm})
        prefs_text = await _to_thread(describe_delivery_preferences, uid)
        return await message.answer(f"✅ Тихие часы сохранены: {start_hhmm}-{end_hhmm}.\n\n{prefs_text}", reply_markup=kb_back_main())

    slot = str((p.data or {}).get("slot") or "")
    if slot not in {"work", "home"}:
        await _to_thread(consume_pending, uid, "set_time")
        return await message.answer("Не смог распознать, какое время нужно сохранить.", reply_markup=kb_back_main())
    hhmm = _parse_hhmm(message.text or "")
    if not hhmm:
        return await message.answer("Пожалуйста, время в формате HH:MM (например, 08:30).", reply_markup=kb_back_main())

    await _to_thread(_persist_user_time, uid, slot, hhmm)
    await _to_thread(consume_pending, uid, "set_time")

    await _to_thread(log_event, uid, "settings_time_set", {"slot": slot, "time": hhmm})
    # Быстрый UX: сразу предложить настроить второе время, чтобы не возвращаться в меню.
    try:
        row = await _to_thread(_load_user_times, uid)
        wt = (row[0] if row and not hasattr(row, "keys") else (row["work_time"] if row else None))
        ht = (row[1] if row and not hasattr(row, "keys") else (row["home_time"] if row else None))
    except (TelegramAPIError, TelegramBadRequest, asyncio.TimeoutError):
        logging.getLogger(__name__).exception("Unhandled exception")
        wt, ht = None, None
    except (ValueError, KeyError, AttributeError):
        logging.getLogger(__name__).exception("Unhandled exception")
        wt, ht = None, None
    except OSError:
        logging.getLogger(__name__).exception("Unhandled exception")
        wt, ht = None, None

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    if slot == "work" and not ht:
        rm = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏰ Настроить время: дорога домой", callback_data="settings:time:home")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main")],
        ])
        await message.answer(f"✅ Сохранил время «Дорога на работу»: {hhmm}\n\nТеперь задайте время для «Дорога домой».", reply_markup=rm)
        await _prompt_after_time_set(message, slot)
        return

    if slot == "home" and not wt:
        rm = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏰ Настроить время: дорога на работу", callback_data="settings:time:work")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main")],
        ])
        await message.answer(f"✅ Сохранил время «Дорога домой»: {hhmm}\n\nТеперь задайте время для «Дорога на работу».", reply_markup=rm)
        await _prompt_after_time_set(message, slot)
        return

    await message.answer(f"✅ Сохранил время: {hhmm}", reply_markup=kb_main(user_id=uid))
    await _prompt_after_time_set(message, slot)


@router.callback_query(F.data == "settings:ref")
async def settings_ref(cb: CallbackQuery):
    await safe_answer_callback(cb)
    message = _callback_message(cb)
    if message is None:
        return
    uid = int(cb.from_user.id)

    n_paid, n_gifts, gift_days, stats = await asyncio.gather(
        _to_thread(paid_referrals_count, uid),
        _to_thread(gift_grants_count, uid),
        _to_thread(gift_days_granted, uid),
        _to_thread(compute_bonus_stats, uid),
    )

    text = (
        "🎁 Мои бонусы за приглашения\n\n"
        f"По Вашему приглашению программу оплатили: {n_paid} человек(а).\n\n"
        f"За подарки Вы получили бонусов: {gift_days} дн. (подарков: {n_gifts}).\n\n"
        "Бонусы (в днях):\n"
        f"• начислено: {stats.earned_days} дн.\n"
        f"• израсходовано: {stats.used_days} дн.\n"
        f"• остаток: {stats.remaining_days} дн.\n\n"
        "Бонус начисляется только за тех, кто оплатил программу.\n"
        "Бонус за подарки начисляется сразу после оплаты подарка.\n"
        "Дни не обнуляются — всё сохраняется."
    )

    await safe_edit(message, text, reply_markup=kb_ref_bonus_actions())
    await _to_thread(log_event, uid, "settings_ref", {"paid_count": n_paid, "earned": stats.earned_days, "used": stats.used_days, "remaining": stats.remaining_days})


async def _prompt_after_time_set(message: Message, slot: str) -> None:
    """Сразу после сохранения времени показываем pre-оценку и отправляем транс по UX.
    Это помогает пользователю убедиться, что всё настроено, без ожидания следующего тика времени.
    """
    try:
        uid = _message_user_id(message)
        if uid is None:
            return
        # slot: work/home -> morning/evening
        slot_norm = "morning" if slot == "work" else "evening"
        kind = "work" if slot == "work" else "home"

        from services.subscription import has_access
        if not await _to_thread(has_access, uid, slot_norm):
            return

        from services.progress import get_index
        from services.audio_anchor import pick_for_slot
        from services.mood import create_session
        from services.db import mark_delivery_once
        from services.idempotency_keys import for_settings_prompt
        from keyboards.inline import kb_mood_scale
        from services.events import log_event

        idx = await _to_thread(get_index, uid, slot_norm)
        aa = await _to_thread(pick_for_slot, slot_norm, idx)
        if not aa:
            return

        # Idempotency (до любых side-effects): защита от повторных апдейтов/двойного клика
        # После смены времени Telegram может прислать update повторно; нам нельзя дублировать окно.
        day_iso = today_tz().isoformat()
        scheduled_at_key = for_settings_prompt(uid, day_iso, slot_norm)
        if not await _to_thread(mark_delivery_once, uid, kind, "pre_score", scheduled_at_key):
            return

        # Создаём сессию и сразу просим оценку "до", как в авто-рассылке
        sid = await _to_thread(
            create_session,
            user_id=uid,
            kind=kind,
            source="settings",
            day=day_iso,
            slot=slot_norm,
            anchor_id=aa.anchor,
        )
        await message.answer(
            "📍 Перед прослушиванием: оцените своё состояние сейчас (−10 … +10):\n\n"
            "Нажмите оценку — и я сразу пришлю Вам аудиотранс.",
            reply_markup=kb_mood_scale(int(sid), stage="pre"),
        )
        await _to_thread(log_event, uid, "settings_time_prompted", {"slot": slot_norm, "anchor": aa.anchor})
    except (TelegramAPIError, TelegramBadRequest, asyncio.TimeoutError):
        logging.getLogger(__name__).exception("Unhandled exception")
    except (ValueError, KeyError, AttributeError):
        logging.getLogger(__name__).exception("Unhandled exception")
    except OSError:
        logging.getLogger(__name__).exception("Unhandled exception")
