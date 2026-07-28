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


def parse_local_booking_start(value: str, *, timezone_name: str) -> str:
    raw = " ".join(str(value or "").split())
    try:
        local = datetime.strptime(raw, "%d.%m.%Y %H:%M")
    except ValueError as exc:
        raise ValueError("дата и время должны быть в формате ДД.ММ.ГГГГ ЧЧ:ММ") from exc
    try:
        zone = ZoneInfo(str(timezone_name or "").strip())
    except ZoneInfoNotFoundError as exc:
        raise ValueError("неизвестный часовой пояс бизнеса") from exc
    return local.replace(tzinfo=zone).astimezone(timezone.utc).isoformat(timespec="seconds")


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
