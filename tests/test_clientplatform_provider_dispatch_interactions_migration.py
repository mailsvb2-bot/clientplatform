from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from services.migrations import (
    clientplatform_provider_dispatch_interactions_v1 as migration,
)


_POST_U009_TABLE = """
CREATE TABLE provider_dispatch_outbox(
    id TEXT PRIMARY KEY, business_id TEXT NOT NULL, platform TEXT NOT NULL,
    source_kind TEXT NOT NULL, source_id TEXT NOT NULL,
    logical_delivery_id TEXT, partner_campaign_id TEXT, partner_candidate_id TEXT,
    sales_followup_id TEXT, connection_id TEXT NOT NULL,
    recipient_kind TEXT NOT NULL, customer_identity_id TEXT,
    external_subject TEXT NOT NULL, payload_kind TEXT NOT NULL,
    payload_ref TEXT NOT NULL, idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL, attempts INTEGER NOT NULL,
    available_at TEXT NOT NULL, locked_at TEXT, lock_token TEXT,
    provider_message_id TEXT, last_error TEXT, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, sent_at TEXT, dead_at TEXT,
    CHECK(source_kind IN ('lesson_delivery','partner_outreach','sales_followup'))
)
"""


def _legacy_row() -> tuple[object, ...]:
    return (
        "dispatch-1", "business-1", "telegram", "sales_followup", "followup-1",
        None, None, None, "followup-1", "connection-1", "external_subject",
        None, "700001", "text", "hello", "legacy-key", "pending", 0,
        "2026-08-20T10:00:00+00:00", None, None, None, None,
        "2026-08-20T08:00:00+00:00", "2026-08-20T08:00:00+00:00", None, None,
    )


def test_sqlite_upgrade_preserves_u009_work_and_adds_customer_interaction() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(_POST_U009_TABLE)
    conn.execute(
        "INSERT INTO provider_dispatch_outbox VALUES(" + ",".join("?" for _ in range(27)) + ")",
        _legacy_row(),
    )

    with patch.object(migration, "is_postgres_enabled", return_value=False):
        migration.apply(conn)

    row = conn.execute(
        "SELECT source_kind,source_id,sales_followup_id,payload_ref "
        "FROM provider_dispatch_outbox WHERE id='dispatch-1'"
    ).fetchone()
    assert dict(row) == {
        "source_kind": "sales_followup",
        "source_id": "followup-1",
        "sales_followup_id": "followup-1",
        "payload_ref": "hello",
    }
    sql = str(
        conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='provider_dispatch_outbox'"
        ).fetchone()["sql"]
    )
    assert "customer_interaction" in sql

    with patch.object(migration, "is_postgres_enabled", return_value=False):
        migration.apply(conn)

    assert conn.execute(
        "SELECT COUNT(*) FROM provider_dispatch_outbox WHERE id='dispatch-1'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
        (migration.NAME,),
    ).fetchone()[0] == 1
    conn.close()


class ProviderDispatchInteractionMigrationBranchTests(unittest.TestCase):
    def test_sqlite_rebuild_creates_missing_table_and_reuses_current_shape(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row

        migration._rebuild_sqlite(conn)
        migration._rebuild_sqlite(conn)

        sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='provider_dispatch_outbox'"
            ).fetchone()["sql"]
        )
        self.assertIn("customer_interaction", sql)
        conn.close()

    def test_postgres_check_cleanup_keeps_unrelated_and_rejects_unsafe_names(self) -> None:
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [
            {
                "conname": "provider_dispatch_status_check",
                "definition": "CHECK(status IN ('pending','sent'))",
            },
            {
                "conname": "provider_dispatch_source_kind_check",
                "definition": "CHECK(source_kind IN ('lesson_delivery'))",
            },
            (
                "provider_dispatch_source_shape_check",
                "CHECK(source_kind='lesson_delivery')",
            ),
        ]

        migration._drop_source_checks_postgres(conn)

        statements = [str(call.args[0]) for call in conn.execute.call_args_list]
        self.assertEqual(
            [statement for statement in statements if statement.startswith("ALTER TABLE")],
            [
                'ALTER TABLE provider_dispatch_outbox DROP CONSTRAINT "provider_dispatch_source_kind_check"',
                'ALTER TABLE provider_dispatch_outbox DROP CONSTRAINT "provider_dispatch_source_shape_check"',
            ],
        )

        unsafe = MagicMock()
        unsafe.execute.return_value.fetchall.return_value = [
            ("unsafe-name;drop", "CHECK(source_kind='lesson_delivery')")
        ]
        with self.assertRaisesRegex(RuntimeError, "unsafe provider dispatch"):
            migration._drop_source_checks_postgres(unsafe)

    def test_apply_selects_postgres_constraint_upgrade_and_records_marker(self) -> None:
        conn = sqlite3.connect(":memory:")
        with (
            patch.object(migration, "is_postgres_enabled", return_value=True),
            patch.object(migration, "_update_postgres") as update,
        ):
            migration.apply(conn)

        update.assert_called_once_with(conn)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
                (migration.NAME,),
            ).fetchone()[0],
            1,
        )
        conn.close()
