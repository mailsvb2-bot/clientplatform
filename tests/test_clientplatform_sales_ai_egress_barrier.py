from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from uuid import uuid4

from clientplatform.infrastructure.sales_ai_consent_repository import SalesAIConsentRepository
from services.db.schema.clientplatform_sales_ai import ensure


class SalesAIEgressBarrierTests(unittest.TestCase):
    def test_disable_waits_for_active_egress_row_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ai.db"
            business_id, member_id = str(uuid4()), str(uuid4())
            setup = sqlite3.connect(db_path)
            setup.execute("PRAGMA foreign_keys=ON")
            setup.execute("CREATE TABLE businesses(id TEXT PRIMARY KEY)")
            setup.execute(
                "CREATE TABLE business_members(id TEXT NOT NULL,business_id TEXT NOT NULL,user_id INTEGER,role TEXT,status TEXT,created_at TEXT,updated_at TEXT,PRIMARY KEY(id,business_id))"
            )
            setup.execute(
                "CREATE TABLE clientplatform_sales_leads(id TEXT NOT NULL,business_id TEXT NOT NULL,PRIMARY KEY(id,business_id))"
            )
            setup.execute("INSERT INTO businesses(id) VALUES(?)", (business_id,))
            setup.execute(
                "INSERT INTO business_members(id,business_id,user_id,role,status,created_at,updated_at) VALUES(?,?,1,'owner','active','now','now')",
                (member_id, business_id),
            )
            ensure(setup)
            SalesAIConsentRepository(setup).set(
                business_id=business_id,
                enabled=True,
                consent_target="deepseek:https://api.deepseek.com",
                data_mode="redacted",
                customer_notice_confirmed=True,
                updated_by_member_id=member_id,
            )
            setup.commit()
            setup.close()

            egress = sqlite3.connect(db_path, timeout=2.0, check_same_thread=False)
            toggle = sqlite3.connect(db_path, timeout=2.0, check_same_thread=False)
            started = threading.Event()
            finished = threading.Event()

            SalesAIConsentRepository(egress).lock_valid_consent(
                business_id=business_id,
                consent_target="deepseek:https://api.deepseek.com",
            )

            def disable() -> None:
                started.set()
                SalesAIConsentRepository(toggle).set(
                    business_id=business_id,
                    enabled=False,
                    consent_target="",
                    data_mode="redacted",
                    customer_notice_confirmed=False,
                    updated_by_member_id=member_id,
                )
                toggle.commit()
                finished.set()

            thread = threading.Thread(target=disable)
            thread.start()
            self.assertTrue(started.wait(0.5))
            time.sleep(0.1)
            self.assertFalse(finished.is_set(), "disable must wait while egress permit holds consent row")
            egress.commit()
            self.assertTrue(finished.wait(1.0))
            thread.join(timeout=1.0)
            egress.close()
            toggle.close()


if __name__ == "__main__":
    unittest.main()
