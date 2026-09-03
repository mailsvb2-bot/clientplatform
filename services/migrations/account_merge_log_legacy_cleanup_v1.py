from __future__ import annotations

import logging

from services.migrations._helpers import mark_migration, migration_applied, table_exists

NAME = "account_merge_log_legacy_cleanup_v1"
LEGACY_TABLE = "account_merge_log"
log = logging.getLogger(__name__)


def apply(conn) -> None:
    if migration_applied(conn, NAME):
        return

    if not table_exists(conn, LEGACY_TABLE):
        mark_migration(conn, NAME)
        return

    row = conn.execute(f'SELECT COUNT(*) AS c FROM "{LEGACY_TABLE}"').fetchone()  # nosec B608 - internal constant
    count = int(row["c"] if hasattr(row, "keys") else row[0])
    if count != 0:
        raise RuntimeError(f"legacy_account_merge_log_not_empty:{count}")

    conn.execute(f'DROP TABLE "{LEGACY_TABLE}"')  # nosec B608 - internal constant
    mark_migration(conn, NAME)
    log.warning("Removed empty forbidden legacy merge store: %s", LEGACY_TABLE)
