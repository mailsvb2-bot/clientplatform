from __future__ import annotations

import logging
import re
import sqlite3

from services.db.runtime import is_postgres_enabled
from services.migrations._helpers import mark_migration, migration_applied


NAME = "clientplatform_event_content_video_mode_v1"
_CONSTRAINT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _sqlite_sql(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='table' AND name='clientplatform_event_content_preferences'"
    ).fetchone()
    if row is None:
        return None
    return str(row["sql"] if hasattr(row, "keys") else row[0])


def _update_sqlite(conn: sqlite3.Connection) -> None:
    from services.db.schema import clientplatform_event_content

    sql = _sqlite_sql(conn)
    if sql is None:
        clientplatform_event_content.ensure(conn)
        return
    if "'text_with_video'" in sql:
        return
    old = "CHECK(mode IN ('text','text_with_image','text_in_image'))"
    new = "CHECK(mode IN ('text','text_with_image','text_in_image','text_with_video'))"
    if sql.count(old) != 1:
        raise RuntimeError("event content mode CHECK has unexpected SQLite shape")
    updated = sql.replace(old, new)
    schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    conn.execute("PRAGMA writable_schema=ON")
    try:
        cursor = conn.execute(
            "UPDATE sqlite_schema SET sql=? "
            "WHERE type='table' AND name='clientplatform_event_content_preferences'",
            (updated,),
        )
        if int(getattr(cursor, "rowcount", 1) or 0) != 1:
            raise RuntimeError("event content preference schema row was not updated")
        conn.execute(f"PRAGMA schema_version={schema_version + 1}")
    finally:
        conn.execute("PRAGMA writable_schema=OFF")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("event content video migration broke SQLite foreign keys")
    quick = conn.execute("PRAGMA quick_check").fetchone()
    if str(quick[0] if quick else "").lower() != "ok":
        raise RuntimeError("event content video migration failed SQLite quick_check")


def _update_postgres(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT conname, pg_get_constraintdef(oid) AS definition
        FROM pg_constraint
        WHERE conrelid='clientplatform_event_content_preferences'::regclass
          AND contype='c'
        """
    ).fetchall()
    for row in rows:
        name = str(row["conname"] if hasattr(row, "keys") else row[0])
        definition = str(row["definition"] if hasattr(row, "keys") else row[1])
        if "mode" not in definition:
            continue
        if not _CONSTRAINT_NAME.fullmatch(name):
            raise RuntimeError("unsafe event content preference constraint name")
        conn.execute(
            f'ALTER TABLE clientplatform_event_content_preferences DROP CONSTRAINT "{name}"'
        )
    conn.execute(
        """
        ALTER TABLE clientplatform_event_content_preferences
        ADD CONSTRAINT cp_event_content_mode_video_v1
        CHECK(mode IN ('text','text_with_image','text_in_image','text_with_video'))
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


__all__ = ["NAME", "apply"]
