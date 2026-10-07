from __future__ import annotations

import sqlite3
import unittest

from clientplatform.domain.creative_generation import (
    MAX_CREATIVE_GENERATION_PROVIDER_PAYLOAD_CHARS,
)
from services.migrations import (
    clientplatform_creative_generation_payload_budget_v1 as migration,
)


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)


class _PostgresProbe:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executed: list[str] = []

    def execute(self, sql, params=()):
        statement = " ".join(str(sql).split())
        self.executed.append(statement)
        if statement.startswith("SELECT conname"):
            return _Rows(self.rows)
        return _Rows([])


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

            # A second application is a real production restart path and must be safe.
            migration.apply(conn)
        finally:
            conn.close()
            migration.is_postgres_enabled = original

    def test_sqlite_update_is_noop_without_receipt_table(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            migration._update_sqlite_constraint(conn)
        finally:
            conn.close()

    def test_sqlite_update_rejects_unexpected_payload_check_shape(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                """
                CREATE TABLE creative_generation_receipts(
                    id TEXT PRIMARY KEY,
                    provider_payload_json TEXT NOT NULL,
                    CHECK(length(provider_payload_json) > 0)
                )
                """
            )
            with self.assertRaisesRegex(RuntimeError, "unexpected SQLite shape"):
                migration._update_sqlite_constraint(conn)
        finally:
            conn.close()

    def test_postgres_update_replaces_only_payload_constraint(self) -> None:
        conn = _PostgresProbe(
            [
                ("other_check", "CHECK (length(request_text) > 0)"),
                (
                    "creative_generation_receipts_provider_payload_json_check",
                    "CHECK ((length(provider_payload_json) >= 1) AND "
                    "(length(provider_payload_json) <= 10000))",
                ),
            ]
        )

        migration._update_postgres_constraint(conn)

        joined = "\n".join(conn.executed)
        self.assertIn(
            'DROP CONSTRAINT "creative_generation_receipts_provider_payload_json_check"',
            joined,
        )
        self.assertIn(
            "ADD CONSTRAINT cp_creative_generation_payload_budget_v1",
            joined,
        )
        self.assertIn(
            str(MAX_CREATIVE_GENERATION_PROVIDER_PAYLOAD_CHARS),
            joined,
        )
        self.assertNotIn('DROP CONSTRAINT "other_check"', joined)

    def test_postgres_update_adds_constraint_when_legacy_check_is_absent(self) -> None:
        conn = _PostgresProbe(
            [("request_text_check", "CHECK (length(request_text) > 0)")]
        )

        migration._update_postgres_constraint(conn)

        joined = "\n".join(conn.executed)
        self.assertIn(
            "ADD CONSTRAINT cp_creative_generation_payload_budget_v1",
            joined,
        )
        self.assertNotIn("DROP CONSTRAINT", joined)

    def test_postgres_update_rejects_unsafe_constraint_name(self) -> None:
        conn = _PostgresProbe(
            [
                (
                    'bad"; DROP TABLE businesses; --',
                    "CHECK (length(provider_payload_json) <= 10000)",
                )
            ]
        )

        with self.assertRaisesRegex(RuntimeError, "unsafe creative generation"):
            migration._update_postgres_constraint(conn)


if __name__ == "__main__":
    unittest.main()
