from __future__ import annotations

from datetime import datetime, timedelta, timezone

from clientplatform.domain.bookings import BookingClaim
from services.jobs import add_job

JOB_TYPE = "clientplatform_booking_reminder"
_REMINDERS = ((24 * 60, "за 24 часа"), (60, "за 1 час"))


def schedule_booking_reminders(
    *,
    telegram_user_id: int,
    claim: BookingClaim,
    now: datetime | None = None,
) -> tuple[int, ...]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    starts_at = datetime.fromisoformat(claim.slot.slot.starts_at).astimezone(timezone.utc)
    scheduled: list[int] = []
    for minutes, label in _REMINDERS:
        run_at = starts_at - timedelta(minutes=minutes)
        if run_at <= current:
            continue
        key = (
            f"clientplatform-booking:{claim.slot.slot.business_id}:"
            f"{claim.slot.slot.id}:{int(telegram_user_id)}:{minutes}"
        )
        inserted = add_job(
            int(telegram_user_id),
            JOB_TYPE,
            run_at.isoformat(timespec="seconds"),
            {
                "business_id": claim.slot.slot.business_id,
                "slot_id": claim.slot.slot.id,
                "reminder_minutes": minutes,
                "reminder_label": label,
            },
            job_key=key,
        )
        if inserted:
            scheduled.append(minutes)
    return tuple(scheduled)


__all__ = ["JOB_TYPE", "schedule_booking_reminders"]
