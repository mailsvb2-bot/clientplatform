from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest

from clientplatform.application.event_analytics import (
    get_event_funnel_in_transaction,
    link_verified_payment_in_transaction,
)
from clientplatform.application.events import (
    create_event_in_transaction,
    publish_event_in_transaction,
    register_public_attendee_in_transaction,
)
from clientplatform.infrastructure.event_repository import EventNotFound, EventRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db.schema import create_or_update_tables


def _setup_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    create_or_update_tables(conn)
    return conn


def _owner(conn: sqlite3.Connection, user_id: int, name: str):
    access = TenancyRepository(conn).create_business(owner_user_id=user_id, name=name)
    return TenancyRepository(conn).resolve_context(
        user_id=user_id,
        business_id=access.business.id,
    )


def _email_connection(conn: sqlite3.Connection, actor) -> str:
    connection_id = str(uuid4())
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    conn.execute(
        """
        INSERT INTO connections(
            id,business_id,platform,connection_type,external_account_id,
            credential_reference,permissions_json,status,created_by_member_id,
            created_at,updated_at,last_success_at,last_error_at,last_error_code
        ) VALUES(?,?, 'email','email_smtp','owner@example.test',
                 'secret://env/TEST_EVENT_SMTP','["send_email"]','active',?,?,?,NULL,NULL,NULL)
        """,
        (connection_id, actor.business_id, actor.membership_id, stamp, stamp),
    )
    return connection_id


def _published(conn: sqlite3.Connection, actor, *, starts_in_hours: int = 6):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    connection_id = _email_connection(conn, actor)
    event = create_event_in_transaction(
        conn,
        actor=actor,
        title="Provider-neutral эфир",
        description="Не зависит от Zoom",
        starts_at=now + timedelta(hours=starts_in_hours),
        timezone_name="Europe/Moscow",
        join_url="https://stream.vendor.example/room/123",
        provider_key="vendor_x",
        offer_url="https://shop.example.test/offer",
        notification_connection_id=connection_id,
        now=now,
    )
    return publish_event_in_transaction(
        conn,
        actor=actor,
        event_id=event.id,
        now=now,
    )


def test_public_projection_never_contains_provider_join_url() -> None:
    conn = _setup_conn()
    actor = _owner(conn, 101, "Практика А")
    event = _published(conn, actor)
    public = EventRepository(conn).get_public(public_slug=event.public_slug)
    assert not hasattr(public, "join_url")
    assert public.provider_key == "vendor_x"
    conn.close()


def test_registration_is_idempotent_and_queues_canonical_email_dispatches() -> None:
    conn = _setup_conn()
    actor = _owner(conn, 101, "Практика А")
    event = _published(conn, actor, starts_in_hours=30)
    with patch(
        "clientplatform.application.event_notifications._public_base_url",
        return_value="https://clientplatform.example.test",
    ):
        first = register_public_attendee_in_transaction(
            conn,
            public_slug=event.public_slug,
            name="Иван",
            email="IVAN@example.test",
            phone="+7 999 111-22-33",
            consent=True,
        )
        second = register_public_attendee_in_transaction(
            conn,
            public_slug=event.public_slug,
            name="Иван",
            email="ivan@example.test",
            phone=None,
            consent=True,
        )
    assert first.created is True
    assert second.created is False
    assert first.registration.id == second.registration.id
    rows = conn.execute(
        """
        SELECT source_kind,platform,status,idempotency_key
        FROM provider_dispatch_outbox
        WHERE business_id=? AND source_kind='event_message'
        ORDER BY available_at
        """,
        (actor.business_id,),
    ).fetchall()
    assert len(rows) >= 4
    assert {row["platform"] for row in rows} == {"email"}
    assert len({row["idempotency_key"] for row in rows}) == len(rows)
    conn.close()


def test_early_join_click_is_not_counted_as_attendance_or_show_up() -> None:
    conn = _setup_conn()
    actor = _owner(conn, 101, "Практика А")
    event = _published(conn, actor, starts_in_hours=6)
    with patch(
        "clientplatform.application.event_notifications._public_base_url",
        return_value="https://clientplatform.example.test",
    ):
        result = register_public_attendee_in_transaction(
            conn,
            public_slug=event.public_slug,
            name="Анна",
            email="anna@example.test",
            consent=True,
        )
    repo = EventRepository(conn)
    counted = repo.mark_join_click(
        registration=result.registration,
        event=event,
        now=event.starts_at - timedelta(hours=2),
    )
    assert counted is False
    row = conn.execute(
        "SELECT first_join_click_at,attendance_confirmed_at FROM clientplatform_event_registrations WHERE id=?",
        (result.registration.id,),
    ).fetchone()
    assert row["first_join_click_at"] is None
    assert row["attendance_confirmed_at"] is None
    conn.close()


