from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from services.migrations import clientplatform_external_observation_lifecycle_v1 as migration
from services.migrations._helpers import ensure_schema_migrations


class ExternalObservationLifecycleMigrationTests(unittest.TestCase):
    @staticmethod
    def _legacy_conn() -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE external_product_event_receipts(
                id TEXT PRIMARY KEY,
                business_id TEXT NOT NULL,
                connector_id TEXT NOT NULL,
                external_event_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                customer_id TEXT,
                customer_fingerprint TEXT,
                payload_fingerprint TEXT NOT NULL,
                outcome_event_id TEXT,
                occurred_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'accepted'
            )
            """
        )
        ensure_schema_migrations(conn)
        return conn

    def test_sqlite_upgrade_adds_lifecycle_columns_and_indexes(self) -> None:
        conn = self._legacy_conn()
        migration.apply(conn)
        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(external_product_event_receipts)"
            ).fetchall()
        }
        self.assertTrue(
            {
                "observation_key",
                "observation_revision",
                "observation_state",
                "observation_supersedes_event_id",
                "observation_fresh_until",
            }.issubset(columns)
        )
        indexes = {
            row["name"]
            for row in conn.execute(
                "PRAGMA index_list(external_product_event_receipts)"
            ).fetchall()
        }
        self.assertIn("idx_external_product_observation_revision", indexes)
        self.assertIn("idx_external_product_observation_supersedes", indexes)
        self.assertIn("idx_external_product_observation_head", indexes)
        migration.apply(conn)
        applied = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (migration.NAME,),
        ).fetchone()[0]
        self.assertEqual(applied, 1)
        conn.close()

    def test_unique_revision_and_supersedes_indexes_fail_closed(self) -> None:
        conn = self._legacy_conn()
        migration.apply(conn)
        base = (
            "id-1",
            "business",
            "connector",
            "event-1",
            "evidence",
            "customer",
            "f" * 64,
            "p" * 64,
            None,
            "2026-09-20T10:00:00+00:00",
            "2026-09-20T10:00:00+00:00",
            "{}",
            "obs:key",
            1,
            "active",
            None,
            None,
            "accepted",
        )
        conn.execute(
            """
            INSERT INTO external_product_event_receipts(
                id,business_id,connector_id,external_event_id,event_type,
                customer_id,customer_fingerprint,payload_fingerprint,outcome_event_id,
                occurred_at,received_at,metadata_json,observation_key,
                observation_revision,observation_state,
                observation_supersedes_event_id,observation_fresh_until,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            base,
        )
        duplicate_revision = list(base)
        duplicate_revision[0] = "id-2"
        duplicate_revision[3] = "event-2"
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO external_product_event_receipts(
                    id,business_id,connector_id,external_event_id,event_type,
                    customer_id,customer_fingerprint,payload_fingerprint,outcome_event_id,
                    occurred_at,received_at,metadata_json,observation_key,
                    observation_revision,observation_state,
                    observation_supersedes_event_id,observation_fresh_until,status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                tuple(duplicate_revision),
            )
        conn.close()

    def test_postgres_upgrade_uses_additive_columns_and_partial_indexes(self) -> None:
        conn = MagicMock()
        with (
            patch.object(migration, "migration_applied", return_value=False),
            patch.object(migration, "table_exists", return_value=True),
            patch.object(migration, "is_postgres_enabled", return_value=True),
            patch.object(migration, "_column_names", return_value=set()),
            patch.object(migration, "mark_migration") as mark,
        ):
            migration.apply(conn)
        statements = "\n".join(str(call.args[0]) for call in conn.execute.call_args_list)
        self.assertIn("ADD COLUMN observation_key TEXT", statements)
        self.assertIn("idx_external_product_observation_revision", statements)
        self.assertIn("idx_external_product_observation_supersedes", statements)
        mark.assert_called_once_with(conn, migration.NAME)


if __name__ == "__main__":
    unittest.main()
