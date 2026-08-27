from __future__ import annotations

import logging
import re
import sqlite3

from services.db.runtime import is_postgres_enabled
from services.migrations._helpers import mark_migration, migration_applied


NAME = "clientplatform_promotion_channel_max_v1"
_OLD_CHECK = "CHECK(channel IN ('telegram', 'vk', 'whatsapp', 'website', 'offline'))"
_NEW_CHECK = "CHECK(channel IN ('telegram', 'vk', 'max', 'whatsapp', 'website', 'offline'))"
_CONSTRAINT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _sqlite_table_sql(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='promotion_campaigns'"
    ).fetchone()
    if row is None:
        return None
    return str(row["sql"] if hasattr(row, "keys") else row[0])


def _update_sqlite_constraint(conn: sqlite3.Connection) -> None:
    sql = _sqlite_table_sql(conn)
    if sql is None or _NEW_CHECK in sql:
        return
    if sql.count(_OLD_CHECK) != 1:
        raise RuntimeError("promotion_campaigns channel CHECK is not the expected legacy shape")
    updated = sql.replace(_OLD_CHECK, _NEW_CHECK)
    schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    conn.execute("PRAGMA writable_schema=ON")
    try:
        cursor = conn.execute(
            "UPDATE sqlite_schema SET sql=? WHERE type='table' AND name='promotion_campaigns'",
            (updated,),
        )
        if int(getattr(cursor, "rowcount", 1) or 0) != 1:
            raise RuntimeError("promotion_campaigns schema row was not updated")
        conn.execute(f"PRAGMA schema_version={schema_version + 1}")
    finally:
        conn.execute("PRAGMA writable_schema=OFF")
    foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise RuntimeError("promotion_campaigns MAX migration broke foreign keys")
    quick_check = conn.execute("PRAGMA quick_check").fetchone()
    quick_value = str(quick_check[0] if quick_check else "")
    if quick_value.lower() != "ok":
        raise RuntimeError("promotion_campaigns MAX migration failed SQLite quick_check")


def _update_postgres_constraint(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT conname, pg_get_constraintdef(oid) AS definition
        FROM pg_constraint
        WHERE conrelid='promotion_campaigns'::regclass AND contype='c'
        """
    ).fetchall()
    for row in rows:
        name = str(row["conname"] if hasattr(row, "keys") else row[0])
        definition = str(row["definition"] if hasattr(row, "keys") else row[1])
        if "channel" not in definition:
            continue
        if not _CONSTRAINT_NAME.fullmatch(name):
            raise RuntimeError("unsafe promotion campaign constraint name")
        conn.execute(f'ALTER TABLE promotion_campaigns DROP CONSTRAINT "{name}"')
    conn.execute(
        """
        ALTER TABLE promotion_campaigns
        ADD CONSTRAINT cp_promotion_campaigns_channel_max_v1
        CHECK(channel IN ('telegram','vk','max','whatsapp','website','offline'))
        """
    )


def apply(conn: sqlite3.Connection) -> None:
    log = logging.getLogger(__name__)
    if migration_applied(conn, NAME):
        log.info("Migration skipped (already applied): %s", NAME)
        return
    log.info("Migration start: %s", NAME)
    if is_postgres_enabled():
        _update_postgres_constraint(conn)
    else:
        _update_sqlite_constraint(conn)
    mark_migration(conn, NAME)
    log.info("Migration applied: %s", NAME)
