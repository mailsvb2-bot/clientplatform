from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from clientplatform.infrastructure.offering_process_repository import OfferingProcessRepository
from services.migrations._helpers import mark_migration, migration_applied


NAME = "clientplatform_offering_process_backfill_v1"


@dataclass(frozen=True, slots=True)
class OfferingProcessBackfillReport:
    candidates_scanned: int
    processes_ensured: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def reconcile_legacy_active_offering_processes(
    conn: sqlite3.Connection,
    *,
    now: str | None = None,
) -> OfferingProcessBackfillReport:
    """Ensure rebuildable process state exists for pre-lifecycle active offerings."""

    timestamp = str(now or _utc_now())
    rows = conn.execute(
        """
        SELECT bo.id, bo.business_id, bo.created_by_member_id
        FROM business_offerings bo
        LEFT JOIN business_offering_processes bop
          ON bop.offering_id=bo.id AND bop.business_id=bo.business_id
        WHERE bo.status='active' AND bop.offering_id IS NULL
        ORDER BY bo.business_id, bo.id
        """
    ).fetchall()
    repository = OfferingProcessRepository(conn)
    ensured = 0
    for row in rows:
        repository.ensure(
            business_id=str(_value(row, "business_id", 1)),
            offering_id=str(_value(row, "id", 0)),
            created_by_member_id=str(_value(row, "created_by_member_id", 2)),
            now=timestamp,
        )
        ensured += 1
    return OfferingProcessBackfillReport(
        candidates_scanned=len(rows),
        processes_ensured=ensured,
    )


def apply(conn: sqlite3.Connection) -> None:
    log = logging.getLogger(__name__)
    if migration_applied(conn, NAME):
        log.info("Migration skipped (already applied): %s", NAME)
        return
    log.info("Migration start: %s", NAME)
    report = reconcile_legacy_active_offering_processes(conn)
    mark_migration(conn, NAME)
    log.info(
        "Migration applied: %s candidates=%s ensured=%s",
        NAME,
        report.candidates_scanned,
        report.processes_ensured,
    )


__all__ = [
    "NAME",
    "OfferingProcessBackfillReport",
    "apply",
    "reconcile_legacy_active_offering_processes",
]
