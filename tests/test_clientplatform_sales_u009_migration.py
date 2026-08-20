from __future__ import annotations

import sqlite3
from unittest.mock import patch

from services.migrations import clientplatform_provider_dispatch_sales_followup_v1 as migration


_OLD_TABLE = """
CREATE TABLE provider_dispatch_outbox(
    id TEXT PRIMARY KEY, business_id TEXT NOT NULL, platform TEXT NOT NULL,
    source_kind TEXT NOT NULL, source_id TEXT NOT NULL,
    logical_delivery_id TEXT, partner_campaign_id TEXT, partner_candidate_id TEXT,
    connection_id TEXT NOT NULL, recipient_kind TEXT NOT NULL,
    customer_identity_id TEXT, external_subject TEXT NOT NULL,
    payload_kind TEXT NOT NULL, payload_ref TEXT NOT NULL,
    idempotency_key TEXT NOT NULL, status TEXT NOT NULL,
    attempts INTEGER NOT NULL, available_at TEXT NOT NULL,
    locked_at TEXT, lock_token TEXT, provider_message_id TEXT, last_error TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, sent_at TEXT, dead_at TEXT
)
"""


def _old_row() -> tuple[object, ...]:
    return (
        "dispatch-1", "business-1", "telegram", "partner_outreach", "candidate-1",
        None, "campaign-1", "candidate-1", "connection-1", "external_subject",
        None, "700001", "text", "hello", "old-key", "pending", 0,
        "2026-08-20T10:00:00+00:00", None, None, None, None,
        "2026-08-20T08:00:00+00:00", "2026-08-20T08:00:00+00:00", None, None,
    )


def test_sqlite_upgrade_preserves_old_provider_work_and_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(_OLD_TABLE)
    conn.execute(
        "INSERT INTO provider_dispatch_outbox VALUES(" + ",".join("?" for _ in range(26)) + ")",
        _old_row(),
    )
    with patch.object(migration, "is_postgres_enabled", return_value=False):
        migration.apply(conn)

    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(provider_dispatch_outbox)").fetchall()
    }
    assert "sales_followup_id" in columns
    row = conn.execute(
        "SELECT source_kind,source_id,partner_campaign_id,partner_candidate_id "
        "FROM provider_dispatch_outbox WHERE id='dispatch-1'"
    ).fetchone()
    assert dict(row) == {
        "source_kind": "partner_outreach",
        "source_id": "candidate-1",
        "partner_campaign_id": "campaign-1",
        "partner_candidate_id": "candidate-1",
    }
    sql = str(
        conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='provider_dispatch_outbox'"
        ).fetchone()["sql"]
    )
    assert "sales_followup" in sql

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
