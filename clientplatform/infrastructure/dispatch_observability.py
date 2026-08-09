from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from services.db import get_connection


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, 'keys'):
        return row[key]
    return row[position]


def _count(row: Any, key: str, position: int) -> int:
    value = _value(row, key, position)
    return max(0, int(value or 0))


def _parse_timestamp(value: Any) -> datetime | None:
    normalized = str(value or '').strip()
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_dispatch_outbox_snapshot(
    *,
    now: datetime | None = None,
    stale_lock_seconds: int = 900,
    dead_window_seconds: int = 900,
) -> dict[str, Any]:
    """Return aggregate provider-dispatch pressure without tenant/payload data."""

    current = (now or _utc_now()).astimezone(timezone.utc).replace(microsecond=0)
    current_iso = current.isoformat()
    stale_before = (
        current - timedelta(seconds=max(1, int(stale_lock_seconds)))
    ).isoformat()
    dead_after = (
        current - timedelta(seconds=max(1, int(dead_window_seconds)))
    ).isoformat()

    with get_connection() as conn:
        row = conn.execute(
            """
            WITH dispatch_rows AS (
                SELECT status,available_at,locked_at,dead_at
                FROM delivery_dispatch_outbox
                UNION ALL
                SELECT status,available_at,locked_at,dead_at
                FROM provider_dispatch_outbox
            )
            SELECT
                COALESCE(SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END), 0)
                    AS pending_count,
                COALESCE(SUM(CASE WHEN status='retry' THEN 1 ELSE 0 END), 0)
                    AS retry_count,
                COALESCE(SUM(CASE WHEN status='sending' THEN 1 ELSE 0 END), 0)
                    AS sending_count,
                COALESCE(SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END), 0)
                    AS sent_count,
                COALESCE(SUM(CASE WHEN status='dead' THEN 1 ELSE 0 END), 0)
                    AS dead_count,
                COALESCE(SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END), 0)
                    AS cancelled_count,
                COALESCE(SUM(CASE
                    WHEN status IN ('pending','retry') AND available_at<=? THEN 1
                    ELSE 0 END), 0) AS due_count,
                COALESCE(SUM(CASE
                    WHEN status='sending' AND locked_at IS NOT NULL AND locked_at<=? THEN 1
                    ELSE 0 END), 0) AS stale_sending_count,
                COALESCE(SUM(CASE
                    WHEN status='dead' AND dead_at IS NOT NULL AND dead_at>=? THEN 1
                    ELSE 0 END), 0) AS recent_dead_count,
                MIN(CASE
                    WHEN status IN ('pending','retry') AND available_at<=? THEN available_at
                    ELSE NULL END) AS oldest_due_at
            FROM dispatch_rows
            """,
            (current_iso, stale_before, dead_after, current_iso),
        ).fetchone()

    if row is None:
        raise RuntimeError('clientplatform_dispatch_outbox_aggregate_unavailable')

    oldest_due_at = _parse_timestamp(_value(row, 'oldest_due_at', 9))
    oldest_due_age_seconds = 0
    if oldest_due_at is not None:
        oldest_due_age_seconds = max(0, int((current - oldest_due_at).total_seconds()))

    return {
        'clientplatform_dispatch_outbox_available': True,
        'clientplatform_dispatch_outbox_pending': _count(row, 'pending_count', 0),
        'clientplatform_dispatch_outbox_retry': _count(row, 'retry_count', 1),
        'clientplatform_dispatch_outbox_sending': _count(row, 'sending_count', 2),
        'clientplatform_dispatch_outbox_sent': _count(row, 'sent_count', 3),
        'clientplatform_dispatch_outbox_dead': _count(row, 'dead_count', 4),
        'clientplatform_dispatch_outbox_cancelled': _count(row, 'cancelled_count', 5),
        'clientplatform_dispatch_outbox_due': _count(row, 'due_count', 6),
        'clientplatform_dispatch_outbox_stale_sending': _count(row, 'stale_sending_count', 7),
        'clientplatform_dispatch_outbox_recent_dead': _count(row, 'recent_dead_count', 8),
        'clientplatform_dispatch_outbox_oldest_due_age_seconds': oldest_due_age_seconds,
        'clientplatform_dispatch_outbox_error': '',
    }