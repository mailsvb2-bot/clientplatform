from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from services.db.schema import (
    clientplatform_customers,
    clientplatform_external_products,
    clientplatform_outcomes,
    clientplatform_tenancy,
)
from services.migrations import clientplatform_external_observation_feedback_v1 as migration
from services.migrations._helpers import ensure_schema_migrations


class ExternalObservationFeedbackMigrationTests(unittest.TestCase):
    @staticmethod
    def _legacy_conn() -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(conn)
        clientplatform_customers.ensure(conn)
        clientplatform_outcomes.ensure(conn)
        clientplatform_external_products.ensure(conn)
        conn.execute("DROP TABLE external_product_observation_feedback")
        ensure_schema_migrations(conn)
        return conn

    def test_upgrade_creates_feedback_table_and_index_idempotently(self) -> None:
        conn = self._legacy_conn()
        migration.apply(conn)
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertIn("external_product_observation_feedback", tables)
        indexes = {
            row["name"]
            for row in conn.execute(
                "PRAGMA index_list(external_product_observation_feedback)"
            ).fetchall()
        }
        self.assertIn("idx_external_observation_feedback_customer", indexes)
        migration.apply(conn)
        applied = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (migration.NAME,),
        ).fetchone()[0]
        self.assertEqual(applied, 1)
        conn.close()

    def test_migration_delegates_to_canonical_schema_owner(self) -> None:
        conn = sqlite3.connect(":memory:")
        ensure_schema_migrations(conn)
        with patch(
            "services.db.schema.clientplatform_external_products.ensure"
        ) as ensure:
            migration.apply(conn)
        ensure.assert_called_once_with(conn)
        conn.close()


if __name__ == "__main__":
    unittest.main()
