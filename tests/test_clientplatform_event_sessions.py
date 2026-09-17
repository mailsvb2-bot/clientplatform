from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from clientplatform.application.event_sessions import select_event_session_for_join
from clientplatform.domain.event_sessions import (
    EventSession,
    legacy_event_session,
    validate_event_session_sequence,
)
from clientplatform.domain.events import Event, EventValidationError
from services.db.schema import clientplatform_event_sessions, clientplatform_events


NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def _event(*, join_url: str | None = "https://day1.example.test/room") -> Event:
    return Event(
        id=str(uuid4()),
        business_id=str(uuid4()),
        created_by_member_id=str(uuid4()),
        kind="webinar",
        status="draft",
        title="Two-day webinar",
        description="",
        starts_at=NOW + timedelta(days=5),
        ends_at=NOW + timedelta(days=5, hours=2),
        timezone_name="Europe/Moscow",
        provider_key="auto" if join_url else "pending",
        provider_label=None,
        join_url=join_url,
        offer_url=None,
        public_slug="A" * 24,
        consent_version="v1",
        notification_connection_id=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _session(event: Event, *, position: int, day_offset: int, join_url: str) -> EventSession:
    starts_at = event.starts_at + timedelta(days=day_offset)
    return EventSession(
        id=str(uuid4()),
        business_id=event.business_id,
        event_id=event.id,
        position=position,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=2),
        provider_key="auto",
        provider_label=None,
        join_url=join_url,
        created_at=NOW,
        updated_at=NOW,
    )


def _two_day_sessions(event: Event) -> tuple[EventSession, EventSession]:
    return (
        _session(
            event,
            position=1,
            day_offset=0,
            join_url="https://day1.example.test/room",
        ),
        _session(
            event,
            position=2,
            day_offset=1,
            join_url="https://day2.example.test/room",
        ),
    )


def test_two_day_event_sessions_keep_distinct_join_targets() -> None:
    event = _event()
    sessions = validate_event_session_sequence(
        _two_day_sessions(event),
        business_id=event.business_id,
        event_id=event.id,
    )

    assert len(sessions) == 2
    assert sessions[0].join_url == "https://day1.example.test/room"
    assert sessions[1].join_url == "https://day2.example.test/room"
    assert sessions[0].starts_at < sessions[1].starts_at
    assert all(session.join_is_ready for session in sessions)


def test_legacy_personal_join_advances_from_day_one_to_day_two() -> None:
    event = _event()
    day_one, day_two = _two_day_sessions(event)

    assert select_event_session_for_join(
        (day_one, day_two), now=day_one.starts_at - timedelta(hours=1)
    ) == day_one
    assert select_event_session_for_join(
        (day_one, day_two), now=day_one.starts_at + timedelta(hours=1)
    ) == day_one
    assert select_event_session_for_join(
        (day_one, day_two), now=day_one.ends_at + timedelta(minutes=1)
    ) == day_two
    assert select_event_session_for_join(
        (day_one, day_two), now=day_two.ends_at + timedelta(minutes=1)
    ) == day_two


def test_session_specific_personal_join_never_leaks_another_day_room() -> None:
    event = _event()
    day_one, day_two = _two_day_sessions(event)

    selected = select_event_session_for_join(
        (day_one, day_two),
        position=2,
        now=day_one.starts_at - timedelta(days=2),
    )

    assert selected.position == 2
    assert selected.join_url == "https://day2.example.test/room"
    with pytest.raises(LookupError, match="not found"):
        select_event_session_for_join((day_one, day_two), position=3, now=NOW)


def test_legacy_event_is_exposed_as_deterministic_single_session() -> None:
    event = _event()

    first = legacy_event_session(event)
    second = legacy_event_session(event)

    assert first == second
    assert first.position == 1
    assert first.business_id == event.business_id
    assert first.event_id == event.id
    assert first.starts_at == event.starts_at
    assert first.ends_at == event.ends_at
    assert first.join_url == event.join_url


def test_session_sequence_rejects_gaps_and_reverse_time() -> None:
    event = _event()
    first = _session(
        event,
        position=1,
        day_offset=1,
        join_url="https://day1.example.test/room",
    )
    gap = _session(
        event,
        position=3,
        day_offset=2,
        join_url="https://day3.example.test/room",
    )
    with pytest.raises(EventValidationError, match="positions"):
        validate_event_session_sequence(
            [first, gap],
            business_id=event.business_id,
            event_id=event.id,
        )

    earlier_second = _session(
        event,
        position=2,
        day_offset=0,
        join_url="https://day2.example.test/room",
    )
    with pytest.raises(EventValidationError, match="chronological"):
        validate_event_session_sequence(
            [first, earlier_second],
            business_id=event.business_id,
            event_id=event.id,
        )


def test_event_session_rejects_insecure_join_url() -> None:
    event = _event()
    with pytest.raises(EventValidationError, match="HTTPS"):
        _session(
            event,
            position=1,
            day_offset=0,
            join_url="http://day1.example.test/room",
        )


def test_event_session_schema_is_idempotent_and_tenant_scoped() -> None:
    conn = sqlite3.connect(":memory:")
    clientplatform_events.ensure(conn)
    clientplatform_event_sessions.ensure(conn)
    clientplatform_event_sessions.ensure(conn)

    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(clientplatform_event_sessions)").fetchall()
    }
    indexes = {
        str(row[1])
        for row in conn.execute("PRAGMA index_list(clientplatform_event_sessions)").fetchall()
    }

    assert {
        "id",
        "business_id",
        "event_id",
        "position",
        "starts_at",
        "join_url",
    }.issubset(columns)
    assert "idx_clientplatform_event_sessions_event" in indexes
    assert "idx_clientplatform_event_sessions_start" in indexes
