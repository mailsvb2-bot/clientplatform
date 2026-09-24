from __future__ import annotations

import json
import sqlite3
import unittest
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import patch

from clientplatform.application import public_business_entry as public_entry
from clientplatform.domain.customers import CustomerIdentityConflict, CustomerPlatform
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.customer_repository import CustomerRepository
from services.db.schema import (
    clientplatform_activity,
    clientplatform_customers,
    clientplatform_offer_ladders,
    clientplatform_sales,
    clientplatform_tenancy,
)


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    clientplatform_tenancy.ensure(conn)
    clientplatform_customers.ensure(conn)
    clientplatform_activity.ensure(conn)
    clientplatform_sales.ensure(conn)
    clientplatform_offer_ladders.ensure(conn)
    return conn


class PublicBusinessEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _database()
        self.access = TenancyRepository(self.conn).create_business(
            owner_user_id=101,
            name="Практика",
        )
        self.actor = TenancyRepository(self.conn).resolve_context(
            user_id=101,
            business_id=self.access.business.id,
        )
        self.conn.commit()
        self.token = public_entry.uuid_token(self.access.business.id)

    def tearDown(self) -> None:
        self.conn.close()

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        yield self.conn
        self.conn.commit()

    def _patch_db(self):
        return (
            patch.object(public_entry, "get_db", self._db),
            patch.object(public_entry, "get_db_ro", self._db),
        )

    def test_public_url_is_stable_and_https_only(self) -> None:
        url = public_entry.public_business_entry_url(
            public_base_url="https://clientplatform.example.test/",
            business_id=self.access.business.id,
        )
        self.assertEqual(
            url,
            f"https://clientplatform.example.test/clientplatform/b/{self.token}",
        )
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            public_entry.public_business_entry_url(
                public_base_url="http://clientplatform.example.test",
                business_id=self.access.business.id,
            )

    def test_web_submission_reuses_customer_and_sales_lead(self) -> None:
        with self._patch_db()[0], self._patch_db()[1]:
            first = public_entry.capture_public_business_lead(
                business_token=self.token,
                display_name="Анна",
                email="Anna@Example.Test",
                phone="+7 (999) 111-22-33",
                source="landing",
                campaign_ref="campaign-42",
            )
            second = public_entry.capture_public_business_lead(
                business_token=self.token,
                display_name="Анна",
                email="anna@example.test",
                phone="+7 999 111 22 33",
                source="landing",
                campaign_ref="campaign-42",
            )

        self.assertEqual(first.customer_id, second.customer_id)
        self.assertEqual(first.lead_id, second.lead_id)
        customers = self.conn.execute(
            "SELECT COUNT(*) FROM customers WHERE business_id=?",
            (self.access.business.id,),
        ).fetchone()[0]
        self.assertEqual(customers, 1)
        identities = self.conn.execute(
            """
            SELECT platform, external_subject
            FROM customer_identities
            WHERE business_id=? AND customer_id=? AND status='active'
            ORDER BY platform
            """,
            (self.access.business.id, first.customer_id),
        ).fetchall()
        self.assertEqual(
            [(row["platform"], row["external_subject"]) for row in identities],
            [("email", "anna@example.test"), ("phone", "79991112233")],
        )

        lead = self.conn.execute(
            """
            SELECT source_kind, source_ref, contact_basis
            FROM clientplatform_sales_leads
            WHERE id=?
            """,
            (first.lead_id,),
        ).fetchone()
        self.assertEqual(lead["source_kind"], "website")
        self.assertEqual(lead["source_ref"], "public_business_entry")
        self.assertEqual(lead["contact_basis"], "inbound")

        events = self.conn.execute(
            """
            SELECT payload_json
            FROM clientplatform_sales_events
            WHERE business_id=? AND lead_id=? AND event_type='conversation_transition'
            """,
            (self.access.business.id, first.lead_id),
        ).fetchall()
        self.assertEqual(len(events), 1)
        payload = json.loads(events[0]["payload_json"])
        self.assertEqual(payload["metadata"]["channel"], "website")
        self.assertEqual(payload["metadata"]["surface"], "public_business_entry")
        self.assertEqual(payload["metadata"]["source"], "landing")
        self.assertEqual(payload["metadata"]["campaign_ref"], "campaign-42")

    def test_conflicting_email_and_phone_fail_closed_without_merge(self) -> None:
        customers = CustomerRepository(self.conn)
        first = customers.create_customer(actor=self.actor, display_name="Первый")
        customers.attach_identity(
            actor=self.actor,
            customer_id=first.id,
            platform=CustomerPlatform.EMAIL,
            external_subject="one@example.test",
        )
        second = customers.create_customer(actor=self.actor, display_name="Второй")
        customers.attach_identity(
            actor=self.actor,
            customer_id=second.id,
            platform=CustomerPlatform.PHONE,
            external_subject="+7 900 000 00 00",
        )
        self.conn.commit()

        with self._patch_db()[0], self._patch_db()[1]:
            with self.assertRaises(CustomerIdentityConflict):
                public_entry.capture_public_business_lead(
                    business_token=self.token,
                    display_name="Конфликт",
                    email="one@example.test",
                    phone="+7 900 000 00 00",
                )

        count = self.conn.execute(
            "SELECT COUNT(*) FROM customers WHERE business_id=?",
            (self.access.business.id,),
        ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_submission_requires_name_and_contact(self) -> None:
        with self._patch_db()[0], self._patch_db()[1]:
            with self.assertRaisesRegex(ValueError, "display_name"):
                public_entry.capture_public_business_lead(
                    business_token=self.token,
                    display_name="",
                    email="person@example.test",
                    phone=None,
                )
            with self.assertRaisesRegex(ValueError, "email or phone"):
                public_entry.capture_public_business_lead(
                    business_token=self.token,
                    display_name="Анна",
                    email=None,
                    phone=None,
                )


if __name__ == "__main__":
    unittest.main()
