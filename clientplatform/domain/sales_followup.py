from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clientplatform.domain.tenancy import normalize_uuid


class SalesFollowupStatus(StrEnum):
    SCHEDULED = "scheduled"
    QUEUED = "queued"
    SENT = "sent"
    STOPPED = "stopped"
    CANCELLED = "cancelled"
    DEAD = "dead"


class SalesFollowupStopReason(StrEnum):
    REPLY = "reply"
    BOOKING = "booking"
    PAYMENT = "payment"
    OPT_OUT = "opt_out"
    LEAD_CLOSED = "lead_closed"
    IDENTITY_REVOKED = "identity_revoked"
    CONTACT_FORBIDDEN = "contact_forbidden"
    CHANNEL_UNAVAILABLE = "channel_unavailable"
    FREQUENCY_CAP = "frequency_cap"
    DELIVERY_FAILED = "delivery_failed"
    OWNER_CANCELLED = "owner_cancelled"


SUPPORTED_FOLLOWUP_PLATFORMS = frozenset({"telegram", "vk", "max"})
STALE_LEAD_AFTER = timedelta(hours=24)
MAX_SENT_FOLLOWUPS_PER_LEAD = 3
QUIET_START = time(21, 0)
QUIET_END = time(9, 0)


def normalize_followup_message(value: object) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").replace("\x00", " ")).strip()
    if not normalized:
        raise ValueError("follow-up message must not be empty")
    if len(normalized) > 4000:
        raise ValueError("follow-up message must be at most 4000 characters")
    return normalized


def _aware(value: datetime | str, *, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _zone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(timezone_name or "").strip())
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("business timezone must be a known IANA timezone") from exc


def is_quiet_time(value: datetime | str, *, timezone_name: str) -> bool:
    local = _aware(value, field_name="timestamp").astimezone(_zone(timezone_name))
    clock = local.timetz().replace(tzinfo=None)
    return clock >= QUIET_START or clock < QUIET_END


def next_allowed_followup_time(
    value: datetime | str,
    *,
    timezone_name: str,
) -> datetime:
    current = _aware(value, field_name="scheduled_at")
    zone = _zone(timezone_name)
    local = current.astimezone(zone)
    clock = local.timetz().replace(tzinfo=None)
    if QUIET_END <= clock < QUIET_START:
        return current
    if clock >= QUIET_START:
        target_date = local.date() + timedelta(days=1)
    else:
        target_date = local.date()
    allowed_local = datetime.combine(target_date, QUIET_END, tzinfo=zone)
    return allowed_local.astimezone(timezone.utc).replace(microsecond=0)


def is_stale_lead(last_signal_at: datetime | str, *, now: datetime | str) -> bool:
    return _aware(last_signal_at, field_name="last_signal_at") <= (
        _aware(now, field_name="now") - STALE_LEAD_AFTER
    )


@dataclass(frozen=True, slots=True)
class SalesFollowup:
    id: str
    business_id: str
    lead_id: str
    customer_id: str
    platform: str
    customer_identity_id: str
    connection_id: str
    message_text: str
    scheduled_at: str
    status: SalesFollowupStatus
    idempotency_key: str
    created_by_member_id: str
    created_at: str
    updated_at: str
    provider_dispatch_id: str | None = None
    queued_at: str | None = None
    sent_at: str | None = None
    stopped_at: str | None = None
    stop_reason: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "id",
            "business_id",
            "lead_id",
            "customer_id",
            "customer_identity_id",
            "connection_id",
            "created_by_member_id",
        ):
            object.__setattr__(
                self,
                field_name,
                normalize_uuid(getattr(self, field_name), field_name=field_name),
            )
        platform = str(self.platform or "").strip().lower()
        if platform not in SUPPORTED_FOLLOWUP_PLATFORMS:
            raise ValueError("follow-up platform must be Telegram, VK or MAX")
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "message_text", normalize_followup_message(self.message_text))
        object.__setattr__(self, "status", SalesFollowupStatus(str(self.status)))
        _aware(self.scheduled_at, field_name="scheduled_at")
