from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from clientplatform.application.events import (
    create_event_in_transaction,
    publish_event_in_transaction,
)
from clientplatform.domain.events import normalize_provider_key, validate_external_https_url
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db import atomic_db


@dataclass(frozen=True, slots=True)
class OnlineEventCreateRequest:
    title: str
    starts_at: datetime
    timezone_name: str
    join_url: str
    description: str = ""
    ends_at: datetime | None = None
    offer_url: str | None = None
    kind: str = "webinar"
    provider_key: str | None = None
    provider_label: str | None = None
    enable_email_notifications: bool = True
    notification_connection_id: str | None = None


@dataclass(frozen=True, slots=True)
class OnlineEventCreated:
    event_id: str
    public_slug: str
    provider_key: str
    email_notifications_enabled: bool

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


def create_and_publish_online_event(
    *,
    actor: TenantContext,
    request: OnlineEventCreateRequest,
) -> OnlineEventCreated:
    join_url = validate_external_https_url(request.join_url, field_name="join_url")
    provider_key = normalize_provider_key(request.provider_key, join_url=join_url)
    with atomic_db() as conn:
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
        )


__all__ = [
    "OnlineEventCreateRequest",
    "OnlineEventCreated",
    "create_and_publish_online_event",
]
