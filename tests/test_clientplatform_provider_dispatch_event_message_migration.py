from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from services.db.schema import create_or_update_tables
from services.migrations import clientplatform_provider_dispatch_event_message_v1 as migration


class EventMessageMigrationTests(unittest.TestCase):
    @staticmethod
    def _conn() -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        create_or_update_tables(conn)
        return conn

    @staticmethod
    def _make_legacy(conn: sqlite3.Connection) -> str:
        row = conn.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='provider_dispatch_outbox'"
        ).fetchone()
        current = str(row["sql"])
        legacy = current.replace(
            "'lesson_delivery', 'partner_outreach', 'sales_followup', 'customer_interaction', 'member_interaction', 'event_message'",
            "'lesson_delivery', 'partner_outreach', 'sales_followup', 'customer_interaction', 'member_interaction'",
        ).replace(
            "source_kind IN ('customer_interaction','member_interaction','event_message')",
            "source_kind IN ('customer_interaction','member_interaction')",
        )
        if legacy == current or "event_message" in legacy:
            raise AssertionError("failed to build legacy provider dispatch schema")
        version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        conn.execute("PRAGMA writable_schema=ON")
        try:
            conn.execute(
                "UPDATE sqlite_schema SET sql=? WHERE type='table' AND name='provider_dispatch_outbox'",
                (legacy,),
            )
            conn.execute(f"PRAGMA schema_version={version + 1}")
        finally:
            conn.execute("PRAGMA writable_schema=OFF")
        return legacy

    def test_sqlite_upgrade_adds_event_message_and_is_idempotent(self) -> None:
        conn = self._conn()
        self._make_legacy(conn)
        self.assertNotIn("event_message", migration._sqlite_sql(conn) or "")

        migration._update_sqlite(conn)
        upgraded = migration._sqlite_sql(conn) or ""
        self.assertIn("event_message", upgraded)
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")

        migration._update_sqlite(conn)
        self.assertEqual(migration._sqlite_sql(conn), upgraded)
        conn.close()

    def test_sqlite_upgrade_rejects_unexpected_shape(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE provider_dispatch_outbox(id TEXT PRIMARY KEY, source_kind TEXT)")
        with self.assertRaisesRegex(RuntimeError, "unexpected SQLite shape"):
            migration._update_sqlite(conn)
        conn.close()

    def test_sqlite_upgrade_creates_missing_table(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.assertIsNone(migration._sqlite_sql(conn))
        with patch.object(migration, "_sqlite_sql", return_value=None), patch(
            "services.db.schema.clientplatform_provider_dispatch.ensure"
        ) as ensure:
            migration._update_sqlite(conn)
        ensure.assert_called_once_with(conn)
        conn.close()

    def test_postgres_upgrade_drops_only_source_constraints_and_adds_event_constraints(self) -> None:
        conn = MagicMock()
        result = MagicMock()
        result.fetchall.return_value = [
            ("provider_dispatch_status_check", "CHECK (status IN ('pending','sent'))"),
            ("provider_dispatch_source_kind_check", "CHECK (source_kind IN ('lesson_delivery'))"),
            ("provider_dispatch_source_shape_check", "CHECK (source_kind='lesson_delivery')"),
        ]
        conn.execute.return_value = result

        migration._update_postgres(conn)

        sql = [str(call.args[0]) for call in conn.execute.call_args_list]
        drops = [statement for statement in sql if "DROP CONSTRAINT" in statement]
        self.assertEqual(len(drops), 2)
        self.assertTrue(any('"provider_dispatch_source_kind_check"' in statement for statement in drops))
        self.assertTrue(any('"provider_dispatch_source_shape_check"' in statement for statement in drops))
        self.assertFalse(any("status_check" in statement for statement in drops))
        additions = "\n".join(sql)
        self.assertIn("cp_provider_dispatch_source_kind_event_v1", additions)
        self.assertIn("cp_provider_dispatch_source_shape_event_v1", additions)
        self.assertIn("'event_message'", additions)

    def test_postgres_upgrade_rejects_unsafe_constraint_name(self) -> None:
        conn = MagicMock()
        result = MagicMock()
        result.fetchall.return_value = [("bad-name;drop", "CHECK (source_kind='x')")]
        conn.execute.return_value = result
        with self.assertRaisesRegex(RuntimeError, "unsafe provider dispatch constraint name"):
            migration._drop_source_checks_postgres(conn)

    def test_apply_routes_backend_and_marks_once(self) -> None:
        conn = MagicMock()
        with (
            patch.object(migration, "migration_applied", return_value=False),
            patch.object(migration, "is_postgres_enabled", return_value=True),
            patch.object(migration, "_update_postgres") as update,
            patch.object(migration, "mark_migration") as mark,
        ):
            migration.apply(conn)
        update.assert_called_once_with(conn)
        mark.assert_called_once_with(conn, migration.NAME)

        with (
            patch.object(migration, "migration_applied", return_value=True),
            patch.object(migration, "_update_postgres") as update,
            patch.object(migration, "_update_sqlite") as sqlite_update,
            patch.object(migration, "mark_migration") as mark,
        ):
            migration.apply(conn)
        update.assert_not_called()
        sqlite_update.assert_not_called()
        mark.assert_not_called()


if __name__ == "__main__":
    unittest.main()
