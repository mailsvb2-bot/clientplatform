from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time



import json
from collections.abc import Awaitable, Callable
from typing import Literal
from dataclasses import dataclass
from datetime import datetime, timedelta
from core.ai.decision_core import DecisionCore
from core.runtime.sovereignty.enforcement import require_token, set_current_token
from core.time_utils import utc_now, utc_now_iso, today_tz

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramAPIError
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config.settings import settings

from services.db import get_db, mark_delivery_once, was_delivered
from services.events import log_event
from services.catalog import AudioCatalog
from services.audio_guard import pick_demo_file
from services.subscription import is_active as is_sub_active
from services.demo_analytics import demo_sent_kinds
from services.demo_policy import can_repeat_demo_for_user
from services.jobs import claim_due_jobs, lock_job, mark_done, reschedule
from services.funnel_texts import funnel_text, funnel_text_ab
from services.events import has_event_since
from services.state_log import recent_hour_local, first_hour_today_local
from services.engine_state import acquire_lock, release_lock
from services.idempotency_keys import for_session, for_job_run_at

# ЭТАП 2: самооценка до/после + графики
from services.mood import create_session
from keyboards.inline import kb_mood_scale

# Funnel 2.0 (сценарии)
from services.funnel2 import (
    SC_DEMO_NOPAY_24H,
    SC_EXPIRED_RETURN_3D,
    eligible_demo_nopay_24h,
    eligible_expired_return_3d,
    mark_sent,
    already_sent,
    log_skip,
)
def _parse_dt(dt_iso: str) -> datetime:
    # run_at_utc хранится в ISO с timezone
    return datetime.fromisoformat(dt_iso)


@dataclass
class Job:
    id: int
    user_id: int
    job_type: str
    run_at_utc: str
    payload: str


JobHandler = Callable[[Bot, int, dict], Awaitable[None]]

FunnelKind = Literal["work", "home", "both"]
FunnelStep = Literal["nudge", "postdemo", "offer", "offer_nextday", "deadline", "lastcall"]
FunnelVariant = Literal["A", "B"]


def _funnel_kind(raw: object, *, default: FunnelKind = "both") -> FunnelKind:
    value = str(raw or default).strip()
    if value == "work":
        return "work"
    if value == "home":
        return "home"
    if value == "both":
        return "both"
    return default



def _is_sub_active_sync(user_id: int) -> bool:
    return bool(is_sub_active(int(user_id)))


def _demo_send_state_sync(user_id: int) -> tuple[set[str], bool]:
    return set(demo_sent_kinds(int(user_id))), bool(can_repeat_demo_for_user(int(user_id)))


def _create_demo_session_sync(user_id: int, *, kind: str, day: str) -> int:
    return int(
        create_session(
            int(user_id),
            kind=("work" if kind == "work" else "home"),
            source="demo",
            day=day,
            slot="demo",
            anchor_id=None,
        )
    )


def _demo_ack_exists_sync(*, user_id: int, kind: str, message_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT ack_at_utc FROM demo_events WHERE user_id=? AND kind=? AND message_id=?",
            (int(user_id), str(kind), int(message_id)),
        ).fetchone()
    return bool(row and row["ack_at_utc"])


def _hour_context_sync(user_id: int) -> int | None:
    return first_hour_today_local(int(user_id), settings.TIMEZONE) or recent_hour_local(int(user_id), settings.TIMEZONE)


def _has_viewed_tariffs_since_sync(*, user_id: int, ack_at: str) -> bool:
    return bool(has_event_since(int(user_id), "view_tariffs", str(ack_at)))


def _paid_setup_already_configured_sync(user_id: int) -> bool:
    if not is_sub_active(int(user_id)):
        return False
    with get_db() as conn:
        row = conn.execute(
            "SELECT work_time, home_time FROM users WHERE user_id=?",
            (int(user_id),),
        ).fetchone()
    return bool(row and ((row["work_time"] if "work_time" in row.keys() else None) or (row["home_time"] if "home_time" in row.keys() else None)))


