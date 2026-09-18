from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from clientplatform.application.event_sessions import (
    EventSessionSpec,
    configure_event_sessions_in_transaction,
)
from clientplatform.application.events import (
    create_event_in_transaction,
    publish_event_in_transaction,
)
from clientplatform.domain.event_sessions import EventSession
from clientplatform.domain.events import normalize_provider_key, validate_external_https_url
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.event_repository import EventRepository, EventStateConflict
from clientplatform.infrastructure.event_session_repository import EventSessionRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db import atomic_db


@dataclass(frozen=True, slots=True)
class OnlineEventCreateRequest:
    title: str
    starts_at: datetime
    timezone_name: str
    join_url: str | None = None
    description: str = ""
    ends_at: datetime | None = None
    offer_url: str | None = None
    kind: str = "webinar"
    provider_key: str | None = None
    provider_label: str | None = None
    enable_email_notifications: bool = True
    notification_connection_id: str | None = None


@dataclass(frozen=True, slots=True)
class OnlineEventSessionCreateRequest:
    starts_at: datetime
    ends_at: datetime | None = None
    join_url: str | None = None
    provider_key: str | None = None
    provider_label: str | None = None


@dataclass(frozen=True, slots=True)
class MultiSessionOnlineEventCreateRequest:
    title: str
    timezone_name: str
    sessions: tuple[OnlineEventSessionCreateRequest, ...]
    description: str = ""
    offer_url: str | None = None
    kind: str = "webinar"
    enable_email_notifications: bool = True
    notification_connection_id: str | None = None


@dataclass(frozen=True, slots=True)
class OnlineEventDraft:
    event_id: str
    public_slug: str
    provider_key: str
    email_notifications_enabled: bool
    join_ready: bool


@dataclass(frozen=True, slots=True)
class OnlineEventCreated:
    event_id: str
    public_slug: str
    provider_key: str
    email_notifications_enabled: bool
    join_ready: bool

    def registration_url(self, public_base_url: str) -> str:
        base = validate_external_https_url(public_base_url, field_name="public_base_url").rstrip("/")
        return f"{base}/e/{self.public_slug}"


