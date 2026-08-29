from __future__ import annotations

import logging
import re
import sqlite3

from services.db.runtime import is_postgres_enabled
from services.migrations._helpers import mark_migration, migration_applied


NAME = "clientplatform_email_outbound_v1"
_CONSTRAINT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _sqlite_sql(conn: sqlite3.Connection, table: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if row is None:
        return None
    return str(row["sql"] if hasattr(row, "keys") else row[0])


def _rewrite_sqlite_table(
    conn: sqlite3.Connection, *, table: str, replacements: tuple[tuple[str, str], ...]
) -> None:
    sql = _sqlite_sql(conn, table)
    if sql is None:
        return
    updated = sql
    for old, new in replacements:
        if new in updated:
            continue
        if updated.count(old) != 1:
            raise RuntimeError(f"{table} CHECK is not the expected legacy shape")
        updated = updated.replace(old, new)
    if updated == sql:
        return
    schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    conn.execute("PRAGMA writable_schema=ON")
    try:
        cursor = conn.execute(
            "UPDATE sqlite_schema SET sql=? WHERE type='table' AND name=?",
            (updated, table),
        )
        if int(getattr(cursor, "rowcount", 1) or 0) != 1:
            raise RuntimeError(f"{table} schema row was not updated")
        conn.execute(f"PRAGMA schema_version={schema_version + 1}")
    finally:
        conn.execute("PRAGMA writable_schema=OFF")


def _update_sqlite(conn: sqlite3.Connection) -> None:
    _rewrite_sqlite_table(
        conn,
        table="connections",
        replacements=(
            (
                "CHECK(platform IN ('telegram', 'vk', 'max'))",
                "CHECK(platform IN ('telegram', 'vk', 'max', 'email'))",
            ),
            (
                "'vk_community', 'max_shared_bot', 'max_personal_bot'",
                "'vk_community', 'max_shared_bot', 'max_personal_bot',\n                'email_smtp'",
            ),
            (
                "OR (platform='max' AND connection_type IN (\n                    'max_shared_bot', 'max_personal_bot'\n                ))",
                "OR (platform='max' AND connection_type IN (\n                    'max_shared_bot', 'max_personal_bot'\n                ))\n                OR (platform='email' AND connection_type='email_smtp')",
            ),
        ),
    )
    _rewrite_sqlite_table(
        conn,
        table="connection_credentials",
        replacements=(
            (
                "CHECK(platform IN ('vk', 'max'))",
                "CHECK(platform IN ('vk', 'max', 'email'))",
            ),
            (
                "CHECK(purpose IN ('provider_token', 'webhook_secret', 'confirmation_code'))",
                "CHECK(purpose IN ('provider_token', 'webhook_secret', 'confirmation_code', 'smtp_credentials'))",
            ),
        ),
    )
    for table in ("provider_dispatch_outbox", "partner_reply_events"):
        _rewrite_sqlite_table(
            conn,
            table=table,
            replacements=((
                "CHECK(platform IN ('telegram', 'vk', 'max'))",
                "CHECK(platform IN ('telegram', 'vk', 'max', 'email'))",
            ),),
        )
    from services.db.schema import clientplatform_provider_dispatch

    clientplatform_provider_dispatch.ensure(conn)
    foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise RuntimeError("email outbound migration broke SQLite foreign keys")
    quick = conn.execute("PRAGMA quick_check").fetchone()
    if str(quick[0] if quick else "").lower() != "ok":
        raise RuntimeError("email outbound migration failed SQLite quick_check")


def _drop_checks(conn: sqlite3.Connection, table: str, needles: tuple[str, ...]) -> None:
    rows = conn.execute(
        """
        SELECT conname, pg_get_constraintdef(oid) AS definition
        FROM pg_constraint
        WHERE conrelid=?::regclass AND contype='c'
        """,
        (table,),
    ).fetchall()
    for row in rows:
        name = str(row["conname"] if hasattr(row, "keys") else row[0])
        definition = str(row["definition"] if hasattr(row, "keys") else row[1])
        if not any(needle in definition for needle in needles):
            continue
        if not _CONSTRAINT_NAME.fullmatch(name):
            raise RuntimeError(f"unsafe {table} constraint name")
        conn.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{name}"')


def _update_postgres(conn: sqlite3.Connection) -> None:
    _drop_checks(conn, "connections", ("platform", "connection_type"))
    conn.execute("ALTER TABLE connections ADD CONSTRAINT cp_connections_platform_email_v1 CHECK(platform IN ('telegram','vk','max','email'))")
    conn.execute("ALTER TABLE connections ADD CONSTRAINT cp_connections_type_email_v1 CHECK(connection_type IN ('telegram_shared_bot','telegram_managed_bot','telegram_business','telegram_channel','vk_community','max_shared_bot','max_personal_bot','email_smtp'))")
    conn.execute("""ALTER TABLE connections ADD CONSTRAINT cp_connections_pair_email_v1 CHECK((platform='telegram' AND connection_type IN ('telegram_shared_bot','telegram_managed_bot','telegram_business','telegram_channel')) OR (platform='vk' AND connection_type='vk_community') OR (platform='max' AND connection_type IN ('max_shared_bot','max_personal_bot')) OR (platform='email' AND connection_type='email_smtp'))""")

    _drop_checks(conn, "connection_credentials", ("platform", "purpose"))
    conn.execute("ALTER TABLE connection_credentials ADD CONSTRAINT cp_connection_credentials_platform_email_v1 CHECK(platform IN ('vk','max','email'))")
    conn.execute("ALTER TABLE connection_credentials ADD CONSTRAINT cp_connection_credentials_purpose_email_v1 CHECK(purpose IN ('provider_token','webhook_secret','confirmation_code','smtp_credentials'))")

    for table in ("provider_dispatch_outbox", "partner_reply_events"):
        _drop_checks(conn, table, ("platform",))
        conn.execute(f"ALTER TABLE {table} ADD CONSTRAINT cp_{table}_platform_email_v1 CHECK(platform IN ('telegram','vk','max','email'))")

    from services.db.schema import clientplatform_provider_dispatch

    clientplatform_provider_dispatch.ensure(conn)


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
