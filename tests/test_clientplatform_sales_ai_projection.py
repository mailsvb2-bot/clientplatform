from __future__ import annotations

import sqlite3
import unittest
from uuid import uuid4

from clientplatform.infrastructure.sales_ai_analysis_repository import SalesAIAnalysisRepository
from services.db.schema.clientplatform_sales_ai import ensure


class SalesAIProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE businesses(id TEXT PRIMARY KEY)")
        self.conn.execute(
            "CREATE TABLE business_members(id TEXT NOT NULL,business_id TEXT NOT NULL,user_id INTEGER,role TEXT,status TEXT,created_at TEXT,updated_at TEXT,PRIMARY KEY(id,business_id))"
        )
        self.conn.execute(
            "CREATE TABLE clientplatform_sales_leads(id TEXT NOT NULL,business_id TEXT NOT NULL,PRIMARY KEY(id,business_id))"
        )
        ensure(self.conn)
        self.business_id, self.lead_id = str(uuid4()), str(uuid4())
        self.conn.execute("INSERT INTO clientplatform_sales_leads(id,business_id) VALUES(?,?)", (self.lead_id, self.business_id))
        self.repo = SalesAIAnalysisRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_newest_source_order_wins_even_if_written_first(self) -> None:
        newer = "00000000000000000000000000000010"
        older = "00000000000000000000000000000009"
        self.assertTrue(
            self.repo.upsert_latest(
                business_id=self.business_id,
                lead_id=self.lead_id,
                source_order_key=newer,
                source_event_dedupe_key="m:10",
                analysis={"v": 10},
                provider="deepseek",
                model="deepseek-v4-flash",
                plan_id=None,
                action_kind=None,
                verified_offer=None,
            )
        )
        self.assertFalse(
            self.repo.upsert_latest(
                business_id=self.business_id,
                lead_id=self.lead_id,
                source_order_key=older,
                source_event_dedupe_key="m:9",
                analysis={"v": 9},
                provider="deepseek",
                model="deepseek-v4-flash",
                plan_id=None,
                action_kind=None,
                verified_offer=None,
            )
        )
        self.assertEqual(self.repo.get_latest(business_id=self.business_id, lead_id=self.lead_id)["analysis"], {"v": 10})


if __name__ == "__main__":
    unittest.main()
