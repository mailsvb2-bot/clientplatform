from __future__ import annotations

import logging
import sqlite3

from services.db.runtime import is_postgres_enabled
from services.migrations._helpers import mark_migration, migration_applied


NAME = "clientplatform_managed_bot_provider_v1"
_TABLE = "managed_bot_provisioning_requests"
_LEGACY_TABLE = "managed_bot_provisioning_requests_provider_v0"
_COLUMNS = """
    id, business_id, created_by_member_id, provider, status, idempotency_key,
    requested_username, display_name, credential_reference,
    webhook_secret_reference, external_bot_id, verified_username,
    connection_id, managed_bot_id, attempts, verification_token,
    verification_started_at, created_at, updated_at, completed_at,
    failed_at, cancelled_at, last_error_code
""".strip()


def _sqlite_supports_managed_provider(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (_TABLE,),
    ).fetchone()
    if row is None:
        return True
    sql = str(row["sql"] if hasattr(row, "keys") else row[0])
    return "telegram_managed" in sql


def _rebuild_sqlite(conn: sqlite3.Connection) -> None:
    from services.db.schema import clientplatform_bot_provisioning

    conn.execute("DROP INDEX IF EXISTS idx_managed_bot_provisioning_business_status")
    conn.execute("DROP INDEX IF EXISTS idx_managed_bot_provisioning_verifying")
    conn.execute(f"DROP TABLE IF EXISTS {_LEGACY_TABLE}")
    conn.execute(f"ALTER TABLE {_TABLE} RENAME TO {_LEGACY_TABLE}")
    clientplatform_bot_provisioning.ensure(conn)
    conn.execute(
        f"INSERT INTO {_TABLE}({_COLUMNS}) SELECT {_COLUMNS} FROM {_LEGACY_TABLE}"
    )
    conn.execute(f"DROP TABLE {_LEGACY_TABLE}")


def _update_postgres_constraint(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        ALTER TABLE managed_bot_provisioning_requests
        DROP CONSTRAINT IF EXISTS managed_bot_provisioning_requests_provider_check
        """.strip()
    )
    conn.execute(
        """
        ALTER TABLE managed_bot_provisioning_requests
        ADD CONSTRAINT managed_bot_provisioning_requests_provider_check
        CHECK(provider IN ('telegram_managed', 'botfather'))
        """.strip()
    )


def apply(conn: sqlite3.Connection) -> None:
    log = logging.getLogger(__name__)
    if migration_applied(conn, NAME):
        log.info("Migration skipped (already applied): %s", NAME)
        return

    log.info("Migration start: %s", NAME)
    if is_postgres_enabled():
        _update_postgres_constraint(conn)
    elif not _sqlite_supports_managed_provider(conn):
        _rebuild_sqlite(conn)
    mark_migration(conn, NAME)
    log.info("Migration applied: %s", NAME)
