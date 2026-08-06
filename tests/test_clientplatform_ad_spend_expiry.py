from __future__ import annotations

import json
import sqlite3
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from clientplatform.infrastructure import ad_spend_expiry_repository


NOW = datetime(2026, 8, 6, 7, 0, tzinfo=timezone.utc)


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
            authorization_expires_at TEXT NOT NULL,
            status TEXT NOT NULL,
            created_by_member_id TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_error_code TEXT,
            row_version INTEGER NOT NULL,
            PRIMARY KEY(id, business_id)
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


class AdSpendExpiryTests(unittest.TestCase):
    def test_due_unused_authorization_expires_but_active_and_future_remain(self) -> None:
        conn = _connection()
        business_id = _id()
        actor_member_id = _id()
        due_authorized = _id()
        due_active = _id()
        future_authorized = _id()
        rows = (
            (
                due_authorized,
                business_id,
                (NOW - timedelta(seconds=1)).isoformat(timespec="seconds"),
                "authorized",
                actor_member_id,
                NOW.isoformat(timespec="seconds"),
                4,
            ),
            (
                due_active,
                business_id,
                (NOW - timedelta(seconds=1)).isoformat(timespec="seconds"),
                "active",
                actor_member_id,
                NOW.isoformat(timespec="seconds"),
                7,
            ),
            (
                future_authorized,
                business_id,
                (NOW + timedelta(minutes=1)).isoformat(timespec="seconds"),
                "authorized",
                actor_member_id,
                NOW.isoformat(timespec="seconds"),
                2,
            ),
        )
        conn.executemany(
            """
            INSERT INTO ad_spend_authorizations(
                id,business_id,authorization_expires_at,status,
                created_by_member_id,updated_at,row_version
            ) VALUES(?,?,?,?,?,?,?)
            """,
            rows,
        )

        @contextmanager
        def _db():
            yield conn
            conn.commit()

        with patch.object(ad_spend_expiry_repository, "get_db", _db):
            result = (
                ad_spend_expiry_repository.expire_due_ad_spend_authorizations(
                    now=NOW,
                )
            )

        observed = {
            row["id"]: (row["status"], row["row_version"], row["last_error_code"])
            for row in conn.execute(
                """
                SELECT id,status,row_version,last_error_code
                FROM ad_spend_authorizations
                """
            ).fetchall()
        }
        audit = conn.execute(
            """
            SELECT actor_member_id,action,subject_id,details_json
            FROM ad_audit_events
            """
        ).fetchone()

        self.assertEqual(result.scanned, 1)
        self.assertEqual(result.expired, 1)
        self.assertEqual(result.lost_races, 0)
        self.assertEqual(
            observed[due_authorized],
            ("expired", 5, "authorization_expired"),
        )
        self.assertEqual(observed[due_active][0], "active")
        self.assertEqual(observed[future_authorized][0], "authorized")
        self.assertEqual(audit["actor_member_id"], actor_member_id)
        self.assertEqual(audit["action"], "ad_spend_authorization_expired")
        self.assertEqual(audit["subject_id"], due_authorized)
        self.assertEqual(
            json.loads(audit["details_json"]),
            {"previous_status": "authorized"},
        )

    def test_naive_expiry_sweep_time_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            ad_spend_expiry_repository.expire_due_ad_spend_authorizations(
                now=datetime(2026, 8, 6, 7, 0),
            )


if __name__ == "__main__":
    unittest.main()