def _post_prompt_idem_kind_sync(session_id: str) -> str:
    try:
        from services.mood import get_session

        s = get_session(int(session_id))
        return str(getattr(s, "kind", "") or "") if s else ""
    except (ImportError, sqlite3.Error, ValueError, AttributeError, KeyError, TypeError):  # validator: allow-wide-except
        return ""


def _funnel2_demo_nopay_guard_sync(user_id: int, payload: dict) -> str:
    if already_sent(int(user_id), SC_DEMO_NOPAY_24H):
        return "already_sent"
    if not eligible_demo_nopay_24h(int(user_id)):
        log_skip(int(user_id), SC_DEMO_NOPAY_24H, "not_eligible")
        return "not_eligible"
    if not mark_sent(int(user_id), SC_DEMO_NOPAY_24H, {"kind": payload.get("kind"), "ack_at_utc": payload.get("ack_at_utc")}):
        return "duplicate"
    return "send"


def _funnel2_expired_return_guard_sync(user_id: int, payload: dict) -> str:
    if already_sent(int(user_id), SC_EXPIRED_RETURN_3D):
        return "already_sent"
    if not eligible_expired_return_3d(int(user_id)):
        log_skip(int(user_id), SC_EXPIRED_RETURN_3D, "not_eligible")
        return "not_eligible"
    if not mark_sent(int(user_id), SC_EXPIRED_RETURN_3D, {"expires_at": payload.get("expires_at")}):
        return "duplicate"
    return "send"


