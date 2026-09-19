from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from services.db.schema import create_or_update_tables
from services.migrations import clientplatform_event_content_video_mode_v1 as migration


class EventContentVideoModeMigrationTests(unittest.TestCase):
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
            "SELECT sql FROM sqlite_schema "
            "WHERE type='table' AND name='clientplatform_event_content_preferences'"
        ).fetchone()
        current = str(row["sql"])
        legacy = current.replace(
            "CHECK(mode IN ('text','text_with_image','text_in_image','text_with_video'))",
            "CHECK(mode IN ('text','text_with_image','text_in_image'))",
        )
        if legacy == current or "text_with_video" in legacy:
            raise AssertionError("failed to build legacy event content schema")
        version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        conn.execute("PRAGMA writable_schema=ON")
        try:
            conn.execute(
                "UPDATE sqlite_schema SET sql=? "
                "WHERE type='table' AND name='clientplatform_event_content_preferences'",
                (legacy,),
            )
            conn.execute(f"PRAGMA schema_version={version + 1}")
        finally:
            conn.execute("PRAGMA writable_schema=OFF")
        return legacy

    def test_sqlite_upgrade_adds_video_mode_and_is_idempotent(self) -> None:
        conn = self._conn()
        self._make_legacy(conn)
        self.assertNotIn("text_with_video", migration._sqlite_sql(conn) or "")
        migration._update_sqlite(conn)
        upgraded = migration._sqlite_sql(conn) or ""
        self.assertIn("text_with_video", upgraded)
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
        migration._update_sqlite(conn)
        self.assertEqual(migration._sqlite_sql(conn), upgraded)
        conn.close()

    def test_sqlite_upgrade_rejects_unexpected_shape(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE clientplatform_event_content_preferences("
            "event_id TEXT PRIMARY KEY, mode TEXT NOT NULL)"
        )
        with self.assertRaisesRegex(RuntimeError, "unexpected SQLite shape"):
            migration._update_sqlite(conn)
        conn.close()

    def test_postgres_upgrade_replaces_only_mode_constraint(self) -> None:
        conn = MagicMock()
        result = MagicMock()
        result.fetchall.return_value = [
            ("event_stage_check", "CHECK (stage = ANY (...))"),
            ("event_mode_check", "CHECK (mode = ANY (...))"),
        ]
        conn.execute.return_value = result
        migration._update_postgres(conn)
        statements = [str(call.args[0]) for call in conn.execute.call_args_list]
        drops = [statement for statement in statements if "DROP CONSTRAINT" in statement]
        self.assertEqual(len(drops), 1)
        self.assertIn('"event_mode_check"', drops[0])
        self.assertIn("cp_event_content_mode_video_v1", "\n".join(statements))
        self.assertIn("'text_with_video'", "\n".join(statements))

    def test_apply_is_idempotent(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
