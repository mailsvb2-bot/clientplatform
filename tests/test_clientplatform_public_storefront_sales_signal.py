from __future__ import annotations

import importlib
import json
import sqlite3
import unittest
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import patch

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


class PublicStorefrontSalesSignalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _database()
        self.access = TenancyRepository(self.conn).create_business(
            owner_user_id=101,
            name="Практика",
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    @contextmanager
    def _use_db(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def test_public_storefront_visit_is_real_replay_safe_sales_evidence(self) -> None:
        with patch.object(journey, "get_db", self._use_db):
            first = journey.connect_public_storefront_customer(
                business_id=self.access.business.id,
                telegram_user_id=202,
                username="anna",
                display_name="Анна",
            )
            second = journey.connect_public_storefront_customer(
                business_id=self.access.business.id,
                telegram_user_id=202,
                username="anna",
                display_name="Анна",
            )

        self.assertEqual(second.customer_id, first.customer_id)
        leads = self.conn.execute(
            """
            SELECT id, customer_id, source_kind, source_ref, contact_basis, stage
            FROM clientplatform_sales_leads
            WHERE business_id=?
            """,
            (self.access.business.id,),
        ).fetchall()
        self.assertEqual(len(leads), 1)
        lead = leads[0]
        self.assertEqual(lead["customer_id"], first.customer_id)
        self.assertEqual(lead["source_kind"], "telegram")
        self.assertEqual(lead["source_ref"], "public_storefront")
        self.assertEqual(lead["contact_basis"], "inbound")
        self.assertEqual(lead["stage"], "contacted")

        events = self.conn.execute(
            """
            SELECT event_type, dedupe_key, payload_json
            FROM clientplatform_sales_events
            WHERE business_id=? AND lead_id=?
            ORDER BY occurred_at, id
            """,
            (self.access.business.id, lead["id"]),
        ).fetchall()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "conversation_transition")
        self.assertTrue(events[0]["dedupe_key"].startswith("conversation_transition:"))
        payload = json.loads(events[0]["payload_json"])
        self.assertEqual(payload["event"], "inbound_received")
        self.assertEqual(payload["from"], "discovered")
        self.assertEqual(payload["to"], "engaged")
        self.assertEqual(
            payload["metadata"],
            {"channel": "telegram", "surface": "public_storefront"},
        )

    def test_owner_cannot_generate_sales_signal_by_opening_own_storefront(self) -> None:
        with patch.object(journey, "get_db", self._use_db):
            with self.assertRaisesRegex(ValueError, "публичная ссылка для клиентов"):
                journey.connect_public_storefront_customer(
                    business_id=self.access.business.id,
                    telegram_user_id=101,
                    username="owner",
                    display_name="Владелец",
                )

        count = self.conn.execute(
            "SELECT COUNT(*) FROM clientplatform_sales_leads WHERE business_id=?",
            (self.access.business.id,),
        ).fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
