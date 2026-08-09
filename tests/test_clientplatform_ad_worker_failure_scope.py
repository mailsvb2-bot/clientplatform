from __future__ import annotations

import sqlite3
import unittest
from uuid import uuid4

from clientplatform.infrastructure.ad_credential_vault import InMemoryAdCredentialVault
from clientplatform.infrastructure.ad_worker_store import AdWorkerStore


class AdWorkerFailureScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            """
            CREATE TABLE ad_connections(
                id TEXT PRIMARY KEY,
                business_id TEXT NOT NULL,
                status TEXT NOT NULL,
                last_error_code TEXT
            )
            """
        )
        self.business_id = str(uuid4())
        self.connection_id = str(uuid4())
        self.store = AdWorkerStore(
            self.conn,
            vault=InMemoryAdCredentialVault(),
        )

    def tearDown(self) -> None:
        self.conn.close()

    def _set_attention(self, error_code: str) -> None:
        self.conn.execute("DELETE FROM ad_connections")
        self.conn.execute(
            """
            INSERT INTO ad_connections(id, business_id, status, last_error_code)
            VALUES(?, ?, 'attention', ?)
            """,
            (self.connection_id, self.business_id, error_code),
        )

    def _status(self) -> str:
        row = self.conn.execute(
            "SELECT status FROM ad_connections WHERE id=?",
            (self.connection_id,),
        ).fetchone()
        assert row is not None
        return str(row[0])

    def test_permission_failures_remain_attention(self) -> None:
        for error_code in (
            "direct_permission_denied",
            "direct_account_access_denied",
        ):
            with self.subTest(error_code=error_code):
                self._set_attention(error_code)
                self.store.keep_available_after_job_failure(
                    business_id=self.business_id,
                    connection_id=self.connection_id,
                )
                self.assertEqual(self._status(), "attention")

    def test_job_scoped_failure_can_restore_active(self) -> None:
        self._set_attention("existing_ad_is_not_draft")
        self.store.keep_available_after_job_failure(
            business_id=self.business_id,
            connection_id=self.connection_id,
        )
        self.assertEqual(self._status(), "active")


if __name__ == "__main__":
    unittest.main()
