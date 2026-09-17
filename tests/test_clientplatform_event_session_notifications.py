from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from clientplatform.application import event_notifications
from clientplatform.domain.event_sessions import EventSession
from clientplatform.domain.events import Event, EventRegistration


NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def _event() -> Event:
    return Event(
        id=str(uuid4()),
        business_id=str(uuid4()),
        created_by_member_id=str(uuid4()),
        kind="webinar",
        status="published",
        title="Два дня практики",
        description="",
        starts_at=datetime(2026, 9, 25, 16, 0, tzinfo=timezone.utc),
        ends_at=datetime(2026, 9, 26, 18, 0, tzinfo=timezone.utc),
        timezone_name="Europe/Moscow",
        provider_key="external",
        provider_label=None,
        join_url="https://room-one.example.test/private",
        offer_url=None,
        public_slug="S" * 24,
        consent_version="v1",
        notification_connection_id=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _registration(event: Event) -> EventRegistration:
    return EventRegistration(
        id=str(uuid4()),
        event_id=event.id,
        business_id=event.business_id,
        customer_id=None,
        status="registered",
        name="Иван",
        email="ivan@example.test",
        phone=None,
        source=None,
        campaign_ref=None,
        token="T" * 40,
        consent_version="v1",
        consented_at=NOW,
        registered_at=NOW,
    )


def _session(event: Event, position: int) -> EventSession:
    starts_at = event.starts_at + timedelta(days=position - 1)
    return EventSession(
        id=str(uuid4()),
        business_id=event.business_id,
        event_id=event.id,
        position=position,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=2),
        provider_key="external",
        provider_label=None,
        join_url=f"https://room-{position}.example.test/private",
        created_at=NOW,
        updated_at=NOW,
    )


def test_second_day_reminder_uses_exact_personal_session_link_without_room_url() -> None:
    event = _event()
    registration = _registration(event)
    day_two = _session(event, 2)

    with patch.object(
        event_notifications,
        "_public_base_url",
        return_value="https://clientplatform.example.test",
    ):
        subject, body = event_notifications._render(
            kind="15m",
            event=event,
            registration=registration,
            session=day_two,
            session_count=2,
        )

    assert subject == "Через 15 минут: Два дня практики"
    assert "день 2 из 2" in body.casefold()
    assert f"/e/join/{registration.token}/2" in body
    assert day_two.join_url not in body
    assert event.join_url not in body


def test_second_day_24h_reminder_shows_second_day_local_time() -> None:
    event = _event()
    registration = _registration(event)
    day_two = _session(event, 2)

    with patch.object(
        event_notifications,
        "_public_base_url",
        return_value="https://clientplatform.example.test",
    ):
        _subject, body = event_notifications._render(
            kind="24h",
            event=event,
            registration=registration,
            session=day_two,
            session_count=2,
        )

    assert "День 2 из 2" in body
    assert "26.09.2026 19:00" in body
    assert "25.09.2026 19:00" not in body