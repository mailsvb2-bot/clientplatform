from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from clientplatform.application import event_notifications
from clientplatform.application.event_delivery_targets import EventDeliveryTarget
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

class _InsertCursor:
    def __init__(self, *, rowcount: int = 1, rows: tuple[object, ...] = ()) -> None:
        self.rowcount = rowcount
        self._rows = rows

    def fetchall(self) -> list[object]:
        return list(self._rows)


class _OutboxConnection:
    def __init__(self, *, confirmation_platforms: tuple[str, ...] = ()) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.confirmation_platforms = confirmation_platforms

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> _InsertCursor:
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        if "SELECT DISTINCT platform" in normalized:
            return _InsertCursor(rows=tuple((platform,) for platform in self.confirmation_platforms))
        return _InsertCursor()


def test_rescheduled_reminder_gets_schedule_revision_idempotency_key() -> None:
    event = _event()
    registration = _registration(event)
    session = _session(event, 1)
    target = EventDeliveryTarget(
        platform="telegram",
        connection_id=str(uuid4()),
        recipient_kind="external_subject",
        customer_identity_id=None,
        external_subject="123456",
    )
    conn = _OutboxConnection()

    with patch.object(
        event_notifications,
        "_public_base_url",
        return_value="https://clientplatform.example.test",
    ):
        created = event_notifications._materialize(
            conn,
            event=event,
            registration=registration,
            target=target,
            kind="24h",
            scheduled_at=session.starts_at - timedelta(hours=24),
            session=session,
            schedule_revision="0123456789abcdef",
        )

    assert created
    assert len(conn.calls) == 1
    params = conn.calls[0][1]
    assert str(params[10]).endswith(":schedule:0123456789abcdef")

def test_schedule_edit_rebuilds_future_reminders_and_cancels_old_queue() -> None:
    event = _event()
    registration = _registration(event)
    sessions = (_session(event, 1), _session(event, 2))
    target = EventDeliveryTarget(
        platform="telegram",
        connection_id=str(uuid4()),
        recipient_kind="external_subject",
        customer_identity_id=None,
        external_subject="123456",
    )
    conn = _OutboxConnection()
    pages = [[registration], []]

    class Repository:
        def get(self, **_kwargs):
            return event

        def list_active_registrations_page(self, **_kwargs):
            return pages.pop(0)

    session_repository = type(
        "SessionRepository",
        (),
        {"list_for_event_record": lambda self, **_kwargs: sessions},
    )()

    with (
        patch.object(event_notifications, "EventRepository", return_value=Repository()),
        patch.object(
            event_notifications,
            "EventSessionRepository",
            return_value=session_repository,
        ),
        patch.object(
            event_notifications,
            "resolve_event_organizational_targets",
            return_value=(target,),
        ),
        patch.object(event_notifications, "_materialize", return_value=True) as materialize,
    ):
        queued = event_notifications.reschedule_event_notifications_in_transaction(
            conn,
            actor=object(),
            event_id=event.id,
            now=NOW,
        )

    assert queued == 6
    update_sql = next(sql for sql, _ in conn.calls if sql.startswith("UPDATE provider_dispatch_outbox"))
    assert "last_error='event_schedule_changed'" in update_sql
    assert "status='sending'" in update_sql
    assert "registration_confirmed" in update_sql
    assert "provider_call_started_non_idempotent" in update_sql
    assert materialize.call_count == 6
    revisions = {call.kwargs["schedule_revision"] for call in materialize.call_args_list}
    assert len(revisions) == 1
    revision = next(iter(revisions))
    assert len(revision) == 16
    assert all(char in "0123456789abcdef" for char in revision)
    assert {call.kwargs["kind"] for call in materialize.call_args_list} == {
        "24h",
        "3h",
        "15m",
    }


def test_schedule_edit_rebuilds_pending_confirmation_with_new_schedule_revision() -> None:
    event = _event()
    registration = _registration(event)
    sessions = (_session(event, 1),)
    target = EventDeliveryTarget(
        platform="telegram",
        connection_id=str(uuid4()),
        recipient_kind="external_subject",
        customer_identity_id=None,
        external_subject="123456",
    )
    conn = _OutboxConnection(confirmation_platforms=("telegram",))
    pages = [[registration], []]

    class Repository:
        def get(self, **_kwargs):
            return event

        def list_active_registrations_page(self, **_kwargs):
            return pages.pop(0)

    session_repository = type(
        "SessionRepository",
        (),
        {"list_for_event_record": lambda self, **_kwargs: sessions},
    )()

    with (
        patch.object(event_notifications, "EventRepository", return_value=Repository()),
        patch.object(
            event_notifications,
            "EventSessionRepository",
            return_value=session_repository,
        ),
        patch.object(
            event_notifications,
            "resolve_event_organizational_targets",
            return_value=(target,),
        ),
        patch.object(event_notifications, "_materialize", return_value=True) as materialize,
    ):
        queued = event_notifications.reschedule_event_notifications_in_transaction(
            conn,
            actor=object(),
            event_id=event.id,
            now=NOW,
        )

    assert queued == 4
    confirmation = [
        call for call in materialize.call_args_list
        if call.kwargs["kind"] == "registration_confirmed"
    ]
    assert len(confirmation) == 1
    assert confirmation[0].kwargs["scheduled_at"] == NOW
    assert len(confirmation[0].kwargs["schedule_revision"]) == 16


def test_schedule_edit_paginates_active_registrations_beyond_one_page() -> None:
    event = _event()
    first = _registration(event)
    last = EventRegistration(
        id=str(uuid4()),
        event_id=event.id,
        business_id=event.business_id,
        customer_id=None,
        status="registered",
        name="Пётр",
        email="petr@example.test",
        phone=None,
        source=None,
        campaign_ref=None,
        token="P" * 40,
        consent_version="v1",
        consented_at=NOW,
        registered_at=NOW + timedelta(seconds=1),
    )
    sessions = (_session(event, 1),)
    first_page = [first] * 500
    pages = [first_page, [last]]

    class Repository:
        def __init__(self) -> None:
            self.calls = []

        def get(self, **_kwargs):
            return event

        def list_active_registrations_page(self, **kwargs):
            self.calls.append(kwargs)
            return pages.pop(0)

    repository = Repository()
    session_repository = type(
        "SessionRepository",
        (),
        {"list_for_event_record": lambda self, **_kwargs: sessions},
    )()
    conn = _OutboxConnection()

    with (
        patch.object(event_notifications, "EventRepository", return_value=repository),
        patch.object(
            event_notifications,
            "EventSessionRepository",
            return_value=session_repository,
        ),
        patch.object(
            event_notifications,
            "resolve_event_organizational_targets",
            return_value=(),
        ),
    ):
        queued = event_notifications.reschedule_event_notifications_in_transaction(
            conn,
            actor=object(),
            event_id=event.id,
            now=NOW,
        )

    assert queued == 0
    assert len(repository.calls) == 2
    assert repository.calls[0]["after_registered_at"] is None
    assert repository.calls[1]["after_registered_at"] == first.registered_at.isoformat()
    assert repository.calls[1]["after_id"] == first.id

