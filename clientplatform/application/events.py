from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clientplatform.application.event_customer_projection import (
    attach_event_registration_to_customer,
)
from clientplatform.application.event_notifications import (
    EventNotificationPlan,
    enqueue_event_notifications,
)
from clientplatform.domain.events import Event, EventRegistration, EventUnavailable
from clientplatform.domain.events import (
    new_public_slug,
    normalize_event_kind,
    normalize_provider_key,
    normalize_provider_label,
    normalize_timezone_name,
    normalize_utc,
    validate_external_https_url,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.event_repository import (
    EventRepository,
    EventStateConflict,
)
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db import get_db, get_db_ro
from services.db.core import ambient_savepoint


log = logging.getLogger(__name__)
CURRENT_EVENT_CONSENT_VERSION = "2026-09-online-event-v2"


@dataclass(frozen=True, slots=True)
class PublicRegistrationResult:
    registration: EventRegistration
    created: bool
    customer_id: str | None
    notifications: EventNotificationPlan


def create_event_in_transaction(
    conn: Any,
    *,
    actor: TenantContext,
    title: str,
    starts_at: datetime,
    timezone_name: str,
    join_url: str,
    provider_key: str | None = None,
    provider_label: str | None = None,
    kind: str = "webinar",
    offer_url: str | None = None,
    description: str = "",
    ends_at: datetime | None = None,
    notification_connection_id: str | None = None,
    now: datetime | None = None,
) -> Event:
    repository = EventRepository(conn)
    current = TenancyRepository(conn).resolve_context(
        user_id=actor.user_id,
        business_id=actor.business_id,
    )
    current.assert_can_manage_business()
    timestamp = normalize_utc(now or datetime.now(timezone.utc), field_name="now")
    start = normalize_utc(starts_at, field_name="starts_at")
    if start <= timestamp:
        raise ValueError("event must start in the future")
    end = None if ends_at is None else normalize_utc(ends_at, field_name="ends_at")
    join = validate_external_https_url(join_url, field_name="join_url")
    event = Event(
        id=str(uuid4()),
        business_id=current.business_id,
        created_by_member_id=current.membership_id,
        kind=normalize_event_kind(kind),
        status="draft",
        title=title,
        description=description,
        starts_at=start,
        ends_at=end,
        timezone_name=normalize_timezone_name(timezone_name),
        provider_key=normalize_provider_key(provider_key, join_url=join),
        provider_label=normalize_provider_label(provider_label),
        join_url=join,
        offer_url=(
            None
            if not offer_url
            else validate_external_https_url(offer_url, field_name="offer_url")
        ),
        public_slug=new_public_slug(),
        consent_version=CURRENT_EVENT_CONSENT_VERSION,
        notification_connection_id=notification_connection_id,
        created_at=timestamp,
        updated_at=timestamp,
    )
    return repository.insert(actor=current, event=event)


def create_event(**kwargs: Any) -> Event:
    with get_db() as conn:
        return create_event_in_transaction(conn, **kwargs)


def publish_event_in_transaction(
    conn: Any,
    *,
    actor: TenantContext,
    event_id: str,
    now: datetime | None = None,
) -> Event:
    repository = EventRepository(conn)
    event = repository.get(actor=actor, event_id=event_id)
    timestamp = normalize_utc(now or datetime.now(timezone.utc), field_name="now")
    if event.starts_at <= timestamp:
        raise EventUnavailable("cannot publish an event that has already started")
    return repository.set_status(
        actor=actor,
        event_id=event.id,
        expected_status="draft",
        status="published",
        now=timestamp,
    )


def publish_event(*, actor: TenantContext, event_id: str, now: datetime | None = None) -> Event:
    with get_db() as conn:
        return publish_event_in_transaction(
            conn,
            actor=actor,
            event_id=event_id,
            now=now,
        )


def cancel_event(*, actor: TenantContext, event_id: str) -> Event:
    with get_db() as conn:
        repository = EventRepository(conn)
        event = repository.get(actor=actor, event_id=event_id)
        if event.status not in {"draft", "published"}:
            raise EventUnavailable("event cannot be cancelled from its current state")
        cancelled = repository.set_status(
            actor=actor,
            event_id=event.id,
            expected_status=event.status,
            status="cancelled",
        )
        conn.execute(
            """
            UPDATE provider_dispatch_outbox
            SET status='cancelled',updated_at=?,locked_at=NULL,lock_token=NULL,
                last_error='event_cancelled'
            WHERE business_id=? AND source_kind='event_message'
              AND status IN ('pending','retry')
              AND source_id IN (
                  SELECT id FROM clientplatform_event_registrations
                  WHERE business_id=? AND event_id=?
              )
            """,
            (
                datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                cancelled.business_id,
                cancelled.business_id,
                cancelled.id,
            ),
        )
        return cancelled


def register_public_attendee_in_transaction(
    conn: Any,
    *,
    public_slug: str,
    name: str,
    email: str,
    phone: str | None = None,
    source: str | None = None,
    campaign_ref: str | None = None,
    consent: bool,
    now: datetime | None = None,
) -> PublicRegistrationResult:
    if consent is not True:
        raise ValueError("consent is required")
    repository = EventRepository(conn)
    event = repository.get_public_owner_event(public_slug=public_slug)
    timestamp = normalize_utc(now or datetime.now(timezone.utc), field_name="now")
    if not event.accepts_registrations(now=timestamp):
        raise EventUnavailable("registration is closed")
    try:
        registration, created = repository.register_public(
            event=event,
            name=name,
            email=email,
            phone=phone,
            source=source,
            campaign_ref=campaign_ref,
            consent_version=event.consent_version,
            now=timestamp,
        )
    except EventStateConflict as exc:
        raise EventUnavailable("registration is closed") from exc

    customer_id = registration.customer_id
    notifications = EventNotificationPlan(
        queued=0,
        skipped_past=0,
        enabled=event.notification_connection_id is not None,
    )
    if customer_id is None:
        try:
            with ambient_savepoint(conn):
                customer_id = attach_event_registration_to_customer(
                    conn,
                    event=event,
                    registration=registration,
                )
            if customer_id is not None:
                registration = repository.get_registration_by_token(token=registration.token)
        except Exception:  # validator: allow-wide-except
            log.exception(
                "Event CRM projection failed; registration remains retryable",
                extra={"business_id": event.business_id, "event_id": event.id},
            )
    try:
        with ambient_savepoint(conn):
            notifications = enqueue_event_notifications(
                conn,
                event=event,
                registration=registration,
                now=timestamp,
            )
    except Exception:  # validator: allow-wide-except
        log.exception(
            "Event notification enqueue failed; registration remains retryable",
            extra={"business_id": event.business_id, "event_id": event.id},
        )
    return PublicRegistrationResult(
        registration=registration,
        created=created,
        customer_id=customer_id,
        notifications=notifications,
    )


def register_public_attendee(**kwargs: Any) -> PublicRegistrationResult:
    with get_db() as conn:
        return register_public_attendee_in_transaction(conn, **kwargs)


def get_public_event(*, public_slug: str):
    with get_db_ro() as conn:
        return EventRepository(conn).get_public(public_slug=public_slug)


__all__ = [
    "CURRENT_EVENT_CONSENT_VERSION",
    "EventUnavailable",
    "PublicRegistrationResult",
    "cancel_event",
    "create_event",
    "create_event_in_transaction",
    "get_public_event",
    "publish_event",
    "publish_event_in_transaction",
    "register_public_attendee",
    "register_public_attendee_in_transaction",
]
