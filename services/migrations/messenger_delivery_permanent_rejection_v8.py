from __future__ import annotations

import logging
import sqlite3

from services.migrations._helpers import mark_migration, migration_applied

NAME = "messenger_delivery_permanent_rejection_v8"
log = logging.getLogger(__name__)

_LEGACY_MAX_PERMANENT_ERRORS = (
    "MessengerTransportError:max.http.403",
    "MaxProviderRejectedError:max.send_text.http_403",
)


def apply(conn: sqlite3.Connection) -> None:
    if migration_applied(conn, NAME):
        return

    log.info("Migration start: %s", NAME)
    placeholders = ",".join("?" for _ in _LEGACY_MAX_PERMANENT_ERRORS)
    conn.execute(
        "UPDATE messenger_delivery_outbox "
        "SET status='rejected', locked_at=NULL, lock_token=NULL "
        "WHERE platform='max' AND status='dead' "
        f"AND last_error IN ({placeholders})",
        _LEGACY_MAX_PERMANENT_ERRORS,
    )
    mark_migration(conn, NAME)
    log.info("Migration applied: %s", NAME)
