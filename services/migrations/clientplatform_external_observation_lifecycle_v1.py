from __future__ import annotations

import logging
import sqlite3

from services.db.runtime import is_postgres_enabled
from services.migrations._helpers import mark_migration, migration_applied, table_exists


NAME = "clientplatform_external_observation_lifecycle_v1"

_COLUMNS = (
    ("observation_key", "TEXT"),
    ("observation_revision", "INTEGER"),
    ("observation_state", "TEXT"),
    ("observation_supersedes_event_id", "TEXT"),
    ("observation_fresh_until", "TEXT"),
)


def _column_names(conn: sqlite3.Connection) -> set[str]:
    if is_postgres_enabled():
        rows = conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema=current_schema()
              AND table_name='external_product_event_receipts'
            """
        ).fetchall()
        return {
            str(row["column_name"] if hasattr(row, "keys") else row[0])
            for row in rows
        }
    rows = conn.execute(
        "PRAGMA table_info(external_product_event_receipts)"
    ).fetchall()
    return {
        str(row["name"] if hasattr(row, "keys") else row[1])
        for row in rows
    }


def _ensure_columns(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "external_product_event_receipts"):
        from services.db.schema import clientplatform_external_products

        clientplatform_external_products.ensure(conn)
    existing = _column_names(conn)
    for name, sql_type in _COLUMNS:
        if name in existing:
            continue
        conn.execute(
            f"ALTER TABLE external_product_event_receipts ADD COLUMN {name} {sql_type}"
        )


def _ensure_indexes(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_external_product_observation_revision
        ON external_product_event_receipts(
            business_id, connector_id, customer_id,
            observation_key, observation_revision
        )
        WHERE observation_key IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_external_product_observation_supersedes
        ON external_product_event_receipts(
            business_id, connector_id, observation_supersedes_event_id
        )
        WHERE observation_supersedes_event_id IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_external_product_observation_head
        ON external_product_event_receipts(
            business_id, customer_id, observation_key, observation_revision
        )
        WHERE observation_key IS NOT NULL
        """
    )


def apply(conn: sqlite3.Connection) -> None:
    log = logging.getLogger(__name__)
    if migration_applied(conn, NAME):
        log.info("Migration skipped (already applied): %s", NAME)
        return
    log.info("Migration start: %s", NAME)
    _ensure_columns(conn)
    _ensure_indexes(conn)
    mark_migration(conn, NAME)
    log.info("Migration applied: %s", NAME)


__all__ = ["NAME", "apply"]
