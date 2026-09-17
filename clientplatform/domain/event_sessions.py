from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from clientplatform.domain.events import (
    Event,
    EventValidationError,
    normalize_provider_key,
    normalize_provider_label,
    normalize_utc,
    validate_external_https_url,
)
from clientplatform.domain.tenancy import normalize_uuid


@dataclass(frozen=True, slots=True)
class EventSession:
    """One scheduled occurrence of a canonical ClientPlatform event."""

    id: str
    business_id: str
    event_id: str
    position: int
    starts_at: datetime
    ends_at: datetime | None
    provider_key: str
    provider_label: str | None
    join_url: str | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", normalize_uuid(self.id, field_name="event_session_id"))
        object.__setattr__(
            self, "business_id", normalize_uuid(self.business_id, field_name="business_id")
        )
        object.__setattr__(self, "event_id", normalize_uuid(self.event_id, field_name="event_id"))
        if isinstance(self.position, bool) or not isinstance(self.position, int) or self.position < 1:
            raise EventValidationError("event session position must be a positive integer")
        start = normalize_utc(self.starts_at, field_name="session_starts_at")
        end = (
            None
            if self.ends_at is None
            else normalize_utc(self.ends_at, field_name="session_ends_at")
        )
        if end is not None and end <= start:
            raise EventValidationError("event session must end after it starts")
        object.__setattr__(self, "starts_at", start)
        object.__setattr__(self, "ends_at", end)
        raw_join = str(self.join_url or "").strip()
        join_url = (
            None
            if not raw_join
            else validate_external_https_url(raw_join, field_name="session_join_url")
        )
        object.__setattr__(self, "join_url", join_url)
        object.__setattr__(
            self,
            "provider_key",
            normalize_provider_key(self.provider_key, join_url=join_url),
        )
        object.__setattr__(self, "provider_label", normalize_provider_label(self.provider_label))
        object.__setattr__(
            self, "created_at", normalize_utc(self.created_at, field_name="session_created_at")
        )
        object.__setattr__(
            self, "updated_at", normalize_utc(self.updated_at, field_name="session_updated_at")
        )

    @property
    def join_is_ready(self) -> bool:
        return bool(self.join_url) and self.provider_key != "pending"


def legacy_event_session(event: Event) -> EventSession:
    """Expose a legacy single-occurrence event through the session contract."""

    session_id = str(uuid5(NAMESPACE_URL, f"clientplatform:event-session:{event.id}:1"))
    return EventSession(
        id=session_id,
        business_id=event.business_id,
        event_id=event.id,
        position=1,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        provider_key=event.provider_key,
        provider_label=event.provider_label,
        join_url=event.join_url,
        created_at=event.created_at,
        updated_at=event.updated_at,
    )


def validate_event_session_sequence(
    sessions: tuple[EventSession, ...] | list[EventSession],
    *,
    business_id: str,
    event_id: str,
) -> tuple[EventSession, ...]:
    """Validate one ordered, tenant-scoped session sequence for an event."""

    expected_business_id = normalize_uuid(business_id, field_name="business_id")
    expected_event_id = normalize_uuid(event_id, field_name="event_id")
    ordered = tuple(sessions)
    if not ordered:
        raise EventValidationError("event must have at least one session")
    previous_start: datetime | None = None
    seen_ids: set[str] = set()
    for expected_position, session in enumerate(ordered, start=1):
        if session.business_id != expected_business_id or session.event_id != expected_event_id:
            raise EventValidationError("event session belongs to another event or business")
        if session.position != expected_position:
            raise EventValidationError("event session positions must be contiguous from 1")
        if session.id in seen_ids:
            raise EventValidationError("event session ids must be unique")
        if previous_start is not None and session.starts_at <= previous_start:
            raise EventValidationError("event sessions must start in chronological order")
        seen_ids.add(session.id)
        previous_start = session.starts_at
    return ordered
