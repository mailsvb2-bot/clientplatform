from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MAX_CALENDAR_MONTHS = 18
QUICK_START_TIMES = ("10:00", "12:00", "15:00", "18:00", "19:00", "20:00")
QUICK_DURATIONS = (30, 60, 90, 120, 180)


@dataclass(frozen=True, slots=True)
class WebinarVenue:
    key: str
    label: str
    open_url: str | None
    public_room_supported: bool = True
    note: str = ""


@dataclass(frozen=True, slots=True)
class CalendarDay:
    day: int
    value: str | None
    enabled: bool


WEBINAR_VENUES = (
    WebinarVenue(
        key="telemost",
        label="Яндекс Телемост",
        open_url="https://telemost.yandex.ru/",
    ),
    WebinarVenue(
        key="zoom",
        label="Zoom",
        open_url="https://zoom.us/meeting/schedule",
    ),
    WebinarVenue(
        key="webinar_ru",
        label="Webinar.ru",
        open_url="https://webinar.ru/",
    ),
    WebinarVenue(
        key="getcourse",
        label="GetCourse",
        open_url="https://getcourse.ru/",
        note="Комната создаётся внутри вашего аккаунта GetCourse.",
    ),
    WebinarVenue(
        key="ucr",
        label="UCR",
        open_url=None,
        public_room_supported=False,
        note=(
            "Текущий UCR-контракт умеет создавать conversation/call, но пока не выдаёт "
            "публичную ссылку на постоянную вебинарную комнату."
        ),
    ),
    WebinarVenue(
        key="other",
        label="Другой сервис",
        open_url=None,
        note="После выбора просто вставьте HTTPS-ссылку на созданную комнату.",
    ),
)


_VENUES_BY_KEY = {item.key: item for item in WEBINAR_VENUES}


def webinar_venue(key: object) -> WebinarVenue:
    normalized = str(key or "").strip().casefold()
    try:
        return _VENUES_BY_KEY[normalized]
    except KeyError as exc:
        raise ValueError("unsupported webinar venue") from exc


def local_today(timezone_name: str, *, now: datetime | None = None) -> date:
    try:
        zone = ZoneInfo(str(timezone_name).strip())
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("invalid event timezone") from exc
    current = now or datetime.now(tz=ZoneInfo("UTC"))
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return current.astimezone(zone).date()


def month_key(year: int, month: int) -> str:
    if not 1 <= int(month) <= 12 or int(year) < 2000 or int(year) > 2200:
        raise ValueError("invalid calendar month")
    return f"{int(year):04d}{int(month):02d}"


def parse_month_key(value: object) -> tuple[int, int]:
    raw = str(value or "").strip()
    if len(raw) != 6 or not raw.isdigit():
        raise ValueError("invalid calendar month")
    year = int(raw[:4])
    month = int(raw[4:])
    month_key(year, month)
    return year, month


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    if delta not in {-1, 1}:
        raise ValueError("calendar month shift must be one step")
    month_key(year, month)
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def month_distance(start: date, *, year: int, month: int) -> int:
    return (year - start.year) * 12 + (month - start.month)


def validate_calendar_month(
    *,
    year: int,
    month: int,
    minimum: date,
    max_months: int = MAX_CALENDAR_MONTHS,
) -> None:
    if max_months < 0 or max_months > 36:
        raise ValueError("calendar month window is invalid")
    month_key(year, month)
    distance = month_distance(minimum, year=year, month=month)
    if distance < 0 or distance > max_months:
        raise ValueError("calendar month is outside the allowed window")


def calendar_days(
    *,
    year: int,
    month: int,
    minimum: date,
    max_months: int = MAX_CALENDAR_MONTHS,
) -> tuple[tuple[CalendarDay, ...], ...]:
    validate_calendar_month(
        year=year,
        month=month,
        minimum=minimum,
        max_months=max_months,
    )
    weeks: list[tuple[CalendarDay, ...]] = []
    for week in calendar.Calendar(firstweekday=0).monthdayscalendar(year, month):
        cells: list[CalendarDay] = []
        for day_number in week:
            if day_number == 0:
                cells.append(CalendarDay(day=0, value=None, enabled=False))
                continue
            current = date(year, month, day_number)
            enabled = current >= minimum
            cells.append(
                CalendarDay(
                    day=day_number,
                    value=current.isoformat() if enabled else None,
                    enabled=enabled,
                )
            )
        weeks.append(tuple(cells))
    return tuple(weeks)


def parse_calendar_date(
    value: object,
    *,
    minimum: date,
    max_months: int = MAX_CALENDAR_MONTHS,
) -> date:
    raw = str(value or "").strip()
    try:
        selected = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("invalid calendar date") from exc
    validate_calendar_month(
        year=selected.year,
        month=selected.month,
        minimum=minimum,
        max_months=max_months,
    )
    if selected < minimum:
        raise ValueError("calendar date is in the past")
    return selected


def parse_quick_time(value: object) -> str:
    raw = str(value or "").strip()
    if raw not in QUICK_START_TIMES:
        raise ValueError("unsupported quick start time")
    return raw


def parse_quick_duration(value: object) -> int:
    try:
        minutes = int(str(value or "").strip())
    except ValueError as exc:
        raise ValueError("unsupported quick duration") from exc
    if minutes not in QUICK_DURATIONS:
        raise ValueError("unsupported quick duration")
    return minutes


def session_window_text(*, selected_date: date, start_time: str, duration_minutes: int) -> str:
    if duration_minutes not in QUICK_DURATIONS:
        raise ValueError("unsupported quick duration")
    try:
        start_clock = datetime.strptime(start_time, "%H:%M")
    except ValueError as exc:
        raise ValueError("invalid start time") from exc
    start_local = datetime.combine(selected_date, start_clock.time())
    end_local = start_local + timedelta(minutes=duration_minutes)
    if end_local.date() != selected_date:
        raise ValueError("quick session cannot cross midnight")
    return (
        f"{selected_date.strftime('%d.%m.%Y')} "
        f"{start_local.strftime('%H:%M')}-{end_local.strftime('%H:%M')}"
    )


__all__ = [
    "CalendarDay",
    "MAX_CALENDAR_MONTHS",
    "QUICK_DURATIONS",
    "QUICK_START_TIMES",
    "WEBINAR_VENUES",
    "WebinarVenue",
    "calendar_days",
    "local_today",
    "month_key",
    "parse_calendar_date",
    "parse_month_key",
    "parse_quick_duration",
    "parse_quick_time",
    "session_window_text",
    "shift_month",
    "validate_calendar_month",
    "webinar_venue",
]
