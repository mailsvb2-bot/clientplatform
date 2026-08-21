from __future__ import annotations

import sqlite3
from unittest.mock import patch

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