class Engine:
    # v16.8: protect event loop latency.
    # - do not run tick concurrently
    # - avoid hammering sqlite lock every second when there is no work
    _tick_lock: asyncio.Lock = asyncio.Lock()
    _last_tick_monotonic: float = 0.0

    def _kb_after_demo(
        self,
        kind: str,
        *,
        allow_other: bool = True,
        message_id: int | None = None,
    ) -> InlineKeyboardMarkup:
        """Клавиатура под демо-аудио.

        Требование UX:
        - после отправки демо показать удобные действия
        - предложить второй демо-транс (в зависимости от первого выбора)
        - дать явную кнопку возврата в главное меню
        """
        kind = (kind or "work").strip()
        other = "home" if kind == "work" else "work"
        other_label = "🌙 «Дорога домой»" if other == "home" else "🚗 «Дорога на работу»"
        rows = []
        if message_id is not None:
            rows.append([InlineKeyboardButton(text="✅ Прослушал(а)", callback_data=f"demo:ack:{kind}:{int(message_id)}")])
        if allow_other:
            rows.append([InlineKeyboardButton(text=f"🎧 Послушать ресурсный транс {other_label}", callback_data=f"demo:other:{other}" )])
        rows.append([InlineKeyboardButton(text="💳 Пакеты практик", callback_data="sub:menu")])
        rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def _kb_funnel(self, user_id: int) -> InlineKeyboardMarkup:
        # Пробный доступ отключён по ТЗ. Оставляем детерминированные кнопки.
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Пакеты практик", callback_data="sub:menu")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main")],
        ])

    async def _should_skip_sales(self, user_id: int) -> bool:
        """Если у пользователя уже есть доступ (подписка) — продающие сообщения не отправляем."""
        return await asyncio.to_thread(_is_sub_active_sync, int(user_id))

    def _job_handlers(self) -> dict[str, JobHandler]:
        """Canonical job registry.

        Engine.tick owns scheduling/idempotency/retry mechanics only. Concrete
        business effects are resolved through this registry instead of a growing
        if/elif chain, so new jobs have one explicit contract surface.
        """
        return {
            "demo_reminder": self._demo_reminder,
            "demo_send": self._demo_send,
            "funnel_offer": self._funnel_offer,
            "after_paid_setup_ping": self._after_paid_setup_ping,
            "funnel_nudge": self._funnel_nudge,
            "funnel_postdemo": self._funnel_postdemo,
            "funnel_deadline": self._funnel_deadline,
            "funnel_lastcall": self._funnel_lastcall,
            "sub_expiring_soon": self._sub_expiring_soon,
            "funnel2_demo_nopay_24h": self._funnel2_demo_nopay_24h,
            "funnel2_expired_return_3d": self._funnel2_expired_return_3d,
            "remind_continue": self._remind_continue,
            "post_prompt": self._post_prompt,
            "clientplatform_booking_reminder": self._clientplatform_booking_reminder,
        }

    async def _execute_job(self, bot: Bot, job: Job, payload: dict) -> None:
        """Authorize and execute one claimed job through DecisionCore.

        This keeps the flow explicit:
        scheduler claim -> idempotency -> lock -> DecisionCore policy token -> effect.
        Unknown job types are logged and treated as no-op effects, so old bad rows
        do not poison the scheduler forever.
        """
        handler = self._job_handlers().get(job.job_type)
        if handler is None:
            await asyncio.to_thread(log_event, job.user_id, "job_unknown", {"job_type": job.job_type})
            return

        world_state = {
            "intent": "engine_job_execute",
            "source": "engine",
            "user_id": int(job.user_id),
            "job_type": str(job.job_type),
            "job_id": int(job.id),
            "run_at_utc": str(job.run_at_utc),
            "payload_keys": sorted(str(k) for k in payload.keys()),
            "token_ttl_sec": 60,
        }
        decision = await asyncio.to_thread(DecisionCore.instance().decide, world_state)
        require_token(decision.token)
        if decision.payload.get("type") != "job_execution_allowed":
            await asyncio.to_thread(
                log_event,
                job.user_id,
                "job_policy_denied",
                {
                    "job_type": job.job_type,
                    "reason": decision.payload.get("reason"),
                    "decision_id": decision.decision_id,
                },
            )
            return

        set_current_token(decision.token)
        try:
            await handler(bot, job.user_id, payload)
        finally:
            set_current_token(None)

    async def tick(self, bot: Bot):
        # Throttle: avoids DB lock contention that causes "SLOW update" spikes.
        min_interval = float(os.getenv("ENGINE_TICK_MIN_INTERVAL", "1.0") or "1.0")
        now_m = time.monotonic()
        if (now_m - self._last_tick_monotonic) < min_interval:
            return

        # Never run tick concurrently in the same process.
        if self._tick_lock.locked():
            return

        async with self._tick_lock:
            self._last_tick_monotonic = time.monotonic()

            # v7.0: идемпотентность тикера. Даже если scheduler случайно
            # запустится дважды, второй инстанс не войдёт в tick одновременно.
            if not await acquire_lock("engine_tick", ttl_sec=45):
                return

            try:
                # Техническая метка: внутренний timebase движка хранится в UTC.
                # ВАЖНО: календарные дни/"сегодня" считаются только через time_utils (локальный TZ),
                # но здесь мы работаем с run_at_utc.
                now_iso = utc_now_iso()

                # Единый планировщик: jobs + engine.tick.
                # Берём due-jobs с lock_token, чтобы другой воркер физически не мог выполнить те же строки.
                # SQLite драйвер синхронный — DB-операции планировщика выносим в thread,
                # чтобы tick() не блокировал event loop и не замедлял обработку апдейтов.
                claimed = await asyncio.to_thread(claim_due_jobs, now_iso, limit=25)
                jobs = [
                    Job(
                        id=int(j.id),
                        user_id=int(j.user_id),
                        job_type=str(j.job_type),
                        run_at_utc=str(j.run_at_utc),
                        payload=str(j.payload or "{}"),
                    )
                    for j in claimed
                ]

                if not jobs:
                    return

                # выполняем по одному, чтобы ошибки не "роняли" пачку
                for job, cj in zip(jobs, claimed):
                    # Жёсткий порядок: idempotency -> execution.
                    # Если ключ уже был выполнен — job закрываем.
                    if not cj.job_key:
                        cj.job_key = f"legacy:{job.id}"

                    if not await asyncio.to_thread(
                        mark_delivery_once,
                        int(job.user_id),
                        "job",
                        str(job.job_type),
                        str(cj.job_key),
                    ):
                        await asyncio.to_thread(mark_done, job.id, cj.lock_token)
                        continue

                    if not await asyncio.to_thread(lock_job, job.id, cj.lock_token):
                        continue
                    try:
                        payload = json.loads(job.payload or "{}")
                    except json.JSONDecodeError:
                        logging.getLogger(__name__).exception(
                            "Bad job payload",
                            extra={
                                "job_type": job.job_type,
                                "user_id": job.user_id,
                                "payload": (job.payload or "")[:200],
                            },
                        )
                        payload = {}
                    except TypeError:
                        logging.getLogger(__name__).exception(
                            "Bad job payload type", extra={"job_type": job.job_type, "user_id": job.user_id}
                        )
                        payload = {}
                    try:
                        await self._execute_job(bot, job, payload)
                    except asyncio.CancelledError:
                        raise
                    except TelegramNetworkError:
                        # сеть отвалилась — единый retry-поток в jobs (без второго scheduler)
                        retry_at = (utc_now().replace(microsecond=0) + timedelta(seconds=60)).isoformat()
                        await asyncio.to_thread(reschedule, cj, retry_at, last_error="TelegramNetworkError")
                        await asyncio.to_thread(log_event, job.user_id, "job_network_retry", {"job_type": job.job_type})
                        continue
                    except TelegramAPIError as e:
                        await asyncio.to_thread(log_event, job.user_id, "job_telegram_error", {"job_type": job.job_type, "err": str(e)})
                        await asyncio.to_thread(mark_done, job.id, cj.lock_token, last_error=f"TelegramAPIError: {e}")
                    except (sqlite3.Error, ValueError, TypeError, KeyError) as e:
                        await asyncio.to_thread(log_event, job.user_id, "job_error", {"job_type": job.job_type, "err": str(e)})
                        await asyncio.to_thread(mark_done, job.id, cj.lock_token, last_error=f"{type(e).__name__}: {e}")
                    except (ArithmeticError, AssertionError, AttributeError, IndexError, LookupError, NameError, NotImplementedError, OSError, OverflowError, ReferenceError, RuntimeError, SystemError, UnboundLocalError) as e:  # validator: allow-except-exception
                        err = f"{type(e).__name__}: {e}"
                        logging.getLogger(__name__).exception(
                            "Engine job crashed", extra={"job_type": job.job_type, "user_id": int(job.user_id)}
                        )
                        await asyncio.to_thread(log_event, job.user_id, "job_error", {"job_type": job.job_type, "err": str(e)})

                        max_retries = int(os.getenv("ENGINE_JOB_CRASH_MAX_RETRIES", "3") or "3")
                        if int(getattr(cj, "retries", 0) or 0) < max_retries:
                            delay_sec = min(60 * (int(getattr(cj, "retries", 0) or 0) + 1), 900)
                            retry_at = (utc_now().replace(microsecond=0) + timedelta(seconds=delay_sec)).isoformat()
                            await asyncio.to_thread(reschedule, cj, retry_at, last_error=err)
                            await asyncio.to_thread(
                                log_event,
                                job.user_id,
                                "job_crash_retry",
                                {"job_type": job.job_type, "retry_at": retry_at, "retries": int(getattr(cj, "retries", 0) or 0) + 1},
                            )
                            continue

                        await asyncio.to_thread(mark_done, job.id, cj.lock_token, last_error=err)
                    else:
                        await asyncio.to_thread(mark_done, job.id, cj.lock_token)
            finally:
                await release_lock("engine_tick")

    async def _demo_reminder(self, bot: Bot, user_id: int, payload: dict):
        kind = _funnel_kind(payload.get("kind"), default="work")
        text = (
            "🕊 Напоминание\n\n"
            "Совсем скоро я пришлю Вам ресурсный демо-транс. "
            "Выберите спокойное место и, по возможности, наденьте наушники — так эффект ощущается глубже.\n\n"
            "Не включайте практику за рулём или при управлении механизмами. "
            "Дождитесь безопасной остановки или слушайте как пассажир."
        )
        await bot.send_message(user_id, text)
        await asyncio.to_thread(log_event, user_id, "demo_reminder_sent", {"kind": kind})

    async def _demo_send(self, bot: Bot, user_id: int, payload: dict):
        kind = (payload.get("kind") or "work").strip()

        sent, admin_demo_bypass = await asyncio.to_thread(_demo_send_state_sync, int(user_id))
        other = "home" if kind == "work" else "work"

        # Лимит бесплатных демо: максимум 2 (work + home). Дальше предлагаем пакет практик.
        if not admin_demo_bypass and "work" in sent and "home" in sent:
            await bot.send_message(
                user_id,
                "✅ Вы уже получили оба ресурсных демо-транса.\n\nЕсли хотите продолжить — выберите пакет практик.",
                reply_markup=self._kb_after_demo(kind, allow_other=False),
            )
            await asyncio.to_thread(log_event, user_id, "demo_send_skipped", {"reason": "both_sent", "kind": kind})
            return

        # Если такой kind уже отправляли — не пересылаем бесконечно.
        if not admin_demo_bypass and kind in sent:
            await bot.send_message(
                user_id,
                "✅ Этот демо-транс уже был отправлен Вам ранее.\n\n"
                "Вы можете послушать второй ресурсный демо-транс или выбрать пакет практик.",
                reply_markup=self._kb_after_demo(other, allow_other=(other not in sent)),
            )
            await asyncio.to_thread(log_event, user_id, "demo_send_skipped", {"reason": "kind_already_sent", "kind": kind})
            return
        file_path = pick_demo_file(kind)

        if not file_path or not file_path.exists():
            await asyncio.to_thread(log_event, user_id, "demo_missing_file", {"kind": kind, "path": str(file_path) if file_path else None})
            await bot.send_message(
                user_id,
                "⚠️ Демо временно недоступно: аудиофайл не найден. Пожалуйста, сообщите администратору."
            )
            return
        # --- ЭТАП 2: мини-опрос до/после демо ---
        # Для демо используем локальный день проекта; ошибки тут не ожидаются.
        day = today_tz().isoformat()
        sid = await asyncio.to_thread(_create_demo_session_sync, int(user_id), kind=kind, day=day)
        await bot.send_message(
            user_id,
            "📍 Перед прослушиванием: оцените своё состояние сейчас (−10 … +10):\n\nНажмите оценку — и я сразу пришлю демо-аудио.",
            reply_markup=kb_mood_scale(sid, stage="pre"),
        )

        # Аудио и последующие шаги отправим после клика по оценке (handlers/mood.py)
        return

    def _kb_offer(self, user_id: int) -> InlineKeyboardMarkup:
        # Пробный доступ отключён по ТЗ. Оставляем только детерминированные кнопки.
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Пакеты практик", callback_data="sub:menu")],
            [InlineKeyboardButton(text="🎁 Подарить пакет практик", callback_data="gift:menu")],
            [InlineKeyboardButton(text="📣 Посоветовать Метротерапию", callback_data="share:menu")],
            [InlineKeyboardButton(text="⬅️ Меню", callback_data="menu:main")],
        ])

    async def _funnel_nudge(self, bot: Bot, user_id: int, payload: dict):
        """Мягкое касание после отправки демо, если пользователь не нажал «Прослушал»."""
        if await self._should_skip_sales(user_id):
            await asyncio.to_thread(log_event, user_id, "funnel_skipped", {"step": "nudge", "reason": "active_access"})
            return

        kind = _funnel_kind(payload.get("kind"), default="work")
        msg_id = payload.get("message_id")

        # если по этому демо уже есть ack — не пишем
        try:
            if msg_id is not None:
                if await asyncio.to_thread(_demo_ack_exists_sync, user_id=int(user_id), kind=kind, message_id=int(msg_id)):
                    await asyncio.to_thread(log_event, user_id, "funnel_skipped", {"step": "nudge", "reason": "already_acked"})
                    return
        except sqlite3.Error:
            logging.getLogger(__name__).exception("Engine DB check failed (non-fatal)")

        hour = await asyncio.to_thread(_hour_context_sync, int(user_id))
        v = (payload.get("variant") or "").strip().lower()
        if v.startswith("nextday"):
            text = (
                "🕊 Хотите продолжить завтра утром?\n\n"
                "Вчера я отправил(а) Вам демо-транс. Если Вы ещё не успели — ничего страшного. "
                "Когда будет удобно, включите его в спокойном темпе.\n\n"
                "Если Вы уже послушали и хотите продолжить регулярно — выберите пакет практик."
            )
        else:
            # kind уже есть в payload: work/home
            text = funnel_text("nudge", kind=kind, hour=hour)
        await bot.send_message(user_id, text, reply_markup=self._kb_offer(user_id))
        await asyncio.to_thread(log_event, user_id, "funnel_nudge_sent", {"kind": kind})

    async def _funnel_postdemo(self, bot: Bot, user_id: int, payload: dict):
        """Мягкий апсейл после demo_ack.

        Идемпотентность:
          - если у пользователя уже есть доступ → ничего не шлём
          - если после ack он уже открыл тарифы → ничего не шлём
        """
        if await asyncio.to_thread(_is_sub_active_sync, int(user_id)):
            await asyncio.to_thread(log_event, user_id, "funnel_skipped", {"step": "postdemo", "reason": "active_access"})
            return

        ack_at = (payload.get("ack_at_utc") or "").strip()
        if ack_at:
            # если человек уже открыл тарифы после ack — не беспокоим
            try:
                if await asyncio.to_thread(_has_viewed_tariffs_since_sync, user_id=int(user_id), ack_at=ack_at):
                    await asyncio.to_thread(log_event, user_id, "funnel_skipped", {"step": "postdemo", "reason": "already_viewed_tariffs"})
                    return
            except sqlite3.Error:
                logging.getLogger(__name__).exception("Engine DB check failed (non-fatal)")

        kind = _funnel_kind(payload.get("kind"))
        hour = await asyncio.to_thread(_hour_context_sync, int(user_id))
        text = funnel_text("postdemo", kind=kind, hour=hour)
        await bot.send_message(user_id, text, reply_markup=self._kb_offer(user_id))
        await asyncio.to_thread(log_event, user_id, "funnel_postdemo_sent", {"kind": kind})

    async def _funnel_offer(self, bot: Bot, user_id: int, payload: dict):
        if await asyncio.to_thread(_is_sub_active_sync, int(user_id)):
            await asyncio.to_thread(log_event, user_id, "funnel_skipped", {"step": "offer", "reason": "active_access"})
            return

        kind = _funnel_kind(payload.get("kind"))
        hour = await asyncio.to_thread(_hour_context_sync, int(user_id))
        variant = (payload.get("variant") or "").strip().lower()
        step: FunnelStep = "offer_nextday" if variant.startswith("nextday") else "offer"

        # A/B (детерминированно, чтобы рестарт не менял вариант):
        # offer: A для чётных, B для нечётных; nextday — наоборот (чтобы балансировать).
        if step == "offer":
            ab: FunnelVariant = "A" if (int(user_id) % 2 == 0) else "B"
        else:
            ab = "B" if (int(user_id) % 2 == 0) else "A"

        text = funnel_text_ab(step, ab, kind=kind, hour=hour)
        await bot.send_message(user_id, text, reply_markup=self._kb_offer(user_id))

        # событие для админ-аналитики
        ev = f"funnel_{step}_variant_{ab}"
        await asyncio.to_thread(log_event, user_id, ev, {"variant": variant} if variant else {})
        await asyncio.to_thread(log_event, user_id, "funnel_offer_sent", {"step": step, "ab": ab, "variant": variant} if variant else {"step": step, "ab": ab})


    async def _after_paid_setup_ping(self, bot: Bot, user_id: int, payload: dict):
        """Если после оплаты человек не назначил время — мягко напомнить."""
        try:
            if await asyncio.to_thread(_paid_setup_already_configured_sync, int(user_id)):
                return

            text = (
                "🕰 Небольшое напоминание\n\n"
                "Чтобы аудиотрансы приходили ровно тогда, когда Вам удобно, "
                "пожалуйста, назначьте время утреннего и/или вечернего транса."
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏰ Назначить время", callback_data="settings:menu")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
            ])
            await bot.send_message(user_id, text, reply_markup=kb)
            await asyncio.to_thread(log_event, user_id, "after_paid_setup_ping_sent", {})
        except (sqlite3.Error, TelegramAPIError, TelegramNetworkError) as e:
            logging.getLogger(__name__).exception("after_paid_setup_ping failed", extra={"user_id": int(user_id)})
            await asyncio.to_thread(log_event, user_id, "after_paid_setup_ping_error", {"err": str(e)})

    async def _funnel_deadline(self, bot: Bot, user_id: int, payload: dict):
        if await asyncio.to_thread(_is_sub_active_sync, int(user_id)):
            await asyncio.to_thread(log_event, user_id, "funnel_skipped", {"step": "deadline", "reason": "active_access"})
            return
        kind = _funnel_kind(payload.get("kind"))
        hour = await asyncio.to_thread(_hour_context_sync, int(user_id))
        text = funnel_text("deadline", kind=kind, hour=hour)
        await bot.send_message(user_id, text, reply_markup=self._kb_offer(user_id))
        await asyncio.to_thread(log_event, user_id, "funnel_deadline_sent", {})

    async def _funnel_lastcall(self, bot: Bot, user_id: int, payload: dict):
        if await asyncio.to_thread(_is_sub_active_sync, int(user_id)):
            await asyncio.to_thread(log_event, user_id, "funnel_skipped", {"step": "lastcall", "reason": "active_access"})
            return

        kind = _funnel_kind(payload.get("kind"))
        hour = await asyncio.to_thread(_hour_context_sync, int(user_id))
        text = funnel_text("lastcall", kind=kind, hour=hour)
        await bot.send_message(user_id, text, reply_markup=self._kb_offer(user_id))
        await asyncio.to_thread(log_event, user_id, "funnel_lastcall_sent", {})

    async def _remind_continue(self, bot: Bot, user_id: int, payload: dict):
        """Напоминание «продолжить завтра утром».

        Требования:
          - уважительный стиль (Вы)
          - не спамить: постановка задачи уже детерминирована через cancel_jobs(prefix)
        """
        if await asyncio.to_thread(_is_sub_active_sync, int(user_id)):
            await asyncio.to_thread(log_event, user_id, "remind_skipped", {"reason": "active_access"})
            return

        text = (
            "☀️ Доброе утро.\n\n"
            "Если Вы хотите продолжить — выберите пакет практик и откройте полный маршрут. "
            "Тогда бот будет присылать утренние и/или вечерние сессии по расписанию."
        )
        await bot.send_message(user_id, text, reply_markup=self._kb_offer(user_id))
        await asyncio.to_thread(log_event, user_id, "remind_continue_sent", {"src": payload.get("src")})

    async def _clientplatform_booking_reminder(
        self,
        bot: Bot,
        user_id: int,
        payload: dict,
    ) -> None:
        business_id = str(payload.get("business_id") or "").strip()
        slot_id = str(payload.get("slot_id") or "").strip()
        if not business_id or not slot_id:
            return
        try:
            from clientplatform.application.bookings import get_customer_booking
            from clientplatform.domain.bookings import BookingError

            claim = await asyncio.to_thread(
                get_customer_booking,
                telegram_user_id=int(user_id),
                business_id=business_id,
                slot_id=slot_id,
            )
        except BookingError:
            await asyncio.to_thread(
                log_event,
                user_id,
                "clientplatform_booking_reminder_skipped",
                {"slot_id": slot_id, "reason": "booking_inactive"},
            )
            return

        label = str(payload.get("reminder_label") or "скоро").strip()
        await bot.send_message(
            int(user_id),
            "⏰ Напоминание о записи\n\n"
            f"{claim.slot.offering_title} — {claim.slot.local_start}.\n"
            f"Бизнес: {claim.slot.business_name}.\n\n"
            f"Встреча {label}.",
        )
        await asyncio.to_thread(
            log_event,
            user_id,
            "clientplatform_booking_reminder_sent",
            {"slot_id": slot_id, "label": label},
        )

    async def _post_prompt(self, bot: Bot, user_id: int, payload: dict):
        """Пост-оценка состояния после транса.

        Раньше выполнялось через services.session_timers (таблица scheduled_jobs).
        Начиная с v16.3 — выполняется через единый persistent scheduler jobs/run_at_utc.

        Требования:
          - не дублировать (idempotency)
          - при сетевых сбоях допускается retry (Engine.tick уже умеет ретраить network)
        """
        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            return

        # Единый замок: post_prompt должен отправляться ровно один раз,
        # независимо от того, что его триггернуло (job, кнопка, рестарт, ...).
        idem_kind = await asyncio.to_thread(_post_prompt_idem_kind_sync, session_id)

        # Ключ идемпотентности: для сессии канонично sid:<id>,
        # но если передали run_at — добавляем wall_key, чтобы различать редкие кейсы.
        run_at = payload.get("run_at")
        try:
            wall_key = for_job_run_at(str("post_prompt"), f"post:{session_id}", int(run_at)) if run_at is not None else ""
        except (TypeError, ValueError):
            wall_key = ""

        idem_scheduled_at = for_session(session_id) if session_id else (wall_key or "")
        if await asyncio.to_thread(was_delivered, int(user_id), idem_kind, "post_prompt_sent", str(idem_scheduled_at)):
            return

        await bot.send_message(
            int(user_id),
            "Оцени состояние после прослушивания:",
            reply_markup=kb_mood_scale(int(session_id), stage="post"),
        )

        # Маркер ставим ПОСЛЕ успешной отправки, чтобы при Telegram-ошибках был retry.
        await asyncio.to_thread(mark_delivery_once, int(user_id), idem_kind, "post_prompt_sent", str(idem_scheduled_at))

    async def _sub_expiring_soon(self, bot: Bot, user_id: int, payload: dict):
        """Напоминание о продлении (за 3 дня до конца).

        ВАЖНО: Telegram не поддерживает автосписание, поэтому мы легально просим продлить.
        """
        if not await asyncio.to_thread(_is_sub_active_sync, int(user_id)):
            return
        exp = (payload.get("expires_at") or "").strip()
        tail = f"\n\nДата окончания: {exp}" if exp else ""
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Продлить подписку", callback_data="sub:menu")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main")],
        ])
        await bot.send_message(
            user_id,
            "⏳ Подписка скоро закончится.\n\n"
            "Чтобы продолжать получать утренние и вечерние трансы — продлите подписку." + tail,
            reply_markup=kb,
        )
        await asyncio.to_thread(log_event, user_id, "sub_expiring_soon_sent", {})

    async def _funnel2_demo_nopay_24h(self, bot: Bot, user_id: int, payload: dict):
        """Сценарий 2.0: после демо прошло 24ч, оплаты нет.

        Установки:
          - idempotency через funnel_events (mark_sent)
          - UX: мягко, 1 сообщение, кнопки стандартные
        """
        guard = await asyncio.to_thread(_funnel2_demo_nopay_guard_sync, int(user_id), payload)
        if guard != "send":
            return

        text = (
            "🧭 Вы уже попробовали демо — и это только небольшой фрагмент.\n\n"
            "Полный цикл даёт заметно более глубокий эффект: регулярность, постепенное накопление ресурса, "
            "и чёткая структура под Ваш ритм дня.\n\n"
            "Если хотите продолжить — выберите подходящий пакет практик."
        )
        await bot.send_message(user_id, text, reply_markup=self._kb_funnel(user_id))
        await asyncio.to_thread(log_event, user_id, "funnel2_sent", {"scenario": SC_DEMO_NOPAY_24H})

    async def _funnel2_expired_return_3d(self, bot: Bot, user_id: int, payload: dict):
        """Сценарий 2.0: подписка закончилась, прошло 3 дня — мягкий возврат."""
        guard = await asyncio.to_thread(_funnel2_expired_return_guard_sync, int(user_id), payload)
        if guard != "send":
            return

        text = (
            "✨ Иногда достаточно трёх дней без практики, чтобы ресурс начал расходоваться.\n\n"
            "Если Вы хотите вернуть ровный ритм — просто продлите подписку, и утренние/вечерние трансы снова "
            "будут приходить автоматически."
        )
        await bot.send_message(user_id, text, reply_markup=self._kb_funnel(user_id))
        await asyncio.to_thread(log_event, user_id, "funnel2_sent", {"scenario": SC_EXPIRED_RETURN_3D})

engine = Engine()

# Экспорт каталога аудио для handlers/audio.py
# (В проекте ожидается: `from core.engine import catalog`)
catalog = AudioCatalog()
