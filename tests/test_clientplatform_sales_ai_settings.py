from __future__ import annotations

import sqlite3
import unittest
from uuid import uuid4

from clientplatform.application.sales_ai_settings import business_sales_ai_enabled_in_conn
from clientplatform.infrastructure.sales_ai_consent_repository import SalesAIConsentRepository


class SalesAISettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE clientplatform_sales_ai_consents(
                business_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL,
                consent_target TEXT NOT NULL,
                consent_epoch INTEGER NOT NULL,
                data_mode TEXT NOT NULL,
                customer_notice_confirmed INTEGER NOT NULL,
                updated_by_member_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.business_id = str(uuid4())
        self.member_id = str(uuid4())

    def tearDown(self) -> None:
        self.conn.close()

    def test_ai_is_opt_in_bound_to_target_and_notice(self) -> None:
        target = "deepseek:https://api.deepseek.com"
        self.assertFalse(business_sales_ai_enabled_in_conn(self.conn, business_id=self.business_id, consent_target=target))
        repo = SalesAIConsentRepository(self.conn)
        repo.set(
            business_id=self.business_id,
            enabled=True,
            consent_target=target,
            data_mode="redacted",
            customer_notice_confirmed=True,
            updated_by_member_id=self.member_id,
        )
        self.assertTrue(business_sales_ai_enabled_in_conn(self.conn, business_id=self.business_id, consent_target=target))
        self.assertFalse(business_sales_ai_enabled_in_conn(self.conn, business_id=self.business_id, consent_target="openai:https://api.openai.com/v1"))

    def test_consent_epoch_increments_on_every_change(self) -> None:
        repo = SalesAIConsentRepository(self.conn)
        first = repo.set(
            business_id=self.business_id,
            enabled=True,
            consent_target="deepseek:https://api.deepseek.com",
            data_mode="redacted",
            customer_notice_confirmed=True,
            updated_by_member_id=self.member_id,
        )
        second = repo.set(
            business_id=self.business_id,
            enabled=False,
            consent_target="",
            data_mode="redacted",
            customer_notice_confirmed=False,
            updated_by_member_id=self.member_id,
        )
        self.assertGreater(second.consent_epoch, first.consent_epoch)
        self.assertFalse(second.enabled)

    def test_cloud_consent_requires_customer_notice_confirmation(self) -> None:
        repo = SalesAIConsentRepository(self.conn)
        with self.assertRaisesRegex(ValueError, "notice"):
            repo.set(
                business_id=self.business_id,
                enabled=True,
                consent_target="deepseek:https://api.deepseek.com",
                data_mode="redacted",
                customer_notice_confirmed=False,
                updated_by_member_id=self.member_id,
            )


if __name__ == "__main__":
    unittest.main()
