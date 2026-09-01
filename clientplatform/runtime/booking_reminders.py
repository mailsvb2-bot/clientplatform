from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from clientplatform.application.bookings import get_customer_booking
from clientplatform.application.booking_reminders import JOB_TYPE
from clientplatform.domain.bookings import BookingError
from clientplatform.transport.telegram_http import AiohttpTelegramBotClient, TelegramBotApiError
from config.settings import settings
from services.events import log_event
from services.jobs import ClaimedJob, claim_due_jobs, mark_done, reschedule

@dataclass(frozen=True, slots=True)
class BookingReminderBatchResult:
    claimed: int = 0
    sent: int = 0
    retried: int = 0
    dead: int = 0


def _retry_at(job: ClaimedJob) -> str:
    delay = min(900, 5 * (2 ** min(int(job.retries), 7)))
    return (datetime.now(timezone.utc) + timedelta(seconds=delay)).replace(
        microsecond=0
    ).isoformat()

def _payload(job: ClaimedJob) -> dict[str, object]:
    try:
        value = json.loads(job.payload or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _reminder_text(claim: object, label: str) -> str:
    slot = claim.slot
    return (
        "⏰ Напоминание о записи\n\n"
        f"{slot.offering_title} — {slot.local_start}.\n"
        f"Бизнес: {slot.business_name}.\n\n"
        f"Встреча {label}."
    )

async def run_booking_reminder_batch(
    *,
    limit: int = 20,
    max_attempts: int = 8,
    client: AiohttpTelegramBotClient | None = None,
) -> BookingReminderBatchResult:
    jobs = await asyncio.to_thread(
        claim_due_jobs,
        datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        limit=max(1, int(limit)),
        job_type=JOB_TYPE,
    )
    if not jobs:
        return BookingReminderBatchResult()
    sender = client or AiohttpTelegramBotClient()
    token = str(settings.BOT_TOKEN or "").strip()
    sent = retried = dead = 0
    for job in jobs:
        payload = _payload(job)
        business_id = str(payload.get("business_id") or "").strip()
        slot_id = str(payload.get("slot_id") or "").strip()
        label = str(payload.get("reminder_label") or "скоро").strip() or "скоро"
        if not business_id or not slot_id:
            await asyncio.to_thread(
                mark_done, job.id, job.lock_token, last_error="booking_reminder_payload_invalid"
            )
            dead += 1
            continue
        try:
            claim = await asyncio.to_thread(
                get_customer_booking,
                telegram_user_id=int(job.user_id),
                business_id=business_id,
                slot_id=slot_id,
            )
        except BookingError:
            await asyncio.to_thread(
                mark_done, job.id, job.lock_token, last_error="booking_reminder_inactive"
            )
            continue
        try:
            if not token:
                raise TelegramBotApiError(
                    "telegram_control_bot_token_missing",
                    retryable=True,
                )
            await sender.send_message(
                token=token,
                chat_id=str(int(job.user_id)),
                text=_reminder_text(claim, label),
            )
        except TelegramBotApiError as exc:
            if exc.retryable and int(job.retries) + 1 < max(1, int(max_attempts)):
                await asyncio.to_thread(
                    reschedule,
                    job,
                    _retry_at(job),
                    last_error=exc.code,
                )
                retried += 1
            else:
                await asyncio.to_thread(
                    mark_done,
                    job.id,
                    job.lock_token,
                    last_error=exc.code,
                )
                dead += 1
            continue
        await asyncio.to_thread(mark_done, job.id, job.lock_token)
        await asyncio.to_thread(
            log_event,
            int(job.user_id),
            "clientplatform_booking_reminder_sent",
            {"slot_id": slot_id, "label": label},
        )
        sent += 1

    return BookingReminderBatchResult(
        claimed=len(jobs),
        sent=sent,
        retried=retried,
        dead=dead,
    )


__all__ = ["BookingReminderBatchResult", "run_booking_reminder_batch"]
