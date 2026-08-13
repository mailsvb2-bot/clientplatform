from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from uuid import uuid4

from clientplatform.infrastructure.sales_ai_job_repository import SalesAIJobRepository
from services.db.schema.clientplatform_sales_ai import ensure


class SalesAIJobRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute(
            """
            CREATE TABLE clientplatform_sales_leads(
                id TEXT NOT NULL,
                business_id TEXT NOT NULL,
                PRIMARY KEY(id, business_id)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE clientplatform_sales_events(
                id TEXT PRIMARY KEY,
                business_id TEXT NOT NULL,
                lead_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                dedupe_key TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            )
            """
        )
        ensure(self.conn)
        self.business_id, self.lead_id = str(uuid4()), str(uuid4())
        self.conn.execute(
            "INSERT INTO clientplatform_sales_leads(id,business_id) VALUES(?,?)",
            (self.lead_id, self.business_id),
        )
        self.repo = SalesAIJobRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_newer_provider_update_makes_older_job_stale(self) -> None:
        now = datetime(2026, 8, 8, 18, 0, tzinfo=timezone.utc)
        older = self.repo.enqueue(
            business_id=self.business_id,
            lead_id=self.lead_id,
            source_event_dedupe_key="message:9",
            source_order="9",
            now=now,
        )
        newer = self.repo.enqueue(
            business_id=self.business_id,
            lead_id=self.lead_id,
            source_event_dedupe_key="message:10",
            source_order="10",
            now=now,
        )
        self.assertTrue(self.repo.has_newer_source(older))
        self.assertFalse(self.repo.has_newer_source(newer))
        self.assertEqual(
            self.repo.get(job_id=older.id, business_id=self.business_id).status.value,
            "done",
        )
        self.assertEqual(
            self.repo.get(job_id=older.id, business_id=self.business_id).last_error_code,
            "superseded_by_newer_source",
        )
        self.assertLess(older.source_order_key, newer.source_order_key)
        self.assertGreater(older.available_at, older.created_at)
        self.assertFalse(self.repo.lock_if_latest_source(older))
        self.assertTrue(self.repo.lock_if_latest_source(newer))
        self.assertEqual(
            self.repo.latest_source_order(
                business_id=self.business_id, lead_id=self.lead_id
            ),
            newer.source_order_key,
        )

    def test_raw_message_ttl_redacts_payload_in_place(self) -> None:
        old = "2026-08-01T00:00:00+00:00"
        self.conn.execute(
            "INSERT INTO clientplatform_sales_events(id,business_id,lead_id,event_type,dedupe_key,payload_json,occurred_at) VALUES(?,?,?,?,?,?,?)",
            (str(uuid4()), self.business_id, self.lead_id, "customer_message", "message:old", '{"text":"secret"}', old),
        )
        changed = self.repo.purge_expired_raw_messages(
            raw_message_ttl_hours=24,
            now=datetime(2026, 8, 8, 18, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(changed, 1)
        payload = self.conn.execute(
            "SELECT payload_json FROM clientplatform_sales_events WHERE dedupe_key='message:old'"
        ).fetchone()[0]
        self.assertIn("redacted", payload)
        self.assertNotIn("secret", payload)


if __name__ == "__main__":
    unittest.main()
