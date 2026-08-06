from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from services.db import get_db_ro


def _timestamp(value: datetime | str | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def load_ad_spend_operation_snapshot(
    *,
    stale_lock_seconds: int = 300,
    dead_window_seconds: int = 900,
    now: datetime | str | None = None,
) -> dict[str, int | bool]:
    current = _timestamp(now)
    stale_before = current - timedelta(seconds=max(30, int(stale_lock_seconds)))
    dead_since = current - timedelta(seconds=max(60, int(dead_window_seconds)))

    counts = {
        "queued": 0,
        "processing": 0,
        "retry": 0,
        "succeeded": 0,
        "failed": 0,
    }
    with get_db_ro() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS total FROM ad_spend_operations GROUP BY status"
        ).fetchall()
        for row in rows:
            status = str(_value(row, "status", 0))
            if status in counts:
                counts[status] = int(_value(row, "total", 1) or 0)

        due_row = conn.execute(
            """
            SELECT COUNT(*) AS total, MIN(available_at) AS oldest_due
            FROM ad_spend_operations
            WHERE status IN ('queued', 'retry') AND available_at<=?
            """,
            (_iso(current),),
        ).fetchone()
        stale_row = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM ad_spend_operations
            WHERE status='processing' AND locked_at IS NOT NULL AND locked_at<?
            """,
            (_iso(stale_before),),
        ).fetchone()
        recent_failed_row = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM ad_spend_operations
            WHERE status='failed' AND dead_at IS NOT NULL AND dead_at>=?
            """,
            (_iso(dead_since),),
        ).fetchone()

    due = int(_value(due_row, "total", 0) or 0) if due_row is not None else 0
    oldest_due_age_seconds = 0
    if due_row is not None:
        oldest_raw = _value(due_row, "oldest_due", 1)
        if oldest_raw:
            oldest = _timestamp(str(oldest_raw))
            oldest_due_age_seconds = max(0, int((current - oldest).total_seconds()))
    stale_processing = (
        int(_value(stale_row, "total", 0) or 0) if stale_row is not None else 0
    )
    recent_failed = (
        int(_value(recent_failed_row, "total", 0) or 0)
        if recent_failed_row is not None
        else 0
    )
    return {
        "clientplatform_ad_spend_outbox_available": True,
        "clientplatform_ad_spend_outbox_queued": counts["queued"],
        "clientplatform_ad_spend_outbox_processing": counts["processing"],
        "clientplatform_ad_spend_outbox_retry": counts["retry"],
        "clientplatform_ad_spend_outbox_succeeded": counts["succeeded"],
        "clientplatform_ad_spend_outbox_failed": counts["failed"],
        "clientplatform_ad_spend_outbox_due": due,
        "clientplatform_ad_spend_outbox_stale_processing": stale_processing,
        "clientplatform_ad_spend_outbox_recent_failed": recent_failed,
        "clientplatform_ad_spend_outbox_oldest_due_age_seconds": (
            oldest_due_age_seconds
        ),
    }


__all__ = ["load_ad_spend_operation_snapshot"]
