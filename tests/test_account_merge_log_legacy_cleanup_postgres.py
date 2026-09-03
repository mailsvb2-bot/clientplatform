from __future__ import annotations

import pytest

from services.db import get_db
from services.db.runtime import CONFIG
from services.migrations._helpers import migration_applied
from services.migrations.account_merge_log_legacy_cleanup_v1 import NAME, apply


@pytest.mark.skipif(not CONFIG.uses_postgres, reason="PostgreSQL-only legacy schema regression")
def test_postgres_cleanup_removes_empty_legacy_merge_store() -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM schema_migrations WHERE name=?", (NAME,))
        conn.execute("DROP TABLE IF EXISTS account_merge_log")
        conn.execute(
            """
            CREATE TABLE account_merge_log(
                id BIGSERIAL PRIMARY KEY,
                target_account_id INTEGER NOT NULL,
                source_account_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """.strip()
        )
        apply(conn)
        assert conn.execute(
            "SELECT to_regclass('public.account_merge_log') AS table_name"
        ).fetchone()["table_name"] is None
        assert migration_applied(conn, NAME)
