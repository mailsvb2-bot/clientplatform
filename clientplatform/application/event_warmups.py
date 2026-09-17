from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from clientplatform.application.event_sessions import (
    validate_warmup_days,
    warmup_window_for_sessions,
)
from clientplatform.domain.events import normalize_utc
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.event_repository import EventRepository
from clientplatform.infrastructure.event_session_repository import EventSessionRepository
from services.db import get_db_ro


@dataclass(frozen=True, slots=True)
class EventWarmupDraft:
    position: int
    publish_date: date
    days_before_event: int
    text: str


@dataclass(frozen=True, slots=True)
class EventWarmupPlan:
    event_id: str
    timezone_name: str
    days_until_event: int
    requested_days: int
    drafts: tuple[EventWarmupDraft, ...]


def _clean(value: object, *, limit: int) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _safe_warmup_text(
    *,
    title: str,
    description: str,
    days_before_event: int,
    position: int,
    total: int,
) -> str:
    clean_title = _clean(title, limit=240)
    clean_description = _clean(description, limit=700)
    if days_before_event == 1:
        opener = "Уже завтра"
    elif days_before_event == 2:
        opener = "До вебинара осталось 2 дня"
    else:
        opener = f"До вебинара осталось {days_before_event} дн."

    if position == 1 and total > 1:
        angle = "Начинаем прогрев к теме и фиксируем дату, чтобы не потерять эфир."
    elif position == total:
        angle = "Завтра перед стартом придёт организационное напоминание с персональной ссылкой на эфир."
    else:
        angle = "Возвращаемся к теме и готовимся применить материал на вебинаре."

    parts = [f"🔥 {opener}: «{clean_title}».", "", angle]
    if clean_description:
        parts.extend(["", clean_description])
    parts.extend(["", "Регистрация — по ссылке мероприятия."])
    return "\n".join(parts)


def build_event_warmup_plan(
    *,
    event_id: str,
    title: str,
    description: str,
    timezone_name: str,
    first_session_starts_at: datetime,
    requested_days: int,
    now: datetime | None = None,
) -> EventWarmupPlan:
    current = normalize_utc(now or datetime.now(timezone.utc), field_name="now")
    zone = ZoneInfo(str(timezone_name))
    first_local_date = first_session_starts_at.astimezone(zone).date()
    current_local_date = current.astimezone(zone).date()
    days_until_event = max((first_local_date - current_local_date).days, 0)
    selected = validate_warmup_days(
        requested_days,
        max_warmup_days=days_until_event,
    )
    if selected == 0:
        return EventWarmupPlan(
            event_id=event_id,
            timezone_name=str(timezone_name),
            days_until_event=days_until_event,
            requested_days=0,
            drafts=(),
        )

    first_publish_date = first_local_date - timedelta(days=selected)
    drafts = tuple(
        EventWarmupDraft(
            position=index + 1,
            publish_date=first_publish_date + timedelta(days=index),
            days_before_event=selected - index,
            text=_safe_warmup_text(
                title=title,
                description=description,
                days_before_event=selected - index,
                position=index + 1,
                total=selected,
            ),
        )
        for index in range(selected)
    )
    return EventWarmupPlan(
        event_id=event_id,
        timezone_name=str(timezone_name),
        days_until_event=days_until_event,
        requested_days=selected,
        drafts=drafts,
    )


def get_event_warmup_plan(
    *,
    actor: TenantContext,
    event_id: str,
    requested_days: int,
    now: datetime | None = None,
) -> EventWarmupPlan:
    with get_db_ro() as conn:
        event = EventRepository(conn).get(actor=actor, event_id=event_id)
        actor.assert_can_manage_business()
        sessions = EventSessionRepository(conn).list_for_event(
            actor=actor,
            event_id=event.id,
        )
    window = warmup_window_for_sessions(
        sessions,
        timezone_name=event.timezone_name,
        now=now,
    )
    validate_warmup_days(
        requested_days,
        max_warmup_days=window.max_warmup_days,
    )
    return build_event_warmup_plan(
        event_id=event.id,
        title=event.title,
        description=event.description,
        timezone_name=event.timezone_name,
        first_session_starts_at=sessions[0].starts_at,
        requested_days=requested_days,
        now=now,
    )


__all__ = [
    "EventWarmupDraft",
    "EventWarmupPlan",
    "build_event_warmup_plan",
    "get_event_warmup_plan",
]
