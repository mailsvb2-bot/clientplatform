from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from uuid import uuid4

from clientplatform.domain.connections import (
    ClaimedDispatch,
    ConnectionPlatform,
    Dispatch,
    DispatchStatus,
)
from clientplatform.domain.programs import ContentKind
from clientplatform.infrastructure.safe_dispatch_outbox import (
    DispatchOutboxRepository,
    mark_non_replay_safe_dispatch_boundary,
)


class MaxAmbiguousDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE delivery_dispatch_outbox(
                id TEXT PRIMARY KEY,
                business_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                logical_delivery_id TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                customer_identity_id TEXT NOT NULL,
                payload_kind TEXT NOT NULL,
                payload_ref TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                available_at TEXT NOT NULL,
                locked_at TEXT,
                lock_token TEXT,
                provider_message_id TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                sent_at TEXT,
                dead_at TEXT
            );
            CREATE TABLE lesson_deliveries(
                id TEXT PRIMARY KEY,
                business_id TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                failed_at TEXT,
                last_error TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE connections(
                id TEXT PRIMARY KEY,
                business_id TEXT NOT NULL,
                status TEXT NOT NULL,
                last_error_at TEXT,
                last_error_code TEXT,
                updated_at TEXT NOT NULL
            );
            """
        )

    def tearDown(self) -> None:
        self.conn.close()

    def _claimed(self) -> ClaimedDispatch:
        business_id = str(uuid4())
        dispatch_id = str(uuid4())
        logical_delivery_id = str(uuid4())
        connection_id = str(uuid4())
        identity_id = str(uuid4())
        lock_token = uuid4().hex
        locked_at = "2026-08-16T00:00:00+00:00"
        self.conn.execute(
            """
            INSERT INTO delivery_dispatch_outbox(
                id,business_id,platform,logical_delivery_id,connection_id,
                customer_identity_id,payload_kind,payload_ref,idempotency_key,
                status,attempts,available_at,locked_at,lock_token,
                provider_message_id,last_error,created_at,updated_at,sent_at,dead_at
            ) VALUES(?,?, 'max', ?,?,?, 'text','hello','delivery:max:1',
                     'sending',0,?,?,?,NULL,NULL,?,?,NULL,NULL)
            """,
            (
                dispatch_id,
                business_id,
                logical_delivery_id,
                connection_id,
                identity_id,
                locked_at,
                locked_at,
                lock_token,
                locked_at,
                locked_at,
            ),
        )
        self.conn.execute(
            """
            INSERT INTO lesson_deliveries(
                id,business_id,status,attempts,failed_at,last_error,updated_at
            ) VALUES(?,?,'pending',0,NULL,NULL,?)
            """,
            (logical_delivery_id, business_id, locked_at),
        )
        self.conn.execute(
            """
            INSERT INTO connections(
                id,business_id,status,last_error_at,last_error_code,updated_at
            ) VALUES(?,?,'active',NULL,NULL,?)
            """,
            (connection_id, business_id, locked_at),
        )
        return ClaimedDispatch(
            dispatch=Dispatch(
                id=dispatch_id,
                business_id=business_id,
                platform=ConnectionPlatform.MAX,
                logical_delivery_id=logical_delivery_id,
                connection_id=connection_id,
                customer_identity_id=identity_id,
                payload_kind=ContentKind.TEXT,
                payload_ref="hello",
                idempotency_key="delivery:max:1",
                status=DispatchStatus.SENDING,
                attempts=0,
                available_at=locked_at,
                locked_at=locked_at,
                lock_token=lock_token,
                created_at=locked_at,
                updated_at=locked_at,
            ),
            external_subject="max-user-1",
            credential_reference="secret://env/MAX_TOKEN",
        )

    def test_stale_marked_max_send_is_quarantined_instead_of_replayed(self) -> None:
        item = self._claimed()
        mark_non_replay_safe_dispatch_boundary(self.conn, item)
        marker = self.conn.execute(
            "SELECT last_error FROM delivery_dispatch_outbox WHERE id=?",
            (item.dispatch.id,),
        ).fetchone()["last_error"]
        self.assertEqual("max_provider_call_started_non_idempotent", marker)

        repository = DispatchOutboxRepository(self.conn)
        quarantined = repository._quarantine_stale_max_boundaries(
            lock_ttl_seconds=60,
            now=datetime(2026, 8, 16, 0, 2, tzinfo=timezone.utc),
        )
        self.assertEqual(1, quarantined)

        dispatch = self.conn.execute(
            """
            SELECT status,lock_token,dead_at,last_error,attempts
            FROM delivery_dispatch_outbox WHERE id=?
            """,
            (item.dispatch.id,),
        ).fetchone()
        self.assertEqual("dead", dispatch["status"])
        self.assertIsNone(dispatch["lock_token"])
        self.assertIsNotNone(dispatch["dead_at"])
        self.assertIn("ambiguous", str(dispatch["last_error"]))
        self.assertEqual(1, int(dispatch["attempts"]))

        lesson = self.conn.execute(
            "SELECT status,last_error FROM lesson_deliveries WHERE id=?",
            (item.dispatch.logical_delivery_id,),
        ).fetchone()
        self.assertEqual("failed", lesson["status"])
        self.assertIn("manual_reconciliation", str(lesson["last_error"]))

        connection = self.conn.execute(
            "SELECT status,last_error_code FROM connections WHERE id=?",
            (item.dispatch.connection_id,),
        ).fetchone()
        self.assertEqual("attention", connection["status"])
        self.assertIn("ambiguous", str(connection["last_error_code"]))


if __name__ == "__main__":
    unittest.main()
