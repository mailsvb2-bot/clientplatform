from __future__ import annotations

import logging
import sqlite3

from services.migrations._helpers import mark_migration, migration_applied


NAME = "clientplatform_external_observation_feedback_v1"


def apply(conn: sqlite3.Connection) -> None:
    log = logging.getLogger(__name__)
    if migration_applied(conn, NAME):
        log.info("Migration skipped (already applied): %s", NAME)
        return
    log.info("Migration start: %s", NAME)
    from services.db.schema import clientplatform_external_products

    clientplatform_external_products.ensure(conn)
    mark_migration(conn, NAME)
    log.info("Migration applied: %s", NAME)


__all__ = ["NAME", "apply"]
