from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from clientplatform.application import customer_activity
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.customer_repository import CustomerRepository
from services.db.schema import clientplatform_customers, clientplatform_tenancy


def _timestamp(hour: int) -> datetime:
    return datetime(2026, 8, 14, hour, 0, 0, tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _activity_db(conn: sqlite3.Connection):
    @contextmanager
    def _open():
        yield conn

    return _open


def _new_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    clientplatform_tenancy.ensure(conn)
    clientplatform_customers.ensure(conn)
    return conn


def _seed_customer(
    conn: sqlite3.Connection,
    *,
    owner_user_id: int,
    business_name: str,
    external_subject: str,
    created_at: datetime,
):
    tenancy = TenancyRepository(conn)
    access = tenancy.create_business(
        owner_user_id=owner_user_id,
        name=business_name,
        now=_iso(created_at),
    )
    actor = tenancy.resolve_context(
        user_id=owner_user_id,
        business_id=access.business.id,
    )
    customers = CustomerRepository(conn)
    customer = customers.create_customer(
        actor=actor,
        display_name=f"Customer {business_name}",
        now=_iso(created_at),
    )
    customers.attach_identity(
        actor=actor,
        customer_id=customer.id,
        platform="telegram",
        external_subject=external_subject,
        username=f"{business_name.lower()}-customer",
        now=_iso(created_at),
    )
    return actor, customer


def test_customer_activity_is_strictly_tenant_scoped_for_same_external_subject(monkeypatch):
    conn = _new_db()
    try:
        created_at = _timestamp(8)
        contact_at = _timestamp(10)
        actor_a, customer_a = _seed_customer(
            conn,
            owner_user_id=10101,
            business_name="Tenant A",
            external_subject="shared-external-subject",
            created_at=created_at,
        )
        actor_b, customer_b = _seed_customer(
            conn,
            owner_user_id=20202,
            business_name="Tenant B",
            external_subject="shared-external-subject",
            created_at=created_at,
        )
        monkeypatch.setattr(customer_activity, "get_db", _activity_db(conn))
        monkeypatch.setattr(customer_activity, "get_db_ro", _activity_db(conn))

        assert customer_activity.record_customer_contact(
            business_id=actor_a.business_id,
            platform="telegram",
            external_subject="shared-external-subject",
            at=contact_at,
        ) is True

        summary_a = customer_activity.tenant_customer_activity(
            actor=actor_a,
            now=contact_at,
        )
        summary_b = customer_activity.tenant_customer_activity(
            actor=actor_b,
            now=contact_at,
        )

        assert summary_a.total == 1
        assert summary_b.total == 1
        assert summary_a.by_platform == {"telegram": 1}
        assert summary_b.by_platform == {"telegram": 1}
        assert summary_a.recent[0].customer_id == customer_a.id
        assert summary_b.recent[0].customer_id == customer_b.id
        assert summary_a.recent[0].business_id == actor_a.business_id
        assert summary_b.recent[0].business_id == actor_b.business_id
        assert summary_a.recent[0].last_contact_at == _iso(contact_at)
        assert summary_b.recent[0].last_contact_at == _iso(created_at)

        tenant_b_identity = conn.execute(
            """
            SELECT last_contact_at
            FROM customer_identities
            WHERE business_id=? AND platform='telegram' AND external_subject=?
            """,
            (actor_b.business_id, "shared-external-subject"),
        ).fetchone()
        assert tenant_b_identity is not None
        assert tenant_b_identity["last_contact_at"] is None
    finally:
        conn.close()


def test_repeat_contact_preserves_first_contact_and_updates_last_contact(monkeypatch):
    conn = _new_db()
    try:
        created_at = _timestamp(7)
        first_repeat = _timestamp(9)
        second_repeat = _timestamp(11)
        actor, customer = _seed_customer(
            conn,
            owner_user_id=30303,
            business_name="Tenant Repeat",
            external_subject="repeat-contact",
            created_at=created_at,
        )
        monkeypatch.setattr(customer_activity, "get_db", _activity_db(conn))
        monkeypatch.setattr(customer_activity, "get_db_ro", _activity_db(conn))

        assert customer_activity.record_customer_contact(
            business_id=actor.business_id,
            platform="telegram",
            external_subject="repeat-contact",
            username="first-handle",
            at=first_repeat,
        ) is True
        assert customer_activity.record_customer_contact(
            business_id=actor.business_id,
            platform="telegram",
            external_subject="repeat-contact",
            username="latest-handle",
            at=second_repeat,
        ) is True

        summary = customer_activity.tenant_customer_activity(
            actor=actor,
            now=second_repeat,
        )
        assert summary.total == 1
        assert summary.by_platform == {"telegram": 1}
        assert len(summary.recent) == 1
        assert summary.recent[0].customer_id == customer.id
        assert summary.recent[0].first_contact_at == _iso(created_at)
        assert summary.recent[0].last_contact_at == _iso(second_repeat)
        assert summary.recent[0].username == "latest-handle"

        identity = conn.execute(
            """
            SELECT first_contact_at, last_contact_at, username
            FROM customer_identities
            WHERE business_id=? AND platform='telegram' AND external_subject='repeat-contact'
            """,
            (actor.business_id,),
        ).fetchone()
        assert identity is not None
        assert identity["first_contact_at"] == _iso(created_at)
        assert identity["last_contact_at"] == _iso(second_repeat)
        assert identity["username"] == "latest-handle"
        assert conn.execute(
            "SELECT COUNT(*) FROM customers WHERE business_id=?",
            (actor.business_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM customer_identities WHERE business_id=?",
            (actor.business_id,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_unlinked_contact_does_not_create_shadow_customer_or_identity(monkeypatch):
    conn = _new_db()
    try:
        tenancy = TenancyRepository(conn)
        access = tenancy.create_business(
            owner_user_id=40404,
            name="Tenant Missing",
            now=_iso(_timestamp(6)),
        )
        monkeypatch.setattr(customer_activity, "get_db", _activity_db(conn))

        assert customer_activity.record_customer_contact(
            business_id=access.business.id,
            platform="telegram",
            external_subject="not-linked",
            at=_timestamp(12),
        ) is False
        assert conn.execute(
            "SELECT COUNT(*) FROM customers WHERE business_id=?",
            (access.business.id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM customer_identities WHERE business_id=?",
            (access.business.id,),
        ).fetchone()[0] == 0
    finally:
        conn.close()
