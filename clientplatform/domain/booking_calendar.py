from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlencode

from clientplatform.domain.bookings import BookingSlotView, normalize_utc_datetime


def _escape_ics(value: str) -> str:
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r", "")
        .replace("\n", "\\n")
    )


def _ics_time(value: str) -> str:
    parsed = datetime.fromisoformat(normalize_utc_datetime(value, field_name="calendar_time"))
    return parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def booking_calendar_filename(slot: BookingSlotView) -> str:
    stamp = datetime.fromisoformat(slot.slot.starts_at).strftime("%Y-%m-%d_%H-%M")
    return f"clientplatform-{stamp}.ics"


def booking_calendar_ics(slot: BookingSlotView) -> bytes:
    uid = f"booking-{slot.slot.id}@clientplatform"
    summary = f"{slot.offering_title} — {slot.business_name}"
    description = (
        f"Запись через ClientPlatform. Время: {slot.local_start}. "
        "Напоминания добавлены за 24 часа и за 1 час."
    )
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//ClientPlatform//Booking//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{_escape_ics(uid)}",
        f"DTSTAMP:{_ics_time(datetime.now(timezone.utc).isoformat())}",
        f"DTSTART:{_ics_time(slot.slot.starts_at)}",
        f"DTEND:{_ics_time(slot.slot.ends_at)}",
        f"SUMMARY:{_escape_ics(summary)}",
        f"DESCRIPTION:{_escape_ics(description)}",
        "BEGIN:VALARM",
        "TRIGGER:-PT24H",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{_escape_ics(summary)} завтра",
        "END:VALARM",
        "BEGIN:VALARM",
        "TRIGGER:-PT1H",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{_escape_ics(summary)} через час",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ]
    return "\r\n".join(lines).encode("utf-8")


def google_calendar_url(slot: BookingSlotView) -> str:
    params = {
        "action": "TEMPLATE",
        "text": f"{slot.offering_title} — {slot.business_name}",
        "dates": f"{_ics_time(slot.slot.starts_at)}/{_ics_time(slot.slot.ends_at)}",
        "details": f"Запись через ClientPlatform. Местное время: {slot.local_start}",
    }
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


__all__ = [
    "booking_calendar_filename",
    "booking_calendar_ics",
    "google_calendar_url",
]
