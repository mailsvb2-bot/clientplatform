from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clientplatform.domain.bookings import parse_local_booking_start


MAX_EVENT_SESSIONS = 31
MOSCOW_TIMEZONE = "Europe/Moscow"

_SESSION_WINDOW_RE = re.compile(
    r"^\s*(?P<date>\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})\s+"
    r"(?P<start>\d{1,2}:\d{2})\s*(?:-|–|—|до)\s*(?P<end>\d{1,2}:\d{2})\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class EventWizardSession:
    position: int
    starts_at: datetime
    ends_at: datetime
    local_label: str
    join_url: str | None = None


def parse_session_count(value: object) -> int:
    raw = " ".join(str(value or "").strip().casefold().split())
    aliases = {
        "один": "1",
        "один день": "1",
        "два": "2",
        "два дня": "2",
    }
    raw = aliases.get(raw, raw)
    if raw.endswith(" дней"):
        raw = raw[:-5].strip()
    elif raw.endswith(" дня"):
        raw = raw[:-4].strip()
    elif raw.endswith(" день"):
        raw = raw[:-5].strip()
    try:
        count = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"session count must be within 1..{MAX_EVENT_SESSIONS}") from exc
    if count < 1 or count > MAX_EVENT_SESSIONS:
        raise ValueError(f"session count must be within 1..{MAX_EVENT_SESSIONS}")
    return count


def normalize_event_timezone(value: object) -> str:
    raw = " ".join(str(value or "").strip().split())
    folded = raw.casefold()
    if folded in {
        "москва",
        "московское",
        "московское время",
        "мск",
        "msk",
        "moscow",
        MOSCOW_TIMEZONE.casefold(),
    }:
        return MOSCOW_TIMEZONE
    if not raw or len(raw) > 80:
        raise ValueError("timezone must be Moscow or a valid IANA timezone")
    try:
        ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("timezone must be Moscow or a valid IANA timezone") from exc
    return raw


def _normalise_date_token(value: str) -> str:
    return value.replace("/", ".").replace("-", ".")


def parse_session_window(value: object, *, timezone_name: str, position: int) -> EventWizardSession:
    raw = " ".join(str(value or "").strip().split())
    match = _SESSION_WINDOW_RE.fullmatch(raw)
    if match is None:
        raise ValueError("session window format is invalid")
    local_date = _normalise_date_token(match.group("date"))
    start_clock = match.group("start")
    end_clock = match.group("end")
    starts_at = datetime.fromisoformat(
        parse_local_booking_start(f"{local_date} {start_clock}", timezone_name=timezone_name)
    )
    ends_at = datetime.fromisoformat(
        parse_local_booking_start(f"{local_date} {end_clock}", timezone_name=timezone_name)
    )
    if ends_at <= starts_at:
        raise ValueError("session end must be after start")
    return EventWizardSession(
        position=int(position),
        starts_at=starts_at,
        ends_at=ends_at,
        local_label=f"{local_date} {start_clock}–{end_clock}",
    )


def validate_session_sequence(
    session: EventWizardSession,
    *,
    previous: EventWizardSession | None,
) -> EventWizardSession:
    if previous is not None and session.starts_at < previous.ends_at:
        raise ValueError("sessions must not overlap and must stay chronological")
    return session


def normalize_session_join_url(
    value: object,
    *,
    existing_urls: tuple[str, ...] = (),
) -> str | None:
    raw = str(value or "").strip()
    if raw.casefold() in {"", "-", "позже"}:
        return None
    if not raw.startswith("https://"):
        raise ValueError("join URL must use HTTPS")
    if raw in set(existing_urls):
        raise ValueError("every session must use its own room URL")
    return raw


__all__ = [
    "EventWizardSession",
    "MAX_EVENT_SESSIONS",
    "MOSCOW_TIMEZONE",
    "normalize_event_timezone",
    "normalize_session_join_url",
    "parse_session_count",
    "parse_session_window",
    "validate_session_sequence",
]
