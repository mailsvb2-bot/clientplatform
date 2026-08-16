from __future__ import annotations

import logging
import sqlite3

from services.db.runtime import is_postgres_enabled
from services.migrations._helpers import mark_migration, migration_applied


NAME = "clientplatform_direct_global_ownership_v1"
_OWNER_INDEX = "uq_ad_connections_direct_global_owner"
_SQLITE_NEW_OWNER_GUARD = "trg_ad_connections_direct_new_owner_guard"
_SQLITE_LEGACY_AMBIGUITY_GUARD = "trg_ad_connections_direct_legacy_ambiguity_guard"
_SQLITE_LEGACY_REACTIVATION_GUARD = "trg_ad_connections_direct_legacy_reactivation_guard"
_SQLITE_LEGACY_CLEANUP = "trg_ad_connections_direct_legacy_cleanup"
_POSTGRES_GUARD_FUNCTION = "clientplatform_guard_direct_identity"
_POSTGRES_CLEANUP_FUNCTION = "clientplatform_cleanup_direct_legacy_identity"


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


def _install_owner_index(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {_OWNER_INDEX}
        ON ad_connections(provider, external_account_id)
        WHERE identity_source='direct_client_id'
          AND status IN ('active', 'attention', 'disabled')
        """.strip()
    )


def _install_sqlite_guards(conn: sqlite3.Connection) -> None:
    # A tenant which already owns this verified ClientId may reconnect even while
    # unrelated legacy rows are still being re-verified. A brand-new claimant may
    # not cross that migration barrier.
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {_SQLITE_NEW_OWNER_GUARD}
        BEFORE INSERT ON ad_connections
        WHEN NEW.provider='yandex_direct'
          AND NEW.identity_source='direct_client_id'
          AND NEW.status IN ('active', 'attention', 'disabled')
          AND EXISTS (
              SELECT 1 FROM ad_connections
              WHERE provider='yandex_direct'
                AND identity_source='legacy_oauth'
                AND status!='revoked'
          )
          AND NOT EXISTS (
              SELECT 1 FROM ad_connections
              WHERE provider='yandex_direct'
                AND identity_source='direct_client_id'
                AND business_id=NEW.business_id
                AND external_account_id=NEW.external_account_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM ad_connections
              WHERE provider='yandex_direct'
                AND identity_source='legacy_oauth'
                AND business_id=NEW.business_id
                AND status!='revoked'
          )
        BEGIN
            SELECT RAISE(ABORT, 'direct_identity_reverification_pending');
        END
        """.strip()
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {_SQLITE_LEGACY_AMBIGUITY_GUARD}
        BEFORE INSERT ON ad_connections
        WHEN NEW.provider='yandex_direct'
          AND NEW.identity_source='direct_client_id'
          AND NEW.status IN ('active', 'attention', 'disabled')
          AND (
              SELECT COUNT(*) FROM ad_connections
              WHERE provider='yandex_direct'
                AND identity_source='legacy_oauth'
                AND business_id=NEW.business_id
                AND status!='revoked'
          ) > 1
        BEGIN
            SELECT RAISE(ABORT, 'direct_identity_reverification_ambiguous');
        END
        """.strip()
    )
    # A historic row can never become verified merely by changing lifecycle
    # status. Re-verification must create/update a row carrying the real ClientId.
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {_SQLITE_LEGACY_REACTIVATION_GUARD}
        BEFORE UPDATE OF status ON ad_connections
        WHEN OLD.provider='yandex_direct'
          AND OLD.identity_source='legacy_oauth'
          AND NEW.identity_source='legacy_oauth'
          AND NEW.status='active'
        BEGIN
            SELECT RAISE(ABORT, 'direct_identity_reverification_required');
        END
        """.strip()
    )
    # Once the sole legacy owner reconnects with a real Direct ClientId, retire
    # and erase the obsolete OAuth-identity row in the same transaction.
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {_SQLITE_LEGACY_CLEANUP}
        AFTER INSERT ON ad_connections
        WHEN NEW.provider='yandex_direct'
          AND NEW.identity_source='direct_client_id'
          AND NEW.status IN ('active', 'attention', 'disabled')
          AND (
              SELECT COUNT(*) FROM ad_connections
              WHERE provider='yandex_direct'
                AND identity_source='legacy_oauth'
                AND business_id=NEW.business_id
                AND status!='revoked'
          ) = 1
        BEGIN
            UPDATE ad_connections
            SET status='revoked',
                credential_ciphertext='',
                permissions_json='[]',
                last_error_code='direct_identity_reverified',
                updated_at=NEW.updated_at
            WHERE provider='yandex_direct'
              AND identity_source='legacy_oauth'
              AND business_id=NEW.business_id
              AND status!='revoked';
        END
        """.strip()
    )


def _install_postgres_guards(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_POSTGRES_GUARD_FUNCTION}()
        RETURNS trigger AS $$
        DECLARE
            own_legacy_count BIGINT;
            global_legacy_exists BOOLEAN;
            verified_same_exists BOOLEAN;
        BEGIN
            IF NEW.provider <> 'yandex_direct' THEN
                RETURN NEW;
            END IF;

            IF TG_OP = 'UPDATE'
               AND OLD.identity_source = 'legacy_oauth'
               AND NEW.identity_source = 'legacy_oauth'
               AND NEW.status = 'active' THEN
                RAISE EXCEPTION 'direct_identity_reverification_required'
                    USING ERRCODE = '23514';
            END IF;

            IF NEW.identity_source <> 'direct_client_id'
               OR NEW.status NOT IN ('active', 'attention', 'disabled') THEN
                RETURN NEW;
            END IF;

            SELECT COUNT(*) INTO own_legacy_count
            FROM ad_connections
            WHERE provider='yandex_direct'
              AND identity_source='legacy_oauth'
              AND business_id=NEW.business_id
              AND status!='revoked';

            IF own_legacy_count > 1 THEN
                RAISE EXCEPTION 'direct_identity_reverification_ambiguous'
                    USING ERRCODE = '23514';
            END IF;

            SELECT EXISTS (
                SELECT 1 FROM ad_connections
                WHERE provider='yandex_direct'
                  AND identity_source='legacy_oauth'
                  AND status!='revoked'
            ) INTO global_legacy_exists;
            SELECT EXISTS (
                SELECT 1 FROM ad_connections
                WHERE provider='yandex_direct'
                  AND identity_source='direct_client_id'
                  AND business_id=NEW.business_id
                  AND external_account_id=NEW.external_account_id
            ) INTO verified_same_exists;

            IF global_legacy_exists
               AND own_legacy_count = 0
               AND NOT verified_same_exists THEN
                RAISE EXCEPTION 'direct_identity_reverification_pending'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """.strip()
    )
    conn.execute(
        f"DROP TRIGGER IF EXISTS {_SQLITE_NEW_OWNER_GUARD} ON ad_connections"
    )
    conn.execute(
        f"DROP TRIGGER IF EXISTS {_SQLITE_LEGACY_REACTIVATION_GUARD} ON ad_connections"
    )
    conn.execute(
        f"""
        CREATE TRIGGER {_SQLITE_NEW_OWNER_GUARD}
        BEFORE INSERT OR UPDATE OF status, identity_source, external_account_id
        ON ad_connections
        FOR EACH ROW
        EXECUTE FUNCTION {_POSTGRES_GUARD_FUNCTION}()
        """.strip()
    )

    conn.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_POSTGRES_CLEANUP_FUNCTION}()
        RETURNS trigger AS $$
        DECLARE
            own_legacy_count BIGINT;
        BEGIN
            IF NEW.provider <> 'yandex_direct'
               OR NEW.identity_source <> 'direct_client_id'
               OR NEW.status NOT IN ('active', 'attention', 'disabled') THEN
                RETURN NEW;
            END IF;
            SELECT COUNT(*) INTO own_legacy_count
            FROM ad_connections
            WHERE provider='yandex_direct'
              AND identity_source='legacy_oauth'
              AND business_id=NEW.business_id
              AND status!='revoked';
            IF own_legacy_count = 1 THEN
                UPDATE ad_connections
                SET status='revoked',
                    credential_ciphertext='',
                    permissions_json='[]',
                    last_error_code='direct_identity_reverified',
                    updated_at=NEW.updated_at
                WHERE provider='yandex_direct'
                  AND identity_source='legacy_oauth'
                  AND business_id=NEW.business_id
                  AND status!='revoked';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """.strip()
    )
    conn.execute(
        f"DROP TRIGGER IF EXISTS {_SQLITE_LEGACY_CLEANUP} ON ad_connections"
    )
    conn.execute(
        f"""
        CREATE TRIGGER {_SQLITE_LEGACY_CLEANUP}
        AFTER INSERT ON ad_connections
        FOR EACH ROW
        EXECUTE FUNCTION {_POSTGRES_CLEANUP_FUNCTION}()
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
    _install_owner_index(conn)
    if is_postgres_enabled():
        _install_postgres_guards(conn)
    else:
        _install_sqlite_guards(conn)
    mark_migration(conn, NAME)
    log.info("Migration applied: %s", NAME)
