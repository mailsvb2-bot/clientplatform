from __future__ import annotations

import sqlite3
import unittest

from services.db.schema import clientplatform_program_media


class ProgramMediaCleanupSchemaTests(unittest.TestCase):
    def test_cleanup_queue_and_due_index_are_created_idempotently(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            clientplatform_program_media.ensure(conn)
            clientplatform_program_media.ensure(conn)
            table = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='program_media_cleanup_queue'"
            ).fetchone()
            index = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND name='idx_program_media_cleanup_due'"
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(table)
        self.assertIsNotNone(index)
        assert table is not None
        table_sql = str(table[0])
        self.assertIn("media_reference TEXT NOT NULL UNIQUE", table_sql)
        self.assertIn("status IN ('pending', 'processing', 'retry', 'dead')", table_sql)
        self.assertIn("lock_token TEXT", table_sql)
        self.assertIn("dead_at TEXT", table_sql)


if __name__ == "__main__":
    unittest.main()
