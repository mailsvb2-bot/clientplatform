from __future__ import annotations

import sqlite3
import unittest

from clientplatform.domain.creative_generation import (
    MAX_CREATIVE_GENERATION_PROVIDER_PAYLOAD_CHARS,
)
from services.migrations import (
    clientplatform_creative_generation_payload_budget_v1 as migration,
)


class CreativePayloadBudgetMigrationTests(unittest.TestCase):
    def test_sqlite_migration_expands_legacy_creative_payload_check(self) -> None:
        original = migration.is_postgres_enabled
        migration.is_postgres_enabled = lambda: False
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(
                """
                CREATE TABLE creative_generation_receipts(
                    id TEXT PRIMARY KEY,
                    provider_payload_json TEXT NOT NULL,
                    CHECK(length(provider_payload_json) BETWEEN 1 AND 10000)
                )
                """
            )
            conn.execute(
                "INSERT INTO creative_generation_receipts(id, provider_payload_json) "
                "VALUES(?, ?)",
                ("legacy", "x" * 10_000),
            )

            migration.apply(conn)

            sql = str(
                conn.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='table' AND name='creative_generation_receipts'"
                ).fetchone()["sql"]
            )
            self.assertIn(
                (
                    "CHECK(length(provider_payload_json) BETWEEN 1 AND "
                    f"{MAX_CREATIVE_GENERATION_PROVIDER_PAYLOAD_CHARS})"
                ),
                sql,
            )

            conn.execute(
                "INSERT INTO creative_generation_receipts(id, provider_payload_json) "
                "VALUES(?, ?)",
                ("expanded", "x" * (10_000 + 1)),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO creative_generation_receipts(id, provider_payload_json) "
                    "VALUES(?, ?)",
                    (
                        "too-large",
                        "x" * (MAX_CREATIVE_GENERATION_PROVIDER_PAYLOAD_CHARS + 1),
                    ),
                )
        finally:
            conn.close()
            migration.is_postgres_enabled = original


if __name__ == "__main__":
    unittest.main()
