from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.events import (
    Event,
    EventRegistration,
    PublicEvent,
    new_registration_token,
    normalize_email,
    normalize_phone,
    normalize_utc,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


class EventNotFound(LookupError):
    pass


class EventStateConflict(RuntimeError):
    pass


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def _optional(row: Any, key: str, position: int) -> Any | None:
    value = _value(row, key, position)
    return None if value is None else value


def _event_from_row(row: Any) -> Event:
    return Event(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        created_by_member_id=str(_value(row, "created_by_member_id", 2)),
        kind=str(_value(row, "kind", 3)),
        status=str(_value(row, "status", 4)),
        title=str(_value(row, "title", 5)),
        description=str(_value(row, "description", 6) or ""),
        starts_at=str(_value(row, "starts_at", 7)),
        ends_at=_optional(row, "ends_at", 8),
        timezone_name=str(_value(row, "timezone_name", 9)),
        provider_key=str(_value(row, "provider_key", 10)),
        provider_label=_optional(row, "provider_label", 11),
        join_url=str(_value(row, "join_url", 12)),
        offer_url=_optional(row, "offer_url", 13),
        public_slug=str(_value(row, "public_slug", 14)),
        consent_version=str(_value(row, "consent_version", 15)),
        notification_connection_id=_optional(row, "notification_connection_id", 16),
        created_at=str(_value(row, "created_at", 17)),
        updated_at=str(_value(row, "updated_at", 18)),
    )


_EVENT_COLUMNS = """
id, business_id, created_by_member_id, kind, status, title, description,
starts_at, ends_at, timezone_name, provider_key, provider_label, join_url,
offer_url, public_slug, consent_version, notification_connection_id,
created_at, updated_at
""".strip()


def _registration_from_row(row: Any) -> EventRegistration:
    return EventRegistration(
        id=str(_value(row, "id", 0)),
        event_id=str(_value(row, "event_id", 1)),
        business_id=str(_value(row, "business_id", 2)),
        customer_id=_optional(row, "customer_id", 3),
        status=str(_value(row, "status", 4)),
        name=str(_value(row, "name", 5)),
        email=str(_value(row, "email", 6)),
        phone=_optional(row, "phone", 7),
        source=_optional(row, "source", 8),
        campaign_ref=_optional(row, "campaign_ref", 9),
        token=str(_value(row, "token", 10)),
        consent_version=str(_value(row, "consent_version", 11)),
        consented_at=str(_value(row, "consented_at", 12)),
        registered_at=str(_value(row, "registered_at", 13)),
        first_join_click_at=_optional(row, "first_join_click_at", 14),
        attendance_confirmed_at=_optional(row, "attendance_confirmed_at", 15),
        attendance_source=_optional(row, "attendance_source", 16),
        offer_clicked_at=_optional(row, "offer_clicked_at", 17),
    )


_REGISTRATION_COLUMNS = """
id, event_id, business_id, customer_id, status, name, email, phone,
source, campaign_ref, token, consent_version, consented_at, registered_at,
first_join_click_at, attendance_confirmed_at, attendance_source, offer_clicked_at
""".strip()


def _identity_hash(email: str) -> str:
    # Event id already scopes uniqueness. E-mail is the stable public identity;
    # changing/omitting a phone number must not create a duplicate registration.
    return hashlib.sha256(normalize_email(email).encode("utf-8")).hexdigest()


class EventRepository:
    """Tenant-authorized owner operations plus narrowly scoped public capabilities."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def _actor(self, actor: TenantContext, *, manage: bool) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        if manage:
            current.assert_can_manage_business()
        else:
            current.assert_can_view_customer_records()
        return current

    def insert(self, *, actor: TenantContext, event: Event) -> Event:
        current = self._actor(actor, manage=True)
        current.assert_business(event.business_id)
        if event.created_by_member_id != current.membership_id:
            raise ValueError("event creator must match the active membership")
        self._conn.execute(
            """
            INSERT INTO clientplatform_events(
                id,business_id,created_by_member_id,kind,status,title,description,
                starts_at,ends_at,timezone_name,provider_key,provider_label,
                join_url,offer_url,public_slug,consent_version,
                notification_connection_id,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event.id,
                event.business_id,
                event.created_by_member_id,
                event.kind,
                event.status,
                event.title,
                event.description,
                event.starts_at.isoformat(),
                None if event.ends_at is None else event.ends_at.isoformat(),
                event.timezone_name,
                event.provider_key,
                event.provider_label,
                event.join_url,
                event.offer_url,
                event.public_slug,
                event.consent_version,
                event.notification_connection_id,
                event.created_at.isoformat(),
                event.updated_at.isoformat(),
            ),
        )
        return self.get(actor=current, event_id=event.id)

    def get(self, *, actor: TenantContext, event_id: str) -> Event:
        current = self._actor(actor, manage=False)
        normalized = normalize_uuid(event_id, field_name="event_id")
        row = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM clientplatform_events "
            "WHERE id=? AND business_id=? LIMIT 1",  # nosec B608 - static columns
            (normalized, current.business_id),
        ).fetchone()
        if row is None:
            raise EventNotFound("event was not found in the active business")
        return _event_from_row(row)

    def list(self, *, actor: TenantContext, limit: int = 100) -> list[Event]:
        current = self._actor(actor, manage=False)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be an integer between 1 and 500")
        rows = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM clientplatform_events "
            "WHERE business_id=? ORDER BY starts_at DESC,id DESC LIMIT ?",  # nosec B608
            (current.business_id, limit),
        ).fetchall()
        return [_event_from_row(row) for row in rows]

    def set_status(
        self,
        *,
        actor: TenantContext,
        event_id: str,
        expected_status: str,
        status: str,
        now: datetime | None = None,
    ) -> Event:
        current = self._actor(actor, manage=True)
        normalized = normalize_uuid(event_id, field_name="event_id")
        timestamp = normalize_utc(now or datetime.now(timezone.utc), field_name="now").isoformat()
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_events
            SET status=?,updated_at=?
            WHERE id=? AND business_id=? AND status=?
            """,
            (status, timestamp, normalized, current.business_id, expected_status),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            existing = self._conn.execute(
                "SELECT 1 FROM clientplatform_events WHERE id=? AND business_id=? LIMIT 1",
                (normalized, current.business_id),
            ).fetchone()
            if existing is None:
                raise EventNotFound("event was not found in the active business")
            raise EventStateConflict("event status changed concurrently")
        return self.get(actor=current, event_id=normalized)

    def get_public(self, *, public_slug: str) -> PublicEvent:
        slug = str(public_slug or "").strip()
        row = self._conn.execute(
            """
            SELECT public_slug,kind,title,description,starts_at,ends_at,
                   timezone_name,provider_key,provider_label
            FROM clientplatform_events
            WHERE public_slug=? AND status='published'
            LIMIT 1
            """,
            (slug,),
        ).fetchone()
        if row is None:
            raise EventNotFound("public event was not found")
        return PublicEvent(
            public_slug=str(_value(row, "public_slug", 0)),
            kind=str(_value(row, "kind", 1)),
            title=str(_value(row, "title", 2)),
            description=str(_value(row, "description", 3) or ""),
            starts_at=normalize_utc(str(_value(row, "starts_at", 4)), field_name="starts_at"),
            ends_at=(
                None
                if _optional(row, "ends_at", 5) is None
                else normalize_utc(str(_optional(row, "ends_at", 5)), field_name="ends_at")
            ),
            timezone_name=str(_value(row, "timezone_name", 6)),
            provider_key=str(_value(row, "provider_key", 7)),
            provider_label=_optional(row, "provider_label", 8),
        )

    def get_public_owner_event(self, *, public_slug: str) -> Event:
        """Resolve internal event fields from one opaque public slug.

        This method is for server-side public handlers only. It intentionally is
        not returned by the landing-page read model, which never exposes join_url.
        """
        slug = str(public_slug or "").strip()
        row = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM clientplatform_events "
            "WHERE public_slug=? AND status='published' LIMIT 1",  # nosec B608
            (slug,),
        ).fetchone()
        if row is None:
            raise EventNotFound("public event was not found")
        return _event_from_row(row)

    def register_public(
        self,
        *,
        event: Event,
        name: str,
        email: str,
        phone: str | None,
        source: str | None,
        campaign_ref: str | None,
        consent_version: str,
        now: datetime | None = None,
    ) -> tuple[EventRegistration, bool]:
        timestamp = normalize_utc(now or datetime.now(timezone.utc), field_name="now")
        if not event.accepts_registrations(now=timestamp):
            raise EventStateConflict("registration is closed")
        normalized_email = normalize_email(email)
        normalized_phone = normalize_phone(phone)
        identity_hash = _identity_hash(normalized_email)
        registration_id = str(uuid4())
        token = new_registration_token()
        cursor = self._conn.execute(
            """
            INSERT INTO clientplatform_event_registrations(
                id,event_id,business_id,customer_id,status,name,email,phone,
                identity_hash,source,campaign_ref,token,consent_version,
                consented_at,registered_at,first_join_click_at,
                attendance_confirmed_at,attendance_source,offer_clicked_at
            ) VALUES(?,?,?,NULL,'registered',?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL)
            ON CONFLICT(event_id,identity_hash) DO NOTHING
            """,
            (
                registration_id,
                event.id,
                event.business_id,
                str(name or "").strip(),
                normalized_email,
                normalized_phone,
                identity_hash,
                None if not source else str(source).strip()[:200],
                None if not campaign_ref else str(campaign_ref).strip()[:200],
                token,
                consent_version,
                timestamp.isoformat(),
                timestamp.isoformat(),
            ),
        )
        created = int(getattr(cursor, "rowcount", 0) or 0) == 1
        row = self._conn.execute(
            f"SELECT {_REGISTRATION_COLUMNS} FROM clientplatform_event_registrations "
            "WHERE event_id=? AND business_id=? AND identity_hash=? LIMIT 1",  # nosec B608
            (event.id, event.business_id, identity_hash),
        ).fetchone()
        if row is None:
            raise RuntimeError("event registration upsert did not persist a row")
        return _registration_from_row(row), created

    def get_registration_by_token(self, *, token: str) -> EventRegistration:
        capability = str(token or "").strip()
        row = self._conn.execute(
            f"SELECT {_REGISTRATION_COLUMNS} FROM clientplatform_event_registrations "
            "WHERE token=? AND status='registered' LIMIT 1",  # nosec B608
            (capability,),
        ).fetchone()
        if row is None:
            raise EventNotFound("registration token is invalid")
        return _registration_from_row(row)

    def get_registration(
        self,
        *,
        actor: TenantContext,
        registration_id: str,
        event_id: str,
    ) -> EventRegistration:
        current = self._actor(actor, manage=False)
        registration = normalize_uuid(registration_id, field_name="registration_id")
        event = normalize_uuid(event_id, field_name="event_id")
        row = self._conn.execute(
            f"SELECT {_REGISTRATION_COLUMNS} FROM clientplatform_event_registrations "
            "WHERE id=? AND event_id=? AND business_id=? LIMIT 1",  # nosec B608
            (registration, event, current.business_id),
        ).fetchone()
        if row is None:
            raise EventNotFound("event registration was not found")
        return _registration_from_row(row)

    def attach_customer(
        self,
        *,
        business_id: str,
        registration_id: str,
        customer_id: str,
    ) -> None:
        business = normalize_uuid(business_id, field_name="business_id")
        registration = normalize_uuid(registration_id, field_name="registration_id")
        customer = normalize_uuid(customer_id, field_name="customer_id")
        self._conn.execute(
            """
            UPDATE clientplatform_event_registrations
            SET customer_id=COALESCE(customer_id,?)
            WHERE id=? AND business_id=? AND status='registered'
            """,
            (customer, registration, business),
        )

    def mark_join_click(
        self,
        *,
        registration: EventRegistration,
        event: Event,
        now: datetime | None = None,
    ) -> bool:
        timestamp = normalize_utc(now or datetime.now(timezone.utc), field_name="now")
        if not event.join_click_counts_for_funnel(now=timestamp):
            return False
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_event_registrations
            SET first_join_click_at=COALESCE(first_join_click_at,?)
            WHERE id=? AND event_id=? AND business_id=? AND status='registered'
            """,
            (timestamp.isoformat(), registration.id, event.id, event.business_id),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) == 1

    def confirm_attendance(
        self,
        *,
        business_id: str,
        event_id: str,
        registration_id: str,
        source: str,
        occurred_at: datetime | str,
    ) -> bool:
        business = normalize_uuid(business_id, field_name="business_id")
        event = normalize_uuid(event_id, field_name="event_id")
        registration = normalize_uuid(registration_id, field_name="registration_id")
        source_value = " ".join(str(source or "").split()).strip()[:120]
        if not source_value:
            raise ValueError("attendance source is required")
        timestamp = normalize_utc(occurred_at, field_name="occurred_at").isoformat()
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_event_registrations
            SET attendance_confirmed_at=COALESCE(attendance_confirmed_at,?),
                attendance_source=COALESCE(attendance_source,?)
            WHERE id=? AND event_id=? AND business_id=? AND status='registered'
            """,
            (timestamp, source_value, registration, event, business),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) == 1

    def mark_offer_clicked(self, *, registration: EventRegistration, event: Event, now: datetime | None = None) -> bool:
        if not event.offer_url:
            return False
        timestamp = normalize_utc(now or datetime.now(timezone.utc), field_name="now").isoformat()
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_event_registrations
            SET offer_clicked_at=COALESCE(offer_clicked_at,?)
            WHERE id=? AND event_id=? AND business_id=? AND status='registered'
            """,
            (timestamp, registration.id, event.id, event.business_id),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) == 1

    def list_registrations(
        self,
        *,
        actor: TenantContext,
        event_id: str,
        limit: int = 500,
    ) -> list[EventRegistration]:
        current = self._actor(actor, manage=False)
        event = normalize_uuid(event_id, field_name="event_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000:
            raise ValueError("limit must be an integer between 1 and 5000")
        rows = self._conn.execute(
            f"SELECT {_REGISTRATION_COLUMNS} FROM clientplatform_event_registrations "
            "WHERE business_id=? AND event_id=? ORDER BY registered_at,id LIMIT ?",  # nosec B608
            (current.business_id, event, limit),
        ).fetchall()
        return [_registration_from_row(row) for row in rows]


__all__ = ["EventNotFound", "EventRepository", "EventStateConflict"]
