from __future__ import annotations

import logging
import re
import sqlite3

from services.db.runtime import is_postgres_enabled
from services.migrations._helpers import mark_migration, migration_applied


NAME = "clientplatform_provider_dispatch_event_message_v1"
_CONSTRAINT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _sqlite_sql(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='provider_dispatch_outbox'"
    ).fetchone()
    if row is None:
        return None
    return str(row["sql"] if hasattr(row, "keys") else row[0])


def _update_sqlite(conn: sqlite3.Connection) -> None:
    from services.db.schema import clientplatform_provider_dispatch

    sql = _sqlite_sql(conn)
    if sql is None:
        clientplatform_provider_dispatch.ensure(conn)
        return
    if "event_message" in sql:
        return

    replacements = (
        (
            "'lesson_delivery', 'partner_outreach', 'sales_followup', 'customer_interaction', 'member_interaction'",
            "'lesson_delivery', 'partner_outreach', 'sales_followup', 'customer_interaction', 'member_interaction', 'event_message'",
        ),
        (
            "source_kind IN ('customer_interaction','member_interaction')",
            "source_kind IN ('customer_interaction','member_interaction','event_message')",
        ),
    )
    updated = sql
    for old, new in replacements:
        if new in updated:
            continue
        if updated.count(old) != 1:
            raise RuntimeError("provider_dispatch_outbox CHECK has unexpected SQLite shape")
        updated = updated.replace(old, new)

    schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    conn.execute("PRAGMA writable_schema=ON")
    try:
        cursor = conn.execute(
            "UPDATE sqlite_schema SET sql=? WHERE type='table' AND name='provider_dispatch_outbox'",
            (updated,),
        )
        if int(getattr(cursor, "rowcount", 1) or 0) != 1:
            raise RuntimeError("provider_dispatch_outbox schema row was not updated")
        conn.execute(f"PRAGMA schema_version={schema_version + 1}")
    finally:
        conn.execute("PRAGMA writable_schema=OFF")

    foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise RuntimeError("event dispatch migration broke SQLite foreign keys")
    quick = conn.execute("PRAGMA quick_check").fetchone()
    if str(quick[0] if quick else "").lower() != "ok":
        raise RuntimeError("event dispatch migration failed SQLite quick_check")


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
    _drop_source_checks_postgres(conn)
    conn.execute(
        """
        ALTER TABLE provider_dispatch_outbox
        ADD CONSTRAINT cp_provider_dispatch_source_kind_event_v1
        CHECK(source_kind IN (
            'lesson_delivery','partner_outreach','sales_followup',
            'customer_interaction','member_interaction','event_message'
        ))
        """
    )
    conn.execute(
        """
        ALTER TABLE provider_dispatch_outbox
        ADD CONSTRAINT cp_provider_dispatch_source_shape_event_v1
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
            OR
            (source_kind IN ('customer_interaction','member_interaction','event_message')
                AND logical_delivery_id IS NULL
                AND partner_campaign_id IS NULL
                AND partner_candidate_id IS NULL
                AND sales_followup_id IS NULL)
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
        _update_sqlite(conn)
    mark_migration(conn, NAME)
    log.info("Migration applied: %s", NAME)
