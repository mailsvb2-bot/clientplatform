from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from clientplatform.domain.event_sessions import (
    EventSession,
    legacy_event_session,
    validate_event_session_sequence,
)
from clientplatform.domain.events import (
    Event,
    normalize_provider_key,
    normalize_provider_label,
    normalize_utc,
    validate_external_https_url,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.event_repository import (
    EventNotFound,
    EventRepository,
    EventStateConflict,
)
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def _optional(row: Any, key: str, position: int) -> Any | None:
    value = _value(row, key, position)
    return None if value is None else value


def _session_from_row(row: Any) -> EventSession:
    return EventSession(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        event_id=str(_value(row, "event_id", 2)),
        position=int(_value(row, "position", 3)),
        starts_at=str(_value(row, "starts_at", 4)),
        ends_at=_optional(row, "ends_at", 5),
        provider_key=str(_value(row, "provider_key", 6)),
        provider_label=_optional(row, "provider_label", 7),
        join_url=_optional(row, "join_url", 8),
        created_at=str(_value(row, "created_at", 9)),
        updated_at=str(_value(row, "updated_at", 10)),
    )


_SESSION_COLUMNS = """
id, business_id, event_id, position, starts_at, ends_at, provider_key,
provider_label, join_url, created_at, updated_at
""".strip()


class EventSessionRepository:
    """Tenant-scoped persistence for ordered occurrences of one canonical event."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)
        self._events = EventRepository(conn)

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

    def _list_for_event_record(self, *, event: Event) -> tuple[EventSession, ...]:
        rows = self._conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM clientplatform_event_sessions "
            "WHERE event_id=? AND business_id=? ORDER BY position ASC",  # nosec B608
            (event.id, event.business_id),
        ).fetchall()
        if rows:
            return tuple(_session_from_row(row) for row in rows)
        return (legacy_event_session(event),)

    def list_for_event_record(self, *, event: Event) -> tuple[EventSession, ...]:
        """List sessions for an Event that was already authorized by its caller."""

        return self._list_for_event_record(event=event)

    def list_for_event(self, *, actor: TenantContext, event_id: str) -> tuple[EventSession, ...]:
        current = self._actor(actor, manage=False)
        normalized = normalize_uuid(event_id, field_name="event_id")
        event = self._events.get(actor=current, event_id=normalized)
        return self._list_for_event_record(event=event)

    def replace_for_event(
        self,
        *,
        actor: TenantContext,
        event_id: str,
        sessions: tuple[EventSession, ...] | list[EventSession],
        now: datetime | None = None,
    ) -> tuple[EventSession, ...]:
        current = self._actor(actor, manage=True)
        normalized = normalize_uuid(event_id, field_name="event_id")
        event = self._events.get(actor=current, event_id=normalized)
        if event.status not in {"draft", "published"}:
            raise EventStateConflict("event sessions cannot be changed in its current state")
        ordered = validate_event_session_sequence(
            sessions,
            business_id=current.business_id,
            event_id=normalized,
        )
        timestamp = normalize_utc(now or datetime.now(timezone.utc), field_name="now")
        first = ordered[0]
        last = ordered[-1]
        root_ends_at = last.ends_at or (last.starts_at + timedelta(hours=2))

        self._conn.execute(
            "DELETE FROM clientplatform_event_sessions WHERE event_id=? AND business_id=?",
            (normalized, current.business_id),
        )
        for session in ordered:
            self._conn.execute(
                """
                INSERT INTO clientplatform_event_sessions(
                    id,business_id,event_id,position,starts_at,ends_at,provider_key,
                    provider_label,join_url,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    session.id,
                    session.business_id,
                    session.event_id,
                    session.position,
                    session.starts_at.isoformat(),
                    None if session.ends_at is None else session.ends_at.isoformat(),
                    session.provider_key,
                    session.provider_label,
                    session.join_url,
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                ),
            )

        cursor = self._conn.execute(
            """
            UPDATE clientplatform_events
            SET starts_at=?,ends_at=?,provider_key=?,provider_label=?,join_url=?,updated_at=?
            WHERE id=? AND business_id=? AND status IN ('draft','published')
            """,
            (
                first.starts_at.isoformat(),
                root_ends_at.isoformat(),
                first.provider_key,
                first.provider_label,
                first.join_url or "",
                timestamp.isoformat(),
                normalized,
                current.business_id,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise EventStateConflict("event sessions changed concurrently")
        return self.list_for_event(actor=current, event_id=normalized)

    def set_join_target(
        self,
        *,
        actor: TenantContext,
        event_id: str,
        position: int,
        join_url: str,
        provider_key: str = "auto",
        provider_label: str | None = None,
        now: datetime | None = None,
    ) -> EventSession:
        current = self._actor(actor, manage=True)
        normalized = normalize_uuid(event_id, field_name="event_id")
        if isinstance(position, bool) or not isinstance(position, int) or position < 1:
            raise ValueError("position must be a positive integer")
        normalized_url = validate_external_https_url(join_url, field_name="session_join_url")
        normalized_provider = normalize_provider_key(provider_key, join_url=normalized_url)
        normalized_label = normalize_provider_label(provider_label)
        timestamp = normalize_utc(now or datetime.now(timezone.utc), field_name="now")

        explicit = self._conn.execute(
            "SELECT 1 FROM clientplatform_event_sessions WHERE event_id=? AND business_id=? LIMIT 1",
            (normalized, current.business_id),
        ).fetchone()
        if explicit is None:
            if position != 1:
                raise EventNotFound("event session was not found in the active business")
            event = self._events.set_join_target(
                actor=current,
                event_id=normalized,
                join_url=normalized_url,
                provider_key=normalized_provider,
                provider_label=normalized_label,
                now=timestamp,
            )
            return legacy_event_session(event)

        cursor = self._conn.execute(
            """
            UPDATE clientplatform_event_sessions
            SET join_url=?,provider_key=?,provider_label=?,updated_at=?
            WHERE event_id=? AND business_id=? AND position=?
            """,
            (
                normalized_url,
                normalized_provider,
                normalized_label,
                timestamp.isoformat(),
                normalized,
                current.business_id,
                position,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise EventNotFound("event session was not found in the active business")
        if position == 1:
            parent_cursor = self._conn.execute(
                """
                UPDATE clientplatform_events
                SET join_url=?,provider_key=?,provider_label=?,updated_at=?
                WHERE id=? AND business_id=? AND status IN ('draft','published')
                """,
                (
                    normalized_url,
                    normalized_provider,
                    normalized_label,
                    timestamp.isoformat(),
                    normalized,
                    current.business_id,
                ),
            )
            if int(getattr(parent_cursor, "rowcount", 0) or 0) != 1:
                raise EventStateConflict("event join target cannot be changed in its current state")
        row = self._conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM clientplatform_event_sessions "
            "WHERE event_id=? AND business_id=? AND position=? LIMIT 1",  # nosec B608
            (normalized, current.business_id, position),
        ).fetchone()
        if row is None:
            raise EventNotFound("event session was not found in the active business")
        return _session_from_row(row)
