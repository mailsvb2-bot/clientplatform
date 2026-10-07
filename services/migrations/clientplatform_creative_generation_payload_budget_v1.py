from __future__ import annotations

import logging
import re
import sqlite3

from clientplatform.domain.creative_generation import (
    MAX_CREATIVE_GENERATION_PROVIDER_PAYLOAD_CHARS,
)
from services.db.runtime import is_postgres_enabled
from services.migrations._helpers import mark_migration, migration_applied


NAME = "clientplatform_creative_generation_payload_budget_v1"
_OLD_CHECK = "CHECK(length(provider_payload_json) BETWEEN 1 AND 10000)"
_NEW_CHECK = (
    "CHECK(length(provider_payload_json) BETWEEN 1 AND "
    f"{MAX_CREATIVE_GENERATION_PROVIDER_PAYLOAD_CHARS})"
)
_CONSTRAINT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _sqlite_table_sql(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='table' AND name='creative_generation_receipts'"
    ).fetchone()
    if row is None:
        return None
    return str(row["sql"] if hasattr(row, "keys") else row[0])


def _update_sqlite_constraint(conn: sqlite3.Connection) -> None:
    sql = _sqlite_table_sql(conn)
    if sql is None or _NEW_CHECK in sql:
        return
    if sql.count(_OLD_CHECK) != 1:
        raise RuntimeError(
            "creative_generation_receipts payload CHECK has unexpected SQLite shape"
        )
    updated = sql.replace(_OLD_CHECK, _NEW_CHECK)
    schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    conn.execute("PRAGMA writable_schema=ON")
    try:
        cursor = conn.execute(
            "UPDATE sqlite_schema SET sql=? "
            "WHERE type='table' AND name='creative_generation_receipts'",
            (updated,),
        )
        if int(getattr(cursor, "rowcount", 1) or 0) != 1:
            raise RuntimeError("creative generation receipt schema row was not updated")
        conn.execute(f"PRAGMA schema_version={schema_version + 1}")
    finally:
        conn.execute("PRAGMA writable_schema=OFF")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("creative payload budget migration broke SQLite foreign keys")
    quick = conn.execute("PRAGMA quick_check").fetchone()
    if str(quick[0] if quick else "").lower() != "ok":
        raise RuntimeError("creative payload budget migration failed SQLite quick_check")


def _update_postgres_constraint(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT conname, pg_get_constraintdef(oid) AS definition
        FROM pg_constraint
        WHERE conrelid='creative_generation_receipts'::regclass
          AND contype='c'
        """
    ).fetchall()
    found = False
    for row in rows:
        name = str(row["conname"] if hasattr(row, "keys") else row[0])
        definition = str(row["definition"] if hasattr(row, "keys") else row[1])
        if "provider_payload_json" not in definition:
            continue
        found = True
        if not _CONSTRAINT_NAME.fullmatch(name):
            raise RuntimeError("unsafe creative generation payload constraint name")
        conn.execute(
            f'ALTER TABLE creative_generation_receipts DROP CONSTRAINT "{name}"'
        )
    if not found:
        logging.getLogger(__name__).info(
            "No legacy provider_payload_json CHECK found; installing canonical one"
        )
    conn.execute(
        f"""
        ALTER TABLE creative_generation_receipts
        ADD CONSTRAINT cp_creative_generation_payload_budget_v1
        CHECK(length(provider_payload_json) BETWEEN 1 AND
              {MAX_CREATIVE_GENERATION_PROVIDER_PAYLOAD_CHARS})
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


__all__ = ["NAME", "apply"]
