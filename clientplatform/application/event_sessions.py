from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from clientplatform.domain.event_sessions import EventSession
from clientplatform.domain.events import (
    normalize_provider_key,
    normalize_provider_label,
    normalize_utc,
    validate_external_https_url,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.event_repository import EventRepository
from clientplatform.infrastructure.event_session_repository import EventSessionRepository
from services.db import get_db, get_db_ro


@dataclass(frozen=True, slots=True)
class EventSessionSpec:
    starts_at: datetime
    ends_at: datetime | None = None
    join_url: str | None = None
    provider_key: str | None = None
    provider_label: str | None = None


@dataclass(frozen=True, slots=True)
class EventWarmupWindow:
    days_until_event: int
    max_warmup_days: int
    timezone_name: str


def warmup_window_for_sessions(
    sessions: tuple[EventSession, ...] | list[EventSession],
    *,
    timezone_name: str,
    now: datetime | None = None,
) -> EventWarmupWindow:
    ordered = tuple(sessions)
    if not ordered:
        raise ValueError("event sessions are required")
    current = normalize_utc(now or datetime.now(timezone.utc), field_name="now")
    zone = ZoneInfo(str(timezone_name))
    current_date = current.astimezone(zone).date()
    first_date = ordered[0].starts_at.astimezone(zone).date()
    days = max((first_date - current_date).days, 0)
    return EventWarmupWindow(
        days_until_event=days,
        max_warmup_days=days,
        timezone_name=str(timezone_name),
    )


def validate_warmup_days(requested_days: int, *, max_warmup_days: int) -> int:
    if isinstance(requested_days, bool) or not isinstance(requested_days, int):
        raise ValueError("warmup days must be an integer")
    if requested_days < 0:
        raise ValueError("warmup days must not be negative")
    if requested_days > max_warmup_days:
        raise ValueError("warmup days exceed the time remaining before the event")
    return requested_days


def configure_event_sessions_in_transaction(
    conn: Any,
    *,
    actor: TenantContext,
    event_id: str,
    sessions: tuple[EventSessionSpec, ...] | list[EventSessionSpec],
    now: datetime | None = None,
) -> tuple[EventSession, ...]:
    specs = tuple(sessions)
    if not specs:
        raise ValueError("event must have at least one session")
    current = normalize_utc(now or datetime.now(timezone.utc), field_name="now")
    event = EventRepository(conn).get(actor=actor, event_id=event_id)
    materialized: list[EventSession] = []
    for position, spec in enumerate(specs, start=1):
        starts_at = normalize_utc(spec.starts_at, field_name="session_starts_at")
        if starts_at <= current:
            raise ValueError("event session must start in the future")
        raw_join = str(spec.join_url or "").strip()
        join_url = (
            None
            if not raw_join
            else validate_external_https_url(raw_join, field_name="session_join_url")
        )
        materialized.append(
            EventSession(
                id=str(uuid4()),
                business_id=event.business_id,
                event_id=event.id,
                position=position,
                starts_at=starts_at,
                ends_at=spec.ends_at,
                provider_key=normalize_provider_key(spec.provider_key, join_url=join_url),
                provider_label=normalize_provider_label(spec.provider_label),
                join_url=join_url,
                created_at=current,
                updated_at=current,
            )
        )
    return EventSessionRepository(conn).replace_for_event(
        actor=actor,
        event_id=event.id,
        sessions=materialized,
        now=current,
    )


def configure_event_sessions(
    *,
    actor: TenantContext,
    event_id: str,
    sessions: tuple[EventSessionSpec, ...] | list[EventSessionSpec],
    now: datetime | None = None,
) -> tuple[EventSession, ...]:
    with get_db() as conn:
        return configure_event_sessions_in_transaction(
            conn,
            actor=actor,
            event_id=event_id,
            sessions=sessions,
            now=now,
        )


def list_event_sessions(
    *,
    actor: TenantContext,
    event_id: str,
) -> tuple[EventSession, ...]:
    with get_db_ro() as conn:
        return EventSessionRepository(conn).list_for_event(actor=actor, event_id=event_id)


def get_event_warmup_window(
    *,
    actor: TenantContext,
    event_id: str,
    now: datetime | None = None,
) -> EventWarmupWindow:
    with get_db_ro() as conn:
        event = EventRepository(conn).get(actor=actor, event_id=event_id)
        sessions = EventSessionRepository(conn).list_for_event(actor=actor, event_id=event.id)
        return warmup_window_for_sessions(
            sessions,
            timezone_name=event.timezone_name,
            now=now,
        )


def set_event_session_join_target(
    *,
    actor: TenantContext,
    event_id: str,
    position: int,
    join_url: str,
    provider_key: str = "auto",
    provider_label: str | None = None,
    now: datetime | None = None,
) -> EventSession:
    with get_db() as conn:
        return EventSessionRepository(conn).set_join_target(
            actor=actor,
            event_id=event_id,
            position=position,
            join_url=join_url,
            provider_key=provider_key,
            provider_label=provider_label,
            now=now,
        )


__all__ = [
    "EventSessionSpec",
    "EventWarmupWindow",
    "configure_event_sessions",
    "configure_event_sessions_in_transaction",
    "get_event_warmup_window",
    "list_event_sessions",
    "set_event_session_join_target",
    "validate_warmup_days",
    "warmup_window_for_sessions",
]
