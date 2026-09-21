from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from services.db.schema import (
    clientplatform_customers,
    clientplatform_external_products,
    clientplatform_tenancy,
)
from services.migrations import clientplatform_external_connector_ingress_mode_v1 as migration
from services.migrations._helpers import ensure_schema_migrations


class ExternalConnectorIngressModeMigrationTests(unittest.TestCase):
    @staticmethod
    def _legacy_conn() -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(conn)
        clientplatform_customers.ensure(conn)
        conn.execute(
            """
            CREATE TABLE external_product_connectors(
                id TEXT PRIMARY KEY,
                business_id TEXT NOT NULL,
                product_key TEXT NOT NULL,
                display_name TEXT NOT NULL,
                webhook_secret_reference TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_by_member_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                activated_at TEXT,
                disabled_at TEXT,
                revoked_at TEXT,
                last_event_at TEXT,
                last_error_at TEXT,
                last_error_code TEXT
            )
            """
        )
        ensure_schema_migrations(conn)
        return conn

    def test_sqlite_upgrade_defaults_existing_connectors_to_signed_webhook(self) -> None:
        conn = self._legacy_conn()
        conn.execute(
            """
            INSERT INTO external_product_connectors(
                id,business_id,product_key,display_name,webhook_secret_reference,status,
                created_by_member_id,created_at,updated_at
            ) VALUES('c1','b1','legacy','Legacy','secret://env/CLIENTPLATFORM_SECRET_X',
                     'active','m1','2026-09-21T00:00:00+00:00','2026-09-21T00:00:00+00:00')
            """
        )
        migration.apply(conn)
        row = conn.execute(
            "SELECT ingress_mode FROM external_product_connectors WHERE id='c1'"
        ).fetchone()
        self.assertEqual(row["ingress_mode"], "signed_webhook")
        indexes = {
            row["name"]
            for row in conn.execute(
                "PRAGMA index_list(external_product_connectors)"
            ).fetchall()
        }
        self.assertIn("idx_external_product_connectors_ingress_mode", indexes)
        migration.apply(conn)
        conn.close()

    def test_fresh_schema_has_mode_constraint(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(conn)
        clientplatform_customers.ensure(conn)
        clientplatform_external_products.ensure(conn)
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(external_product_connectors)")
        }
        self.assertIn("ingress_mode", columns)
        conn.close()

    def test_postgres_upgrade_is_additive(self) -> None:
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
        self.assertIn(
            "ADD COLUMN ingress_mode TEXT NOT NULL DEFAULT 'signed_webhook'",
            statements,
        )
        self.assertIn("idx_external_product_connectors_ingress_mode", statements)
        mark.assert_called_once_with(conn, migration.NAME)


if __name__ == "__main__":
    unittest.main()
