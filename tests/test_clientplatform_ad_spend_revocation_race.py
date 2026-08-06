from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from uuid import uuid4

from clientplatform.domain.ad_spend_operations import (
    AdSpendOperation,
    AdSpendOperationStatus,
    AdSpendOperationType,
    ad_spend_operation_key,
)
from clientplatform.infrastructure.ad_spend_operation_supersession import (
    complete_superseded_launch,
)
from clientplatform.infrastructure.ad_spend_revocation_repository import (
    queue_stop_for_revoked_live_authorization,
)


NOW = datetime(2026, 8, 5, 19, 0, tzinfo=timezone.utc)


def _id() -> str:
    return str(uuid4())


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE ad_spend_authorizations(
            id TEXT NOT NULL,
            business_id TEXT NOT NULL,
            publication_job_id TEXT NOT NULL,
            status TEXT NOT NULL,
            row_version INTEGER NOT NULL,
            consent_receipt_id TEXT,
            updated_at TEXT,
            PRIMARY KEY(id, business_id)
        );
        CREATE TABLE ad_publication_jobs(
            id TEXT NOT NULL,
            business_id TEXT NOT NULL,
            connection_id TEXT NOT NULL,
            external_ad_id TEXT,
            PRIMARY KEY(id, business_id)
        );
        CREATE TABLE ad_connections(
            id TEXT NOT NULL,
            business_id TEXT NOT NULL,
            status TEXT NOT NULL,
            PRIMARY KEY(id, business_id)
        );
        CREATE TABLE ad_spend_operations(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            authorization_id TEXT NOT NULL,
            operation_type TEXT NOT NULL,
            status TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            attempts INTEGER NOT NULL,
            available_at TEXT NOT NULL,
            locked_at TEXT,
            lock_token TEXT,
            last_error_code TEXT,
            provider_evidence_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            dead_at TEXT,
            UNIQUE(business_id, idempotency_key)
        );
        CREATE TABLE ad_audit_events(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            actor_member_id TEXT NOT NULL,
            action TEXT NOT NULL,
            subject_type TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            details_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    return conn


class RevocationStopTests(unittest.TestCase):
    def test_live_revocation_queues_stop_and_moves_to_stopping_atomically(self) -> None:
        conn = _connection()
        business_id = _id()
        authorization_id = _id()
        publication_job_id = _id()
        connection_id = _id()
        actor_member_id = _id()
        conn.execute(
            """
            INSERT INTO ad_connections(id,business_id,status)
            VALUES(?,?,'active')
            """,
            (connection_id, business_id),
        )
        conn.execute(
            """
            INSERT INTO ad_publication_jobs(
                id,business_id,connection_id,external_ad_id
            ) VALUES(?,?,?,'77')
            """,
            (publication_job_id, business_id, connection_id),
        )
        conn.execute(
            """
            INSERT INTO ad_spend_authorizations(
                id,business_id,publication_job_id,status,row_version,
                consent_receipt_id,updated_at
            ) VALUES(?,?,?,'revoked',4,?,?)
            """,
            (
                authorization_id,
                business_id,
                publication_job_id,
                _id(),
                NOW.isoformat(),
            ),
        )

        operation_id = queue_stop_for_revoked_live_authorization(
            conn,
            business_id=business_id,
            authorization_id=authorization_id,
            actor_member_id=actor_member_id,
            now=NOW,
        )

        authorization = conn.execute(
            """
            SELECT status,row_version
            FROM ad_spend_authorizations
            WHERE id=? AND business_id=?
            """,
            (authorization_id, business_id),
        ).fetchone()
        operation = conn.execute(
            """
            SELECT id,status,operation_type
            FROM ad_spend_operations
            WHERE business_id=? AND authorization_id=?
            """,
            (business_id, authorization_id),
        ).fetchone()
        audit = conn.execute(
            "SELECT details_json FROM ad_audit_events"
        ).fetchone()

        self.assertEqual(authorization["status"], "stopping")
        self.assertEqual(authorization["row_version"], 5)
        self.assertEqual(operation["id"], operation_id)
        self.assertEqual(operation["status"], "queued")
        self.assertEqual(operation["operation_type"], "stop")
        self.assertEqual(json.loads(audit["details_json"])["operation_id"], operation_id)


class LaunchSupersessionTests(unittest.TestCase):
    def test_stop_state_wins_without_being_overwritten_by_launch_completion(self) -> None:
        conn = _connection()
        business_id = _id()
        authorization_id = _id()
        operation_id = _id()
        lock_token = _id()
        key = ad_spend_operation_key(
            business_id=business_id,
            authorization_id=authorization_id,
            operation_type=AdSpendOperationType.LAUNCH,
        )
        conn.execute(
            """
            INSERT INTO ad_spend_authorizations(
                id,business_id,publication_job_id,status,row_version,
                consent_receipt_id,updated_at
            ) VALUES(?,?,?,'stopping',5,?,?)
            """,
            (
                authorization_id,
                business_id,
                _id(),
                _id(),
                NOW.isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO ad_spend_operations(
                id,business_id,authorization_id,operation_type,status,
                idempotency_key,attempts,available_at,locked_at,lock_token,
                provider_evidence_json,created_at,updated_at
            ) VALUES(?,?,?,'launch','processing',?,1,?,?,?,?,?,?)
            """,
            (
                operation_id,
                business_id,
                authorization_id,
                key,
                NOW.isoformat(),
                NOW.isoformat(),
                lock_token,
                "{}",
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        operation = AdSpendOperation(
            id=operation_id,
            business_id=business_id,
            authorization_id=authorization_id,
            operation_type=AdSpendOperationType.LAUNCH,
            status=AdSpendOperationStatus.PROCESSING,
            idempotency_key=key,
            attempts=1,
            available_at=NOW.isoformat(),
            created_at=NOW.isoformat(),
            updated_at=NOW.isoformat(),
            locked_at=NOW.isoformat(),
            lock_token=lock_token,
        )

        completed = complete_superseded_launch(
            conn,
            operation=operation,
            provider_evidence={
                "operation": "moderate",
                "provider_outcome_unknown": True,
            },
            now=NOW,
        )

        status = conn.execute(
            """
            SELECT status
            FROM ad_spend_authorizations
            WHERE id=? AND business_id=?
            """,
            (authorization_id, business_id),
        ).fetchone()["status"]
        evidence = conn.execute(
            """
            SELECT provider_evidence_json
            FROM ad_spend_operations
            WHERE id=?
            """,
            (operation_id,),
        ).fetchone()["provider_evidence_json"]

        self.assertEqual(completed.status, AdSpendOperationStatus.SUCCEEDED)
        self.assertEqual(status, "stopping")
        self.assertTrue(json.loads(evidence)["superseded_by_stop"])


if __name__ == "__main__":
    unittest.main()
