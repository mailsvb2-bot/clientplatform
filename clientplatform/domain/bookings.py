from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clientplatform.domain.tenancy import normalize_user_id, normalize_uuid


class BookingError(RuntimeError):
    """Base error for consultation and service booking."""


class BookingNotFound(BookingError):
    """The requested tenant-scoped booking object does not exist."""


class BookingInvariantViolation(BookingError):
    """A booking transition would violate availability or ownership rules."""


class BookingSlotStatus(StrEnum):
    OPEN = "open"
    BOOKED = "booked"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


def normalize_duration_minutes(value: int) -> int:
    if isinstance(value, bool):
        raise ValueError("duration_minutes must be an integer")
    normalized = int(value)
    if normalized < 15 or normalized > 1440:
        raise ValueError("duration_minutes must be between 15 and 1440")
    return normalized


def normalize_utc_datetime(value: str, *, field_name: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field_name} must not be empty")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO datetime") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include timezone information")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _valid_local_occurrences(local: datetime, zone: ZoneInfo) -> list[datetime]:
    """Return real UTC-resolvable occurrences for one local wall-clock value.

    ``datetime.replace(tzinfo=ZoneInfo(...))`` does not validate DST gaps and
    silently chooses ``fold=0`` for repeated autumn times. Round-tripping both
    folds through UTC gives a strict, dependency-free validity test.
    """

    occurrences: list[datetime] = []
    for fold in (0, 1):
        candidate = local.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(timezone.utc).astimezone(zone)
        if round_trip.replace(tzinfo=None) != local or round_trip.fold != fold:
            continue
        occurrences.append(candidate)
    return occurrences


def _parse_local_wall_clock(
    raw: str,
    *,
    zone: ZoneInfo,
    now_utc: str | None,
) -> datetime:
    for pattern in ("%d.%m.%Y %H:%M", "%d.%m.%y %H:%M"):
        try:
            return datetime.strptime(raw, pattern)
        except ValueError:
            pass

    try:
        month_day = datetime.strptime(raw, "%d.%m %H:%M")
    except ValueError as exc:
        raise ValueError(
            "дата и время должны выглядеть как 10.08 15:00, "
            "10.08.26 15:00 или 10.08.2026 15:00"
        ) from exc

    if now_utc is None:
        reference_utc = datetime.now(timezone.utc)
    else:
        normalized_now = normalize_utc_datetime(now_utc, field_name="now")
        reference_utc = datetime.fromisoformat(normalized_now)
    reference_local = reference_utc.astimezone(zone)
    candidate = month_day.replace(year=reference_local.year)
    if candidate <= reference_local.replace(tzinfo=None):
        candidate = candidate.replace(year=reference_local.year + 1)
    return candidate


def parse_local_booking_start(
    value: str,
    *,
    timezone_name: str,
    now_utc: str | None = None,
) -> str:
    raw = " ".join(str(value or "").split())
    try:
        zone = ZoneInfo(str(timezone_name or "").strip())
    except ZoneInfoNotFoundError as exc:
        raise ValueError("неизвестный часовой пояс бизнеса") from exc

    local = _parse_local_wall_clock(raw, zone=zone, now_utc=now_utc)
    occurrences = _valid_local_occurrences(local, zone)
    if not occurrences:
        raise ValueError(
            "такого местного времени не существует из-за перехода на летнее время; "
            "выберите реальное время"
        )
    if len(occurrences) > 1:
        raise ValueError(
            "это местное время повторяется при переходе на зимнее время; "
            "выберите другое однозначное время"
        )
    return occurrences[0].astimezone(timezone.utc).isoformat(timespec="seconds")


def booking_end(starts_at: str, duration_minutes: int) -> str:
    start = datetime.fromisoformat(normalize_utc_datetime(starts_at, field_name="starts_at"))
    return (start + timedelta(minutes=normalize_duration_minutes(duration_minutes))).isoformat(
        timespec="seconds"
    )


def format_booking_local(starts_at: str, *, timezone_name: str) -> str:
    start = datetime.fromisoformat(normalize_utc_datetime(starts_at, field_name="starts_at"))
    try:
        zone = ZoneInfo(str(timezone_name or "").strip())
    except ZoneInfoNotFoundError as exc:
        raise ValueError("неизвестный часовой пояс бизнеса") from exc
    return start.astimezone(zone).strftime("%d.%m.%Y %H:%M")


@dataclass(frozen=True, slots=True)
class CustomerBusinessLink:
    business_id: str
    business_name: str
    customer_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "business_id", normalize_uuid(self.business_id, field_name="business_id"))
        object.__setattr__(self, "customer_id", normalize_uuid(self.customer_id, field_name="customer_id"))
        normalized_name = " ".join(str(self.business_name or "").split())
        if not normalized_name:
            raise ValueError("business_name must not be empty")
        object.__setattr__(self, "business_name", normalized_name)


@dataclass(frozen=True, slots=True)
class BookingSlot:
    id: str
    business_id: str
    offering_id: str
    starts_at: str
    ends_at: str
    duration_minutes: int
    status: BookingSlotStatus
    created_by_member_id: str
    created_at: str
    updated_at: str
    booked_customer_id: str | None = None
    booked_at: str | None = None
    cancelled_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", normalize_uuid(self.id, field_name="booking_slot_id"))
        object.__setattr__(self, "business_id", normalize_uuid(self.business_id, field_name="business_id"))
        object.__setattr__(self, "offering_id", normalize_uuid(self.offering_id, field_name="offering_id"))
        object.__setattr__(
            self,
            "created_by_member_id",
            normalize_uuid(self.created_by_member_id, field_name="created_by_member_id"),
        )
        object.__setattr__(self, "starts_at", normalize_utc_datetime(self.starts_at, field_name="starts_at"))
        object.__setattr__(self, "ends_at", normalize_utc_datetime(self.ends_at, field_name="ends_at"))
        object.__setattr__(self, "duration_minutes", normalize_duration_minutes(self.duration_minutes))
        if datetime.fromisoformat(self.ends_at) <= datetime.fromisoformat(self.starts_at):
            raise ValueError("booking slot end must be after start")
        if self.booked_customer_id is not None:
            object.__setattr__(
                self,
                "booked_customer_id",
                normalize_uuid(self.booked_customer_id, field_name="booked_customer_id"),
            )
        if self.status == BookingSlotStatus.BOOKED and self.booked_customer_id is None:
            raise ValueError("booked slot requires booked_customer_id")


@dataclass(frozen=True, slots=True)
class BookingSlotView:
    slot: BookingSlot
    offering_title: str
    business_name: str
    timezone: str

    @property
    def local_start(self) -> str:
        return format_booking_local(self.slot.starts_at, timezone_name=self.timezone)


@dataclass(frozen=True, slots=True)
class BookingClaim:
    slot: BookingSlotView
    customer_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "customer_id", normalize_uuid(self.customer_id, field_name="customer_id"))


def normalize_telegram_principal(value: int) -> int:
    return normalize_user_id(value)