def test_funnel_is_tenant_scoped_and_revenue_is_not_mixed_between_currencies() -> None:
    conn = _setup_conn()
    actor_a = _owner(conn, 101, "Практика А")
    actor_b = _owner(conn, 202, "Практика Б")
    event = _published(conn, actor_a, starts_in_hours=4)
    with patch(
        "clientplatform.application.event_notifications._public_base_url",
        return_value="https://clientplatform.example.test",
    ):
        result = register_public_attendee_in_transaction(
            conn,
            public_slug=event.public_slug,
            name="Мария",
            email="maria@example.test",
            consent=True,
        )
    repo = EventRepository(conn)
    repo.mark_join_click(
        registration=result.registration,
        event=event,
        now=event.starts_at - timedelta(minutes=10),
    )
    repo.confirm_attendance(
        business_id=event.business_id,
        event_id=event.id,
        registration_id=result.registration.id,
        source="provider_webhook:vendor_x",
        occurred_at=event.starts_at + timedelta(minutes=3),
    )
    repo.mark_offer_clicked(registration=result.registration, event=event)
    assert link_verified_payment_in_transaction(
        conn,
        business_id=event.business_id,
        event_id=event.id,
        registration_id=result.registration.id,
        payment_ref="pay-rub",
        amount_minor=150_000,
        currency="RUB",
        occurred_at=event.starts_at + timedelta(hours=1),
    )
    assert link_verified_payment_in_transaction(
        conn,
        business_id=event.business_id,
        event_id=event.id,
        registration_id=result.registration.id,
        payment_ref="pay-usd",
        amount_minor=1_000,
        currency="USD",
        occurred_at=event.starts_at + timedelta(hours=1, minutes=1),
    )
    funnel = get_event_funnel_in_transaction(conn, actor=actor_a, event_id=event.id)
    assert funnel.registered == 1
    assert funnel.join_clicked == 1
    assert funnel.attendance_confirmed == 1
    assert funnel.paid == 1
    assert {(item.currency, item.amount_minor) for item in funnel.revenue} == {
        ("RUB", 150_000),
        ("USD", 1_000),
    }
    with pytest.raises(EventNotFound):
        get_event_funnel_in_transaction(conn, actor=actor_b, event_id=event.id)
    conn.close()


def test_repeat_registration_repairs_failed_crm_projection() -> None:
    conn = _setup_conn()
    actor = _owner(conn, 303, "Практика repair")
    event = _published(conn, actor, starts_in_hours=8)
    with (
        patch(
            "clientplatform.application.event_notifications._public_base_url",
            return_value="https://clientplatform.example.test",
        ),
        patch(
            "clientplatform.application.events.attach_event_registration_to_customer",
            side_effect=RuntimeError("temporary crm failure"),
        ),
    ):
        first = register_public_attendee_in_transaction(
            conn,
            public_slug=event.public_slug,
            name="Repair",
            email="repair@example.test",
            consent=True,
        )
    assert first.created is True
    assert first.customer_id is None
    with patch(
        "clientplatform.application.event_notifications._public_base_url",
        return_value="https://clientplatform.example.test",
    ):
        second = register_public_attendee_in_transaction(
            conn,
            public_slug=event.public_slug,
            name="Repair",
            email="repair@example.test",
            consent=True,
        )
    assert second.created is False
    assert second.customer_id is not None
    stored = conn.execute(
        "SELECT customer_id FROM clientplatform_event_registrations WHERE id=?",
        (second.registration.id,),
    ).fetchone()
    assert stored["customer_id"] == second.customer_id
    conn.close()


def test_customer_delete_keeps_registration_and_clears_only_customer_link() -> None:
    conn = _setup_conn()
    actor = _owner(conn, 404, "Практика erase")
    event = _published(conn, actor, starts_in_hours=8)
    with patch(
        "clientplatform.application.event_notifications._public_base_url",
        return_value="https://clientplatform.example.test",
    ):
        result = register_public_attendee_in_transaction(
            conn,
            public_slug=event.public_slug,
            name="Erase",
            email="erase@example.test",
            consent=True,
        )
    assert result.customer_id is not None
    conn.execute("DELETE FROM customers WHERE id=?", (result.customer_id,))
    row = conn.execute(
        "SELECT business_id,customer_id FROM clientplatform_event_registrations WHERE id=?",
        (result.registration.id,),
    ).fetchone()
    assert row is not None
    assert row["business_id"] == actor.business_id
    assert row["customer_id"] is None
    conn.close()


def test_late_registration_confirmation_contains_personal_join_link() -> None:
    conn = _setup_conn()
    actor = _owner(conn, 505, "Практика late")
    event = _published(conn, actor, starts_in_hours=1)
    now = event.starts_at - timedelta(minutes=5)
    with patch(
        "clientplatform.application.event_notifications._public_base_url",
        return_value="https://clientplatform.example.test",
    ):
        result = register_public_attendee_in_transaction(
            conn,
            public_slug=event.public_slug,
            name="Late",
            email="late@example.test",
            consent=True,
            now=now,
        )
    row = conn.execute(
        """
        SELECT payload_ref FROM provider_dispatch_outbox
        WHERE business_id=? AND source_kind='event_message'
          AND idempotency_key LIKE '%:message:registration_confirmed:v2'
        LIMIT 1
        """,
        (actor.business_id,),
    ).fetchone()
    assert row is not None
    assert f"/e/join/{result.registration.token}" in row["payload_ref"]
    conn.close()
