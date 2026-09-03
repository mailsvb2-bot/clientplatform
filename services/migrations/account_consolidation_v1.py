from __future__ import annotations

import logging

from services.db.runtime import is_postgres_enabled
from services.migrations._helpers import mark_migration, migration_applied

NAME = "account_consolidation_v1"
log = logging.getLogger(__name__)


def _columns(conn, table: str) -> set[str]:
    if is_postgres_enabled():
        rows = conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name=?
            """.strip(),
            (table,),
        ).fetchall()
        return {str(row["column_name"]) for row in rows}
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()  # nosec B608 - internal table only
    return {str(row["name"] if hasattr(row, "keys") else row[1]) for row in rows}


def _add_column(conn, table: str, ddl: str) -> None:
    column = ddl.split()[0]
    if column in _columns(conn, table):
        return
    conn.execute(f'ALTER TABLE "{table}" ADD COLUMN {ddl}')  # nosec B608 - internal constants only


def apply(conn) -> None:
    if migration_applied(conn, NAME):
        return

    log.info("Migration start: %s", NAME)
    _add_column(conn, "accounts", "merged_into_account_id BIGINT")
    _add_column(conn, "accounts", "merged_at TEXT")
    _add_column(conn, "accounts", "merged_by_user_id BIGINT")
    _add_column(conn, "accounts", "merge_reason TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_accounts_merged_into ON accounts(merged_into_account_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_consolidation_operations(
            id TEXT PRIMARY KEY,
            operator_user_id BIGINT NOT NULL,
            source_account_id BIGINT NOT NULL,
            target_account_id BIGINT NOT NULL,
            source_user_id BIGINT NOT NULL,
            target_user_id BIGINT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            plan_fingerprint TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            before_counts_json TEXT NOT NULL,
            after_counts_json TEXT NOT NULL,
            UNIQUE(operator_user_id, idempotency_key),
            FOREIGN KEY(source_account_id) REFERENCES accounts(account_id) ON DELETE RESTRICT,
            FOREIGN KEY(target_account_id) REFERENCES accounts(account_id) ON DELETE RESTRICT,
            CHECK(operator_user_id > 0),
            CHECK(source_account_id <> target_account_id),
            CHECK(source_user_id <> target_user_id),
            CHECK(length(trim(idempotency_key)) BETWEEN 1 AND 200),
            CHECK(length(request_fingerprint)=64),
            CHECK(length(plan_fingerprint)=64),
            CHECK(length(trim(reason)) BETWEEN 3 AND 500),
            CHECK(status='applied')
        )
        """.strip()
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_account_consolidation_operations_accounts
        ON account_consolidation_operations(source_account_id, target_account_id, applied_at)
        """.strip()
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_consolidation_audit_events(
            id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL,
            operator_user_id BIGINT NOT NULL,
            source_user_id BIGINT NOT NULL,
            target_user_id BIGINT NOT NULL,
            event_type TEXT NOT NULL,
            detail_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(operation_id) REFERENCES account_consolidation_operations(id) ON DELETE RESTRICT,
            CHECK(operator_user_id > 0),
            CHECK(source_user_id <> target_user_id),
            CHECK(event_type='applied')
        )
        """.strip()
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_account_consolidation_audit_operation_event
        ON account_consolidation_audit_events(operation_id, event_type)
        """.strip()
    )

    mark_migration(conn, NAME)
    log.info("Migration applied: %s", NAME)
