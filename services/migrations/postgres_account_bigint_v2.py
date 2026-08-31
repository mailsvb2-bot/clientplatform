from __future__ import annotations

import logging
import re
import sqlite3

from services.db.runtime import is_postgres_enabled
from services.migrations._helpers import mark_migration, migration_applied

NAME = "postgres_account_bigint_v2"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ACCOUNT_ID_COLUMN = re.compile(r"^account_id$", re.IGNORECASE)


def _account_integer_columns(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        """
        SELECT c.table_name, c.column_name
        FROM information_schema.columns AS c
        JOIN information_schema.tables AS t
          ON t.table_schema=c.table_schema
         AND t.table_name=c.table_name
        WHERE c.table_schema=current_schema()
          AND c.data_type='integer'
          AND t.table_type='BASE TABLE'
        ORDER BY c.table_name, c.ordinal_position
        """.strip()
    ).fetchall()
    columns: list[tuple[str, str]] = []
    for row in rows:
        table = str(row["table_name"])
        column = str(row["column_name"])
        if (
            _IDENTIFIER.fullmatch(table)
            and _IDENTIFIER.fullmatch(column)
            and _ACCOUNT_ID_COLUMN.fullmatch(column)
        ):
            columns.append((table, column))
    return columns


def apply(conn: sqlite3.Connection) -> None:
    log = logging.getLogger(__name__)
    if migration_applied(conn, NAME):
        log.info("Migration skipped (already applied): %s", NAME)
        return

    log.info("Migration start: %s", NAME)
    promoted = 0
    if is_postgres_enabled():
        for table, column in _account_integer_columns(conn):
            conn.execute(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE BIGINT')
            promoted += 1
    mark_migration(conn, NAME)
    log.info("Migration applied: %s promoted=%s", NAME, promoted)
