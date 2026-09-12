from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.email_outbound import EmailPayload
from clientplatform.domain.events import Event, EventRegistration, normalize_utc


@dataclass(frozen=True, slots=True)
class EventNotificationPlan:
    queued: int
    skipped_past: int
    enabled: bool


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


def _render(
    *,
    kind: str,
    event: Event,
    registration: EventRegistration,
) -> tuple[str, str]:
    when = event.local_start_label()
    join = _event_url(f"/e/join/{registration.token}")
    name = registration.name

    if kind == "registration_confirmed":
        return (
            f"Вы зарегистрированы: {event.title}",
            f"{name}, регистрация подтверждена.\n\n{event.title}\n{when}\n\n"
            f"Войти в эфир: {join}\n\n"
            "Сохраните эту персональную ссылку: она работает даже при поздней регистрации.",
        )
    if kind == "24h":
        return (
            f"Скоро: {event.title}",
            f"{name}, напоминаем о мероприятии.\n\n{event.title}\n{when}\n\nВойти: {join}",
        )
    if kind == "3h":
        return (
            f"Через 3 часа: {event.title}",
            f"{name}, начинаем примерно через 3 часа.\n\nВойти: {join}",
        )
    if kind == "15m":
        return (
            f"Через 15 минут: {event.title}",
            f"{name}, начинаем примерно через 15 минут.\n\nВойти: {join}",
        )
    raise ValueError("unsupported event notification kind")


def _connection_is_active(conn: Any, *, event: Event) -> bool:
    if event.notification_connection_id is None:
        return False
    row = conn.execute(
        """
        SELECT 1 FROM connections
        WHERE id=? AND business_id=? AND platform='email'
          AND connection_type='email_smtp' AND status='active'
        LIMIT 1
        """,
        (event.notification_connection_id, event.business_id),
    ).fetchone()
    return row is not None


def _materialize(
    conn: Any,
    *,
    event: Event,
    registration: EventRegistration,
    kind: str,
    scheduled_at: datetime,
) -> bool:
    if event.notification_connection_id is None:
        return False
    subject, body = _render(kind=kind, event=event, registration=registration)
    payload_ref = EmailPayload(subject=subject, body=body).to_json()
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    idempotency_key = f"event:{event.id}:registration:{registration.id}:message:{kind}:v2"
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
            ?,?,'email','event_message',?,NULL,NULL,NULL,NULL,?,
            'external_subject',NULL,?,'mixed',?,?,'pending',0,?,
            NULL,NULL,NULL,NULL,?,?,NULL,NULL
        )
        ON CONFLICT(business_id,idempotency_key) DO NOTHING
        """,
        (
            str(uuid4()),
            event.business_id,
            registration.id,
            event.notification_connection_id,
            registration.email,
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
    if not _connection_is_active(conn, event=event):
        return EventNotificationPlan(queued=0, skipped_past=0, enabled=False)

    schedule: list[tuple[str, datetime]] = [
        ("registration_confirmed", current),
        ("24h", event.starts_at - timedelta(hours=24)),
        ("3h", event.starts_at - timedelta(hours=3)),
        ("15m", event.starts_at - timedelta(minutes=15)),
    ]
    queued = 0
    skipped_past = 0
    for kind, run_at in schedule:
        if kind != "registration_confirmed" and run_at <= current:
            skipped_past += 1
            continue
        queued += int(
            _materialize(
                conn,
                event=event,
                registration=registration,
                kind=kind,
                scheduled_at=run_at,
            )
        )
    return EventNotificationPlan(queued=queued, skipped_past=skipped_past, enabled=True)


__all__ = ["EventNotificationPlan", "enqueue_event_notifications"]
