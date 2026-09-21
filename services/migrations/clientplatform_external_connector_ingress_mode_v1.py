from __future__ import annotations

import logging
import sqlite3

from services.db.runtime import is_postgres_enabled
from services.migrations._helpers import mark_migration, migration_applied, table_exists


NAME = "clientplatform_external_connector_ingress_mode_v1"


def _column_names(conn: sqlite3.Connection) -> set[str]:
    if is_postgres_enabled():
        rows = conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema=current_schema()
              AND table_name='external_product_connectors'
            """
        ).fetchall()
        return {
            str(row["column_name"] if hasattr(row, "keys") else row[0])
            for row in rows
        }
    rows = conn.execute("PRAGMA table_info(external_product_connectors)").fetchall()
    return {
        str(row["name"] if hasattr(row, "keys") else row[1])
        for row in rows
    }


def apply(conn: sqlite3.Connection) -> None:
    log = logging.getLogger(__name__)
    if migration_applied(conn, NAME):
        log.info("Migration skipped (already applied): %s", NAME)
        return
    if not table_exists(conn, "external_product_connectors"):
        from services.db.schema import clientplatform_external_products

        clientplatform_external_products.ensure(conn)
    if "ingress_mode" not in _column_names(conn):
        conn.execute(
            """
            ALTER TABLE external_product_connectors
            ADD COLUMN ingress_mode TEXT NOT NULL DEFAULT 'signed_webhook'
            """
        )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_external_product_connectors_ingress_mode
        ON external_product_connectors(business_id, ingress_mode, status, product_key)
        """
    )
    mark_migration(conn, NAME)
    log.info("Migration applied: %s", NAME)


__all__ = ["NAME", "apply"]
