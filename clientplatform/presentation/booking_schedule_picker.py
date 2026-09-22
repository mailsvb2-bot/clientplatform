from __future__ import annotations

BOOKING_START_TIMES = tuple(
    f"{hour:02d}:{minute:02d}"
    for hour in range(8, 22)
    for minute in (0, 30)
) + ("22:00",)

BOOKING_DURATIONS = (15, 30, 45, 60, 75, 90, 120, 180)

BOOKING_DURATION_LABELS = {
    15: "15 мин",
    30: "30 мин",
    45: "45 мин",
    60: "1 час",
    75: "1 ч 15 мин",
    90: "1,5 часа",
    120: "2 часа",
    180: "3 часа",
}

BOOKING_MONTHS_RU_FULL = (
    "",
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
)


def parse_booking_start_time(value: object) -> str:
    raw = str(value or "").strip()
    if raw not in BOOKING_START_TIMES:
        raise ValueError("unsupported booking start time")
    return raw


def parse_booking_duration(value: object) -> int:
    try:
        minutes = int(str(value or "").strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("unsupported booking duration") from exc
    if minutes not in BOOKING_DURATIONS:
        raise ValueError("unsupported booking duration")
    return minutes


__all__ = [
    "BOOKING_DURATIONS",
    "BOOKING_DURATION_LABELS",
    "BOOKING_MONTHS_RU_FULL",
    "BOOKING_START_TIMES",
    "parse_booking_duration",
    "parse_booking_start_time",
]