def _resolve_notification_connection(
    conn: Any,
    *,
    actor: TenantContext,
    requested_connection_id: str | None,
    enabled: bool,
) -> str | None:
    if not enabled:
        return None
    current = TenancyRepository(conn).resolve_context(
        user_id=actor.user_id,
        business_id=actor.business_id,
    )
    current.assert_can_manage_business()
    if requested_connection_id:
        rows = conn.execute(
            """
            SELECT id FROM connections
            WHERE id=? AND business_id=? AND platform='email'
              AND connection_type='email_smtp' AND status='active'
            LIMIT 2
            """,
            (str(requested_connection_id), current.business_id),
        ).fetchall()
        if len(rows) != 1:
            raise ValueError("selected SMTP notification connection is not active")
        row = rows[0]
        return str(row["id"] if hasattr(row, "keys") else row[0])
    rows = conn.execute(
        """
        SELECT id FROM connections
        WHERE business_id=? AND platform='email'
          AND connection_type='email_smtp' AND status='active'
        ORDER BY created_at,id
        LIMIT 2
        """,
        (current.business_id,),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise ValueError("multiple active SMTP connections require explicit selection")
    row = rows[0]
    return str(row["id"] if hasattr(row, "keys") else row[0])


def create_and_publish_online_event_in_transaction(
    conn: Any,
    *,
    actor: TenantContext,
    request: OnlineEventCreateRequest,
) -> OnlineEventCreated:
    join_url = (
        None
        if not str(request.join_url or "").strip()
        else validate_external_https_url(request.join_url, field_name="join_url")
    )
    provider_key = normalize_provider_key(request.provider_key, join_url=join_url)
    notification_connection_id = _resolve_notification_connection(
        conn,
        actor=actor,
        requested_connection_id=request.notification_connection_id,
        enabled=bool(request.enable_email_notifications),
    )
    event = create_event_in_transaction(
        conn,
        actor=actor,
        title=request.title,
        starts_at=request.starts_at,
        timezone_name=request.timezone_name,
        join_url=join_url,
        description=request.description,
        ends_at=request.ends_at,
        offer_url=request.offer_url,
        kind=request.kind,
        provider_key=provider_key,
        provider_label=request.provider_label,
        notification_connection_id=notification_connection_id,
    )
    event = publish_event_in_transaction(
        conn,
        actor=actor,
        event_id=event.id,
    )
    return OnlineEventCreated(
        event_id=event.id,
        public_slug=event.public_slug,
        provider_key=event.provider_key,
        email_notifications_enabled=event.notification_connection_id is not None,
        join_ready=event.join_is_ready,
    )


def create_multisession_online_event_draft_in_transaction(
    conn: Any,
    *,
    actor: TenantContext,
    request: MultiSessionOnlineEventCreateRequest,
) -> OnlineEventDraft:
    sessions = tuple(request.sessions)
    if not sessions:
        raise ValueError("event must have at least one session")
    if len(sessions) > 31:
        raise ValueError("event has too many sessions")

    first = sessions[0]
    first_join_url = (
        None
        if not str(first.join_url or "").strip()
        else validate_external_https_url(first.join_url, field_name="join_url")
    )
    first_provider_key = normalize_provider_key(first.provider_key, join_url=first_join_url)
    notification_connection_id = _resolve_notification_connection(
        conn,
        actor=actor,
        requested_connection_id=request.notification_connection_id,
        enabled=bool(request.enable_email_notifications),
    )
    event = create_event_in_transaction(
        conn,
        actor=actor,
        title=request.title,
        starts_at=first.starts_at,
        timezone_name=request.timezone_name,
        join_url=first_join_url,
        description=request.description,
        ends_at=first.ends_at,
        offer_url=request.offer_url,
        kind=request.kind,
        provider_key=first_provider_key,
        provider_label=first.provider_label,
        notification_connection_id=notification_connection_id,
    )
    configured = configure_event_sessions_in_transaction(
        conn,
        actor=actor,
        event_id=event.id,
        sessions=tuple(
            EventSessionSpec(
                starts_at=session.starts_at,
                ends_at=session.ends_at,
                join_url=session.join_url,
                provider_key=session.provider_key,
                provider_label=session.provider_label,
            )
            for session in sessions
        ),
    )
    return OnlineEventDraft(
        event_id=event.id,
        public_slug=event.public_slug,
        provider_key=event.provider_key,
        email_notifications_enabled=event.notification_connection_id is not None,
        join_ready=all(session.join_is_ready for session in configured),
    )


def append_multisession_online_event_draft_session_in_transaction(
    conn: Any,
    *,
    actor: TenantContext,
    event_id: str,
    session: OnlineEventSessionCreateRequest,
) -> tuple[EventSession, ...]:
    event = EventRepository(conn).get(actor=actor, event_id=event_id)
    if event.status != "draft":
        raise EventStateConflict("only draft events can accept wizard sessions")
    existing = EventSessionRepository(conn).list_for_event(actor=actor, event_id=event.id)
    specs = tuple(
        EventSessionSpec(
            starts_at=item.starts_at,
            ends_at=item.ends_at,
            join_url=item.join_url,
            provider_key=item.provider_key,
            provider_label=item.provider_label,
        )
        for item in existing
    ) + (
        EventSessionSpec(
            starts_at=session.starts_at,
            ends_at=session.ends_at,
            join_url=session.join_url,
            provider_key=session.provider_key,
            provider_label=session.provider_label,
        ),
    )
    return configure_event_sessions_in_transaction(
        conn,
        actor=actor,
        event_id=event.id,
        sessions=specs,
    )


def publish_multisession_online_event_draft_in_transaction(
    conn: Any,
    *,
    actor: TenantContext,
    event_id: str,
) -> OnlineEventCreated:
    event = EventRepository(conn).get(actor=actor, event_id=event_id)
    if event.status != "draft":
        raise EventStateConflict("event draft is no longer publishable")
    sessions = EventSessionRepository(conn).list_for_event(actor=actor, event_id=event.id)
    event = publish_event_in_transaction(conn, actor=actor, event_id=event.id)
    return OnlineEventCreated(
        event_id=event.id,
        public_slug=event.public_slug,
        provider_key=event.provider_key,
        email_notifications_enabled=event.notification_connection_id is not None,
        join_ready=all(session.join_is_ready for session in sessions),
    )


def create_and_publish_multisession_online_event_in_transaction(
    conn: Any,
    *,
    actor: TenantContext,
    request: MultiSessionOnlineEventCreateRequest,
) -> OnlineEventCreated:
    draft = create_multisession_online_event_draft_in_transaction(
        conn,
        actor=actor,
        request=request,
    )
    return publish_multisession_online_event_draft_in_transaction(
        conn,
        actor=actor,
        event_id=draft.event_id,
    )


def create_and_publish_online_event(
    *,
    actor: TenantContext,
    request: OnlineEventCreateRequest,
) -> OnlineEventCreated:
    with atomic_db() as conn:
        return create_and_publish_online_event_in_transaction(
            conn, actor=actor, request=request
        )


def create_and_publish_multisession_online_event(
    *,
    actor: TenantContext,
    request: MultiSessionOnlineEventCreateRequest,
) -> OnlineEventCreated:
    with atomic_db() as conn:
        return create_and_publish_multisession_online_event_in_transaction(
            conn, actor=actor, request=request
        )


def create_multisession_online_event_draft(
    *,
    actor: TenantContext,
    request: MultiSessionOnlineEventCreateRequest,
) -> OnlineEventDraft:
    with atomic_db() as conn:
        return create_multisession_online_event_draft_in_transaction(
            conn, actor=actor, request=request
        )


def append_multisession_online_event_draft_session(
    *,
    actor: TenantContext,
    event_id: str,
    session: OnlineEventSessionCreateRequest,
) -> tuple[EventSession, ...]:
    with atomic_db() as conn:
        return append_multisession_online_event_draft_session_in_transaction(
            conn,
            actor=actor,
            event_id=event_id,
            session=session,
        )


def publish_multisession_online_event_draft(
    *,
    actor: TenantContext,
    event_id: str,
) -> OnlineEventCreated:
    with atomic_db() as conn:
        return publish_multisession_online_event_draft_in_transaction(
            conn,
            actor=actor,
            event_id=event_id,
        )


__all__ = [
    "MultiSessionOnlineEventCreateRequest",
    "OnlineEventDraft",
    "OnlineEventCreateRequest",
    "OnlineEventCreated",
    "OnlineEventSessionCreateRequest",
    "append_multisession_online_event_draft_session",
    "append_multisession_online_event_draft_session_in_transaction",
    "create_and_publish_multisession_online_event",
    "create_multisession_online_event_draft",
    "create_multisession_online_event_draft_in_transaction",
    "create_and_publish_multisession_online_event_in_transaction",
    "create_and_publish_online_event",
    "create_and_publish_online_event_in_transaction",
    "publish_multisession_online_event_draft",
    "publish_multisession_online_event_draft_in_transaction",
]
