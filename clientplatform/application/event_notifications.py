from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from clientplatform.application.event_delivery_targets import (
    EventDeliveryTarget,
    resolve_event_organizational_targets,
)
from clientplatform.domain.email_outbound import EmailPayload
from clientplatform.domain.event_sessions import EventSession
from clientplatform.domain.events import Event, EventRegistration, normalize_utc
from clientplatform.infrastructure.event_repository import EventRepository
from clientplatform.infrastructure.event_session_repository import EventSessionRepository


@dataclass(frozen=True, slots=True)
class EventNotificationPlan:
    queued: int
    skipped_past: int
    enabled: bool
    channels: tuple[str, ...] = ()


def _public_base_url() -> str:
    from config.settings import settings

    value = str(
        getattr(settings, "MESSENGER_PUBLIC_BASE_URL", "") or ""
    ).strip().rstrip("/")
    if not value.startswith("https://"):
        raise RuntimeError("MESSENGER_PUBLIC_BASE_URL must be configured as HTTPS")
    return value


def _event_url(path: str) -> str:
    return f"{_public_base_url()}{path}"


def _session_local_start_label(*, event: Event, session: EventSession) -> str:
    return session.starts_at.astimezone(ZoneInfo(event.timezone_name)).strftime(
        "%d.%m.%Y %H:%M"
    )


def _render(
    *,
    kind: str,
    event: Event,
    registration: EventRegistration,
    session: EventSession | None = None,
    session_count: int = 1,
) -> tuple[str, str]:
    name = registration.name
    if kind == "registration_confirmed":
        when = event.local_start_label()
        join = _event_url(f"/e/join/{registration.token}")
        return (
            f"Вы зарегистрированы: {event.title}",
            f"{name}, регистрация подтверждена.\n\n{event.title}\n{when}\n\n"
            f"Войти в эфир: {join}\n\n"
            "Сохраните эту персональную ссылку: она работает даже при поздней регистрации.",
        )
    if session is None:
        raise ValueError("event session is required for a reminder")

    when = _session_local_start_label(event=event, session=session)
    join = _event_url(f"/e/join/{registration.token}/{session.position}")
    day = f"День {session.position} из {session_count}.\n" if session_count > 1 else ""
    if kind == "24h":
        return (
            f"Скоро: {event.title}",
            f"{name}, напоминаем о мероприятии.\n\n{event.title}\n{day}{when}\n\nВойти: {join}",
        )
    if kind == "3h":
        return (
            f"Через 3 часа: {event.title}",
            f"{name}, {day.lower()}начинаем примерно через 3 часа.\n\nВойти: {join}",
        )
    if kind == "15m":
        return (
            f"Через 15 минут: {event.title}",
            f"{name}, {day.lower()}начинаем примерно через 15 минут.\n\nВойти: {join}",
        )
    raise ValueError("unsupported event notification kind")


def _materialize(
    conn: Any,
    *,
    event: Event,
    registration: EventRegistration,
    target: EventDeliveryTarget,
    kind: str,
    scheduled_at: datetime,
    session: EventSession | None = None,
    session_count: int = 1,
) -> bool:
    subject, body = _render(
        kind=kind,
        event=event,
        registration=registration,
        session=session,
        session_count=session_count,
    )
    if target.platform == "email":
        payload_kind = "mixed"
        payload_ref = EmailPayload(subject=subject, body=body).to_json()
    else:
        payload_kind = "text"
        payload_ref = body
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if kind == "registration_confirmed" or session is None or session.position == 1:
        idempotency_key = (
            f"event:{event.id}:registration:{registration.id}:message:{kind}:v2"
            if target.platform == "email"
            else (
                f"event:{event.id}:registration:{registration.id}:"
                f"message:org:v3:{kind}:{target.platform}"
            )
        )
    else:
        idempotency_key = (
            f"event:{event.id}:registration:{registration.id}:"
            f"message:session:{session.position}:{kind}:v1"
            if target.platform == "email"
            else (
                f"event:{event.id}:registration:{registration.id}:"
                f"message:org:v4:session:{session.position}:{kind}:{target.platform}"
            )
        )
    cursor = conn.execute(
        """
        INSERT INTO provider_dispatch_outbox(
            id,business_id,platform,source_kind,source_id,
            logical_delivery_id,partner_campaign_id,partner_candidate_id,
            sales_followup_id,connection_id,recipient_kind,customer_identity_id,
            external_subject,payload_kind,payload_ref,idempotency_key,status,
            attempts,available_at,locked_at,lock_token,provider_message_id,
            last_error,created_at,updated_at,sent_at,dead_at
        ) VALUES(
            ?,?,?,'event_message',?,NULL,NULL,NULL,NULL,?,?,?,?,?,?,?,'pending',0,?,
            NULL,NULL,NULL,NULL,?,?,NULL,NULL
        )
        ON CONFLICT(business_id,idempotency_key) DO NOTHING
        """,
        (
            str(uuid4()),
            event.business_id,
            target.platform,
            registration.id,
            target.connection_id,
            target.recipient_kind,
            target.customer_identity_id,
            target.external_subject,
            payload_kind,
            payload_ref,
            idempotency_key,
            scheduled_at.isoformat(),
            timestamp,
            timestamp,
        ),
    )
    return int(getattr(cursor, "rowcount", 0) or 0) == 1


