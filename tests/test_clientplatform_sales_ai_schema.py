from __future__ import annotations

import sqlite3
import unittest
from uuid import uuid4

from services.db.schema.clientplatform_sales_ai import ensure


class SalesAISchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
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
        ensure(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_queue_is_idempotent_per_source_event(self) -> None:
        business_id, lead_id = str(uuid4()), str(uuid4())
        self.conn.execute(
            "INSERT INTO clientplatform_sales_leads(id,business_id) VALUES(?,?)",
            (lead_id, business_id),
        )
        args = (
            str(uuid4()),
            business_id,
            lead_id,
            "managed-bot-message:1",
            "00000000000000000000000000000001",
            "2026-08-08T18:00:00+00:00",
            "2026-08-08T18:00:00+00:00",
            "2026-08-08T18:00:00+00:00",
        )
        self.conn.execute(
            """
            INSERT INTO clientplatform_sales_ai_jobs(
                id,business_id,lead_id,source_event_dedupe_key,source_order_key,status,attempts,
                available_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,'pending',0,?,?,?)
            """,
            args,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO clientplatform_sales_ai_jobs(
                    id,business_id,lead_id,source_event_dedupe_key,source_order_key,status,attempts,
                    available_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,'pending',0,?,?,?)
                """,
                (str(uuid4()), *args[1:]),
            )

    def test_head_is_unique_and_tenant_bound(self) -> None:
        business_id, lead_id = str(uuid4()), str(uuid4())
        self.conn.execute(
            "INSERT INTO clientplatform_sales_leads(id,business_id) VALUES(?,?)",
            (lead_id, business_id),
        )
        self.conn.execute(
            "INSERT INTO clientplatform_sales_ai_heads(business_id,lead_id,latest_source_order_key,updated_at) VALUES(?,?,?,?)",
            (business_id, lead_id, "00000000000000000000000000000001", "now"),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO clientplatform_sales_ai_heads(business_id,lead_id,latest_source_order_key,updated_at) VALUES(?,?,?,?)",
                (business_id, lead_id, "00000000000000000000000000000002", "now"),
            )

    def test_queue_rejects_cross_tenant_lead_reference(self) -> None:
        business_a, business_b, lead_id = str(uuid4()), str(uuid4()), str(uuid4())
        self.conn.execute(
            "INSERT INTO clientplatform_sales_leads(id,business_id) VALUES(?,?)",
            (lead_id, business_a),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO clientplatform_sales_ai_jobs(
                    id,business_id,lead_id,source_event_dedupe_key,source_order_key,status,attempts,
                    available_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,'pending',0,?,?,?)
                """,
                (
                    str(uuid4()), business_b, lead_id, "message:1",
                    "00000000000000000000000000000001",
                    "2026-08-08T18:00:00+00:00",
                    "2026-08-08T18:00:00+00:00",
                    "2026-08-08T18:00:00+00:00",
                ),
            )


if __name__ == "__main__":
    unittest.main()
