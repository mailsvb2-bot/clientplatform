from __future__ import annotations

import logging
import sqlite3

from services.db.runtime import is_postgres_enabled
from services.migrations._helpers import mark_migration, migration_applied


NAME = "clientplatform_direct_global_ownership_v1"
_OWNER_INDEX = "uq_ad_connections_direct_global_owner"
_SQLITE_PROMOTION_TRIGGER = "trg_ad_connections_promote_direct_identity"
_POSTGRES_PROMOTION_FUNCTION = "clientplatform_promote_direct_identity"


def _sqlite_columns(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("PRAGMA table_info(ad_connections)").fetchall()
    return {
        str(row["name"] if hasattr(row, "keys") else row[1]).strip()
        for row in rows
    }


def _ensure_identity_source(conn: sqlite3.Connection) -> None:
    if is_postgres_enabled():
        conn.execute(
            """
            ALTER TABLE ad_connections
            ADD COLUMN IF NOT EXISTS identity_source TEXT NOT NULL
            DEFAULT 'direct_client_id'
            """.strip()
        )
        return
    if "identity_source" not in _sqlite_columns(conn):
        conn.execute(
            """
            ALTER TABLE ad_connections
            ADD COLUMN identity_source TEXT NOT NULL DEFAULT 'direct_client_id'
            """.strip()
        )


def _quarantine_legacy_connections(conn: sqlite3.Connection) -> None:
    """Never reinterpret a historic OAuth-user id as a Direct advertiser id."""

    conn.execute(
        """
        UPDATE ad_connections
        SET identity_source='legacy_oauth',
            status=CASE WHEN status='revoked' THEN 'revoked' ELSE 'disabled' END,
            last_error_code=CASE
                WHEN status='revoked' THEN last_error_code
                ELSE 'direct_identity_reverification_required'
            END
        """.strip()
    )


def _install_sqlite_promotion_trigger(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {_SQLITE_PROMOTION_TRIGGER}
        AFTER UPDATE OF status ON ad_connections
        WHEN OLD.identity_source='legacy_oauth' AND NEW.status='active'
        BEGIN
            UPDATE ad_connections
            SET identity_source='direct_client_id'
            WHERE id=NEW.id;
        END
        """.strip()
    )


def _install_postgres_promotion_trigger(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_POSTGRES_PROMOTION_FUNCTION}()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.identity_source='legacy_oauth' AND NEW.status='active' THEN
                NEW.identity_source := 'direct_client_id';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """.strip()
    )
    conn.execute(
        f"DROP TRIGGER IF EXISTS {_SQLITE_PROMOTION_TRIGGER} ON ad_connections"
    )
    conn.execute(
        f"""
        CREATE TRIGGER {_SQLITE_PROMOTION_TRIGGER}
        BEFORE UPDATE OF status ON ad_connections
        FOR EACH ROW
        EXECUTE FUNCTION {_POSTGRES_PROMOTION_FUNCTION}()
        """.strip()
    )


def _install_owner_index(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {_OWNER_INDEX}
        ON ad_connections(provider, external_account_id)
        WHERE identity_source='direct_client_id'
          AND status IN ('active', 'attention', 'disabled')
        """.strip()
    )


def apply(conn: sqlite3.Connection) -> None:
    log = logging.getLogger(__name__)
    if migration_applied(conn, NAME):
        log.info("Migration skipped (already applied): %s", NAME)
        return

    log.info("Migration start: %s", NAME)
    _ensure_identity_source(conn)
    _quarantine_legacy_connections(conn)
    if is_postgres_enabled():
        _install_postgres_promotion_trigger(conn)
    else:
        _install_sqlite_promotion_trigger(conn)
    _install_owner_index(conn)
    mark_migration(conn, NAME)
    log.info("Migration applied: %s", NAME)