def enqueue_event_notifications(
    conn: Any,
    *,
    event: Event,
    registration: EventRegistration,
    now: datetime | None = None,
) -> EventNotificationPlan:
    current = normalize_utc(now or datetime.now(timezone.utc), field_name="now")
    targets = resolve_event_organizational_targets(
        conn, event=event, registration=registration
    )
    if not targets:
        return EventNotificationPlan(
            queued=0, skipped_past=0, enabled=False, channels=()
        )

    sessions = EventSessionRepository(conn).list_for_event_record(event=event)
    queued = 0
    skipped_past = 0
    for target in targets:
        queued += int(
            _materialize(
                conn,
                event=event,
                registration=registration,
                target=target,
                kind="registration_confirmed",
                scheduled_at=current,
            )
        )

    session_count = len(sessions)
    offsets = (
        ("24h", timedelta(hours=24)),
        ("3h", timedelta(hours=3)),
        ("15m", timedelta(minutes=15)),
    )
    for session in sessions:
        for kind, offset in offsets:
            run_at = session.starts_at - offset
            if run_at <= current:
                skipped_past += 1
                continue
            for target in targets:
                queued += int(
                    _materialize(
                        conn,
                        event=event,
                        registration=registration,
                        target=target,
                        kind=kind,
                        scheduled_at=run_at,
                        session=session,
                        session_count=session_count,
                    )
                )
    return EventNotificationPlan(
        queued=queued,
        skipped_past=skipped_past,
        enabled=True,
        channels=tuple(target.platform for target in targets),
    )


def reconcile_future_event_notifications_for_customer(
    conn: Any,
    *,
    business_id: str,
    customer_id: str,
    now: datetime | None = None,
) -> int:
    """Idempotently materialize still-relevant event notices after channel linking."""

    current = normalize_utc(now or datetime.now(timezone.utc), field_name="now")
    rows = conn.execute(
        """
        SELECT r.token,e.public_slug
        FROM clientplatform_event_registrations r
        JOIN clientplatform_events e
          ON e.id=r.event_id AND e.business_id=r.business_id
        WHERE r.business_id=? AND r.customer_id=? AND r.status='registered'
          AND e.status='published'
          AND (
              e.starts_at>?
              OR EXISTS(
                  SELECT 1 FROM clientplatform_event_sessions s
                  WHERE s.event_id=e.id AND s.business_id=e.business_id AND s.starts_at>?
              )
          )
        ORDER BY e.starts_at,r.id
        LIMIT 100
        """,
        (
            business_id,
            customer_id,
            current.isoformat(),
            current.isoformat(),
        ),
    ).fetchall()
    repository = EventRepository(conn)
    queued = 0
    for row in rows:
        token = str(row["token"] if hasattr(row, "keys") else row[0])
        public_slug = str(row["public_slug"] if hasattr(row, "keys") else row[1])
        registration = repository.get_registration_by_token(token=token)
        event = repository.get_public_owner_event(public_slug=public_slug)
        queued += enqueue_event_notifications(
            conn, event=event, registration=registration, now=current
        ).queued
    return queued


__all__ = [
    "EventNotificationPlan",
    "enqueue_event_notifications",
    "reconcile_future_event_notifications_for_customer",
]
