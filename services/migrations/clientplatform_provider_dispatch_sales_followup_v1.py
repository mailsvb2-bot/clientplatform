from __future__ import annotations

import logging
import re
import sqlite3

from services.db.runtime import is_postgres_enabled
from services.migrations._helpers import mark_migration, migration_applied


NAME = "clientplatform_provider_dispatch_sales_followup_v1"
_CONSTRAINT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _rebuild_sqlite(conn: sqlite3.Connection) -> None:
    from services.db.schema import clientplatform_provider_dispatch

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='provider_dispatch_outbox'"
    ).fetchone()
    if row is None:
        clientplatform_provider_dispatch.ensure(conn)
        return
    sql = str(row["sql"] if hasattr(row, "keys") else row[0])
    if "sales_followup_id" in sql and "sales_followup" in sql:
        return

    conn.execute("DROP INDEX IF EXISTS idx_provider_dispatch_due")
    conn.execute("DROP INDEX IF EXISTS idx_provider_dispatch_business_source")
    conn.execute("DROP INDEX IF EXISTS idx_provider_dispatch_partner_reply")
    conn.execute("DROP TABLE IF EXISTS provider_dispatch_outbox_u009_v0")
    conn.execute(
        "ALTER TABLE provider_dispatch_outbox RENAME TO provider_dispatch_outbox_u009_v0"
    )
    clientplatform_provider_dispatch.ensure(conn)
    conn.execute(
        """
        INSERT INTO provider_dispatch_outbox(
            id,business_id,platform,source_kind,source_id,
            logical_delivery_id,partner_campaign_id,partner_candidate_id,
            sales_followup_id,connection_id,recipient_kind,customer_identity_id,
            external_subject,payload_kind,payload_ref,idempotency_key,status,
            attempts,available_at,locked_at,lock_token,provider_message_id,
            last_error,created_at,updated_at,sent_at,dead_at
        )
        SELECT
            id,business_id,platform,source_kind,source_id,
            logical_delivery_id,partner_campaign_id,partner_candidate_id,
            NULL,connection_id,recipient_kind,customer_identity_id,
            external_subject,payload_kind,payload_ref,idempotency_key,status,
            attempts,available_at,locked_at,lock_token,provider_message_id,
            last_error,created_at,updated_at,sent_at,dead_at
        FROM provider_dispatch_outbox_u009_v0
        """
    )
    conn.execute("DROP TABLE provider_dispatch_outbox_u009_v0")


def _drop_source_checks_postgres(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT conname, pg_get_constraintdef(oid) AS definition
        FROM pg_constraint
        WHERE conrelid='provider_dispatch_outbox'::regclass AND contype='c'
        """
    ).fetchall()
    for row in rows:
        name = str(row["conname"] if hasattr(row, "keys") else row[0])
        definition = str(row["definition"] if hasattr(row, "keys") else row[1])
        if "source_kind" not in definition:
            continue
        if not _CONSTRAINT_NAME.fullmatch(name):
            raise RuntimeError("unsafe provider dispatch constraint name")
        conn.execute(f'ALTER TABLE provider_dispatch_outbox DROP CONSTRAINT "{name}"')


def _update_postgres(conn: sqlite3.Connection) -> None:
    conn.execute(
        "ALTER TABLE provider_dispatch_outbox ADD COLUMN IF NOT EXISTS sales_followup_id TEXT"
    )
    _drop_source_checks_postgres(conn)
    conn.execute(
        """
        ALTER TABLE provider_dispatch_outbox
        ADD CONSTRAINT cp_provider_dispatch_source_kind_u009
        CHECK(source_kind IN ('lesson_delivery','partner_outreach','sales_followup'))
        """
    )
    conn.execute(
        """
        ALTER TABLE provider_dispatch_outbox
        ADD CONSTRAINT cp_provider_dispatch_source_shape_u009
        CHECK(
            (source_kind='lesson_delivery'
                AND logical_delivery_id IS NOT NULL
                AND partner_campaign_id IS NULL
                AND partner_candidate_id IS NULL
                AND sales_followup_id IS NULL
                AND source_id=logical_delivery_id)
            OR
            (source_kind='partner_outreach'
                AND logical_delivery_id IS NULL
                AND partner_campaign_id IS NOT NULL
                AND partner_candidate_id IS NOT NULL
                AND sales_followup_id IS NULL
                AND source_id=partner_candidate_id)
            OR
            (source_kind='sales_followup'
                AND logical_delivery_id IS NULL
                AND partner_campaign_id IS NULL
                AND partner_candidate_id IS NULL
                AND sales_followup_id IS NOT NULL
                AND source_id=sales_followup_id)
        )
        """
    )


def apply(conn: sqlite3.Connection) -> None:
    log = logging.getLogger(__name__)
    if migration_applied(conn, NAME):
        log.info("Migration skipped (already applied): %s", NAME)
        return
    log.info("Migration start: %s", NAME)
    if is_postgres_enabled():
        _update_postgres(conn)
    else:
        _rebuild_sqlite(conn)
    mark_migration(conn, NAME)
    log.info("Migration applied: %s", NAME)
