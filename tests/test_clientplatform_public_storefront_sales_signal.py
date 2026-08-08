from __future__ import annotations

import importlib
import json
import sqlite3
from contextlib import contextmanager
from typing import Iterator

import pytest

from clientplatform.infrastructure import TenancyRepository
from services.db.schema import (
    clientplatform_activity,
    clientplatform_customers,
    clientplatform_sales,
    clientplatform_tenancy,
)

journey = importlib.import_module("clientplatform.application.owner_booking_journey")


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    clientplatform_tenancy.ensure(conn)
    clientplatform_customers.ensure(conn)
    clientplatform_activity.ensure(conn)
    clientplatform_sales.ensure(conn)
    return conn


def test_public_storefront_visit_is_real_replay_safe_sales_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _database()
    try:
        access = TenancyRepository(conn).create_business(
            owner_user_id=101,
            name="Практика",
        )
        conn.commit()

        @contextmanager
        def use_db() -> Iterator[sqlite3.Connection]:
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()

        monkeypatch.setattr(journey, "get_db", use_db)

        first = journey.connect_public_storefront_customer(
            business_id=access.business.id,
            telegram_user_id=202,
            username="anna",
            display_name="Анна",
        )
        second = journey.connect_public_storefront_customer(
            business_id=access.business.id,
            telegram_user_id=202,
            username="anna",
            display_name="Анна",
        )

        assert second.customer_id == first.customer_id
        leads = conn.execute(
            """
            SELECT id, customer_id, source_kind, source_ref, contact_basis, stage
            FROM clientplatform_sales_leads
            WHERE business_id=?
            """,
            (access.business.id,),
        ).fetchall()
        assert len(leads) == 1
        lead = leads[0]
        assert lead["customer_id"] == first.customer_id
        assert lead["source_kind"] == "telegram"
        assert lead["source_ref"] == "public_storefront"
        assert lead["contact_basis"] == "inbound"
        assert lead["stage"] == "contacted"

        events = conn.execute(
            """
            SELECT event_type, dedupe_key, payload_json
            FROM clientplatform_sales_events
            WHERE business_id=? AND lead_id=?
            ORDER BY occurred_at, id
            """,
            (access.business.id, lead["id"]),
        ).fetchall()
        assert len(events) == 1
        assert events[0]["event_type"] == "conversation_transition"
        assert events[0]["dedupe_key"].startswith("conversation_transition:")
        payload = json.loads(events[0]["payload_json"])
        assert payload["event"] == "inbound_received"
        assert payload["from"] == "discovered"
        assert payload["to"] == "engaged"
        assert payload["metadata"] == {
            "channel": "telegram",
            "surface": "public_storefront",
        }
    finally:
        conn.close()


def test_owner_cannot_generate_sales_signal_by_opening_own_storefront(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _database()
    try:
        access = TenancyRepository(conn).create_business(
            owner_user_id=101,
            name="Практика",
        )
        conn.commit()

        @contextmanager
        def use_db() -> Iterator[sqlite3.Connection]:
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()

        monkeypatch.setattr(journey, "get_db", use_db)

        with pytest.raises(ValueError, match="публичная ссылка для клиентов"):
            journey.connect_public_storefront_customer(
                business_id=access.business.id,
                telegram_user_id=101,
                username="owner",
                display_name="Владелец",
            )

        count = conn.execute(
            "SELECT COUNT(*) FROM clientplatform_sales_leads WHERE business_id=?",
            (access.business.id,),
        ).fetchone()[0]
        assert count == 0
    finally:
        conn.close()
