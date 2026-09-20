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
from clientplatform.domain.tenancy import TenantContext
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
    schedule_revision: str | None = None,
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
    if schedule_revision:
        revision = str(schedule_revision).strip().lower()
        if not revision or any(ch not in "0123456789abcdef" for ch in revision):
            raise ValueError("schedule revision must be lowercase hexadecimal")
        idempotency_key = f"{idempotency_key}:schedule:{revision}"

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


def reschedule_event_notifications_in_transaction(
    conn: Any,
    *,
    actor: TenantContext,
    event_id: str,
    now: datetime | None = None,
) -> int:
    """Replace only unsent session reminders after an owner changes event timing."""

    current = normalize_utc(now or datetime.now(timezone.utc), field_name="now")
    repository = EventRepository(conn)
    event = repository.get(actor=actor, event_id=event_id)
    sessions = EventSessionRepository(conn).list_for_event_record(event=event)
    revision = uuid4().hex[:16]
    queued = 0
    offsets = (
        ("24h", timedelta(hours=24)),
        ("3h", timedelta(hours=3)),
        ("15m", timedelta(minutes=15)),
    )
    cursor_registered_at: str | None = None
    cursor_id: str | None = None
    while True:
        registrations = repository.list_active_registrations_page(
            actor=actor,
            event_id=event.id,
            limit=500,
            after_registered_at=cursor_registered_at,
            after_id=cursor_id,
        )
        if not registrations:
            break
        for registration in registrations:
            confirmation_rows = conn.execute(
                """
                SELECT DISTINCT platform
                FROM provider_dispatch_outbox
                WHERE business_id=? AND source_kind='event_message' AND source_id=?
                  AND (
                      status IN ('pending','retry')
                      OR (
                          status='sending'
                          AND COALESCE(last_error,'')
                              NOT LIKE '%provider_call_started_non_idempotent%'
                      )
                  )
                  AND (
                      idempotency_key LIKE '%:message:registration_confirmed:%'
                      OR idempotency_key LIKE '%:message:org:v3:registration_confirmed:%'
                  )
                """,
                (event.business_id, registration.id),
            ).fetchall()
            confirmation_platforms = {
                str(row["platform"] if hasattr(row, "keys") else row[0])
                for row in confirmation_rows
            }
            conn.execute(
                """
                UPDATE provider_dispatch_outbox
                SET status='cancelled',updated_at=?,locked_at=NULL,lock_token=NULL,
                    last_error='event_schedule_changed'
                WHERE business_id=? AND source_kind='event_message' AND source_id=?
                  AND (
                      status IN ('pending','retry')
                      OR (
                          status='sending'
                          AND COALESCE(last_error,'')
                              NOT LIKE '%provider_call_started_non_idempotent%'
                      )
                  )
                  AND (
                      idempotency_key LIKE '%:message:registration_confirmed:%'
                      OR idempotency_key LIKE '%:message:org:v3:registration_confirmed:%'
                      OR idempotency_key LIKE '%:message:24h:%'
                      OR idempotency_key LIKE '%:message:3h:%'
                      OR idempotency_key LIKE '%:message:15m:%'
                      OR idempotency_key LIKE '%:message:session:%'
                      OR idempotency_key LIKE '%:message:org:v3:24h:%'
                      OR idempotency_key LIKE '%:message:org:v3:3h:%'
                      OR idempotency_key LIKE '%:message:org:v3:15m:%'
                      OR idempotency_key LIKE '%:message:org:v4:session:%'
                  )
                """,
                (current.isoformat(), event.business_id, registration.id),
            )
            targets = resolve_event_organizational_targets(
                conn,
                event=event,
                registration=registration,
            )
            for target in targets:
                if target.platform in confirmation_platforms:
                    queued += int(
                        _materialize(
                            conn,
                            event=event,
                            registration=registration,
                            target=target,
                            kind="registration_confirmed",
                            scheduled_at=current,
                            schedule_revision=revision,
                        )
                    )
            session_count = len(sessions)
            for session in sessions:
                for kind, offset in offsets:
                    run_at = session.starts_at - offset
                    if run_at <= current:
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
                                schedule_revision=revision,
                            )
                        )
        last = registrations[-1]
        cursor_registered_at = last.registered_at.isoformat()
        cursor_id = last.id
        if len(registrations) < 500:
            break
    return queued


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
    "reschedule_event_notifications_in_transaction",
]
