from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from core.callback_utils import safe_answer_callback
from keyboards.inline import kb_body_question, kb_menu_only
from services.messenger.progress_charts import build_vk_mood_progress_chart_path
from services.body import pick_body_question
from services.mood import get_user_session

router = Router()
log = logging.getLogger(__name__)


def _callback_message(cb: CallbackQuery) -> Message | None:
    message = cb.message
    return message if isinstance(message, Message) else None


@router.callback_query(F.data.regexp(r"^post:chart:\d+$"))
async def post_score_chart(cb: CallbackQuery) -> None:
    """Build and send the state-change chart shown after post-score.

    Telegram post-score UI has a direct button:
    `📈 Посмотреть график изменения моего состояния` -> `post:chart:<session_id>`.
    This handler keeps that button alive and uses the same canonical mood-session
    chart builder that VK/MAX use for their `progress_chart` reply.
    """
    try:
        await safe_answer_callback(cb, "Готовлю график…")
    except TelegramAPIError:
        log.debug("post chart callback answer failed", exc_info=True)
    except asyncio.TimeoutError:
        log.debug("post chart callback answer timed out", exc_info=True)

    message = _callback_message(cb)
    if message is None:
        return

    raw = cb.data or ""
    try:
        session_id = int(raw.rsplit(":", 1)[1])
    except (TypeError, ValueError, IndexError):
        return

    user_id = int(cb.from_user.id)
    session = await asyncio.to_thread(get_user_session, session_id, user_id)
    if session is None:
        log.warning(
            "Rejected post chart for a foreign or missing session",
            extra={"user_id": user_id, "session_id": session_id},
        )
        await message.answer("⚠️ Не нашёл сессию для построения графика.", reply_markup=kb_menu_only())
        return

    chart_path = await asyncio.to_thread(build_vk_mood_progress_chart_path, user_id)
    if chart_path is None:
        await message.answer(
            "📈 Пока недостаточно данных для графика. Пройдите цикл: шкала ДО → аудио → Прослушал → шкала ПОСЛЕ.",
            reply_markup=kb_menu_only(),
        )
        return

    try:
        data = await asyncio.to_thread(chart_path.read_bytes)
        await message.answer_photo(
            BufferedInputFile(data, filename="metrotherapy_state_change.png"),
            caption="📈 График изменения состояния после практики",
            reply_markup=kb_menu_only(),
        )
        log.info("Telegram post-score chart sent: user_id=%s session_id=%s path=%s", user_id, session_id, chart_path)

        question = await asyncio.to_thread(pick_body_question)
        text, markup = kb_body_question(
            session_id,
            question.key,
            "В каком месте тела заметнее напряжение?",
            question.options,
        )
        await message.answer(text, reply_markup=markup)
    except TelegramAPIError:
        log.exception("Telegram post-score chart send failed")
        await message.answer("⚠️ Не удалось построить или отправить график. Попробуйте позже.", reply_markup=kb_menu_only())
    except OSError:
        log.exception("Telegram post-score chart file read failed")
        await message.answer("⚠️ Не удалось построить или отправить график. Попробуйте позже.", reply_markup=kb_menu_only())
    except RuntimeError:
        log.exception("Telegram post-score chart runtime failed")
        await message.answer("⚠️ Не удалось построить или отправить график. Попробуйте позже.", reply_markup=kb_menu_only())
    except ValueError:
        log.exception("Telegram post-score chart value failed")
        await message.answer("⚠️ Не удалось построить или отправить график. Попробуйте позже.", reply_markup=kb_menu_only())
