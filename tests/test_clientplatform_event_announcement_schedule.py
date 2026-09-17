from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from clientplatform.application.event_announcements import _safe_template, _schedule_lines
from clientplatform.domain.event_sessions import EventSession
from clientplatform.domain.events import Event


NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def _event() -> Event:
    return Event(
        id=str(uuid4()),
        business_id=str(uuid4()),
        created_by_member_id=str(uuid4()),
        kind="webinar",
        status="published",
        title="Два дня практики",
        description="Только заявленная программа.",
        starts_at=datetime(2026, 9, 25, 16, 0, tzinfo=timezone.utc),
        ends_at=datetime(2026, 9, 26, 18, 0, tzinfo=timezone.utc),
        timezone_name="Europe/Moscow",
        provider_key="auto",
        provider_label=None,
        join_url="https://room-one.example.test/private",
        offer_url=None,
        public_slug="B" * 24,
        consent_version="v1",
        notification_connection_id=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _sessions(event: Event) -> tuple[EventSession, EventSession]:
    return tuple(
        EventSession(
            id=str(uuid4()),
            business_id=event.business_id,
            event_id=event.id,
            position=position,
            starts_at=event.starts_at + timedelta(days=position - 1),
            ends_at=event.starts_at + timedelta(days=position - 1, hours=2),
            provider_key="auto",
            provider_label=None,
            join_url=f"https://room-{position}.example.test/private",
            created_at=NOW,
            updated_at=NOW,
        )
        for position in (1, 2)
    )


def test_two_day_announcement_lists_both_days_without_room_urls() -> None:
    event = _event()
    schedule = _schedule_lines(event, _sessions(event))
    text = _safe_template(
        title=event.title,
        description=event.description,
        schedule=schedule,
    )

    assert schedule == (
        "День 1: 25.09.2026 19:00",
        "День 2: 26.09.2026 19:00",
    )
    assert "День 1: 25.09.2026 19:00" in text
    assert "День 2: 26.09.2026 19:00" in text
    assert "room-one" not in text
    assert "room-2" not in text
