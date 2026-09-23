from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from core.time_utils import utc_now, utc_now_iso
from services.bg import tm
from services.db import db, tx
from services.db.runtime import CONFIG
from services.messenger import delivery_outbox
from runtime.messenger_transport_errors import safe_transport_error_text

log = logging.getLogger(__name__)
_ALLOWED_PLATFORMS = ("vk", "max")

_pool_task: asyncio.Task | None = None
_pool_stop: asyncio.Event | None = None
_worker_tasks: list[asyncio.Task] = []
_metrics_lock = threading.Lock()
_metrics: dict[str, int | str] = {
    "delivered": 0,
    "retried": 0,
    "dead": 0,
    "rejected": 0,
    "leases_released": 0,
    "sent_deleted": 0,
    "dead_deleted": 0,
    "webhook_deleted": 0,
    "cleanup_runs": 0,
    "last_cleanup_at": "",
}

_POSTGRES_STREAM_HEAD_SQL = """
WITH due AS (
    SELECT candidate.id
    FROM messenger_delivery_outbox AS candidate
    WHERE candidate.platform=?
      AND (
        (candidate.status IN ('pending','retry') AND candidate.available_at<=?)
        OR (candidate.status='sending' AND candidate.locked_at IS NOT NULL AND candidate.locked_at<=?)
      )
      AND NOT EXISTS (
        SELECT 1
        FROM messenger_delivery_outbox AS older
        WHERE older.platform=candidate.platform
          AND older.canonical_user_id=candidate.canonical_user_id
          AND older.id<candidate.id
          AND older.status IN ('pending','retry','sending')
      )
    ORDER BY candidate.id ASC
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
UPDATE messenger_delivery_outbox
SET status='sending', locked_at=?, lock_token=?, updated_at=?
FROM due
WHERE messenger_delivery_outbox.id=due.id
RETURNING messenger_delivery_outbox.id, messenger_delivery_outbox.platform,
          messenger_delivery_outbox.external_user_id,
          messenger_delivery_outbox.canonical_user_id,
          messenger_delivery_outbox.event_key, messenger_delivery_outbox.action,
          messenger_delivery_outbox.replies_json, messenger_delivery_outbox.attempts,
          messenger_delivery_outbox.lock_token
""".strip()

_SQLITE_STREAM_HEAD_SQL = """
SELECT candidate.id
FROM messenger_delivery_outbox AS candidate
WHERE candidate.platform=?
  AND (
    (candidate.status IN ('pending','retry') AND candidate.available_at<=?)
    OR (candidate.status='sending' AND candidate.locked_at IS NOT NULL AND candidate.locked_at<=?)
  )
  AND NOT EXISTS (
    SELECT 1
    FROM messenger_delivery_outbox AS older
    WHERE older.platform=candidate.platform
      AND older.canonical_user_id=candidate.canonical_user_id
      AND older.id<candidate.id
      AND older.status IN ('pending','retry','sending')
  )
ORDER BY candidate.id ASC
LIMIT 1
""".strip()


@dataclass(frozen=True)
class RetentionResult:
    sent_deleted: int = 0
    dead_deleted: int = 0
    webhook_deleted: int = 0


def _bounded_int(
    name: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: int = 1_000_000,
) -> int:
    raw = str(os.getenv(name) or default).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = int(default)
    return min(max(value, minimum), maximum)


def configured_worker_counts() -> dict[str, int]:
    return {
        "vk": _bounded_int("MESSENGER_OUTBOX_VK_WORKERS", 2, minimum=1, maximum=32),
        "max": _bounded_int("MESSENGER_OUTBOX_MAX_WORKERS", 2, minimum=1, maximum=32),
    }


def _metric_add(name: str, amount: int = 1) -> None:
    with _metrics_lock:
        current = int(_metrics.get(name, 0) or 0)
        _metrics[name] = current + int(amount)


def _metric_set(name: str, value: int | str) -> None:
    with _metrics_lock:
        _metrics[name] = value


def _claimed_from_row(row: Any, token: str) -> delivery_outbox.ClaimedDelivery:
    def value(key: str, index: int) -> Any:
        return row[key] if hasattr(row, "keys") else row[index]

    return delivery_outbox.ClaimedDelivery(
        id=int(value("id", 0)),
        platform=str(value("platform", 1)),
        external_user_id=str(value("external_user_id", 2)),
        canonical_user_id=int(value("canonical_user_id", 3)),
        event_key=str(value("event_key", 4)),
        action=str(value("action", 5) or ""),
        replies_json=str(value("replies_json", 6) or "[]"),
        attempts=int(value("attempts", 7) or 0),
        lock_token=str(value("lock_token", 8) or token),
    )


def claim_stream_head(
    *,
    platform: str,
    lock_ttl_sec: int = 900,
) -> delivery_outbox.ClaimedDelivery | None:
    """Lease one due row while preserving order inside a user/platform stream."""

    normalized = str(platform or "").strip().lower()
    if normalized not in _ALLOWED_PLATFORMS:
        raise ValueError(f"unsupported delivery platform: {normalized!r}")

    now = utc_now().replace(microsecond=0)
    now_iso = now.isoformat()
    stale_before = (now - timedelta(seconds=max(1, int(lock_ttl_sec)))).isoformat()
    import uuid

    token = uuid.uuid4().hex
    with db() as conn:
        with tx(conn):
            if CONFIG.uses_postgres:
                row = conn.execute(
                    _POSTGRES_STREAM_HEAD_SQL,
                    (normalized, now_iso, stale_before, now_iso, token, now_iso),
                ).fetchone()
                return _claimed_from_row(row, token) if row is not None else None

            selected = conn.execute(
                _SQLITE_STREAM_HEAD_SQL,
                (normalized, now_iso, stale_before),
            ).fetchone()
            if selected is None:
                return None
            selected_id = int(selected["id"] if hasattr(selected, "keys") else selected[0])
            cursor = conn.execute(
                """
                UPDATE messenger_delivery_outbox
                SET status='sending', locked_at=?, lock_token=?, updated_at=?
                WHERE id=? AND platform=?
                  AND (
                    (status IN ('pending','retry') AND available_at<=?)
                    OR (status='sending' AND locked_at IS NOT NULL AND locked_at<=?)
                  )
                """.strip(),
                (now_iso, token, now_iso, selected_id, normalized, now_iso, stale_before),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                return None
            row = conn.execute(
                """
                SELECT id,platform,external_user_id,canonical_user_id,event_key,
                       action,replies_json,attempts,lock_token
                FROM messenger_delivery_outbox
                WHERE id=? AND lock_token=?
                """.strip(),
                (selected_id, token),
            ).fetchone()
            return _claimed_from_row(row, token) if row is not None else None


def _delete_ids(conn: Any, table: str, ids: list[int]) -> int:
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    cursor = conn.execute(
        f"DELETE FROM {table} WHERE id IN ({placeholders})",  # nosec B608 - private table constants and generated placeholders
        tuple(int(item) for item in ids),
    )
    return max(0, int(getattr(cursor, "rowcount", 0) or 0))


def _delete_webhook_keys(conn: Any, keys: list[tuple[str, str]]) -> int:
    deleted = 0
    for platform, event_key in keys:
        cursor = conn.execute(
            "DELETE FROM messenger_webhook_events WHERE platform=? AND event_key=?",
            (str(platform), str(event_key)),
        )
        deleted += max(0, int(getattr(cursor, "rowcount", 0) or 0))
    return deleted


def cleanup_delivery_history(
    *,
    sent_retention_days: int | None = None,
    dead_retention_days: int | None = None,
    webhook_retention_days: int | None = None,
    batch_size: int | None = None,
) -> RetentionResult:
    """Delete bounded batches of terminal delivery evidence after retention."""

    sent_days = int(
        sent_retention_days
        if sent_retention_days is not None
        else _bounded_int("MESSENGER_OUTBOX_SENT_RETENTION_DAYS", 30, minimum=1, maximum=3650)
    )
    dead_days = int(
        dead_retention_days
        if dead_retention_days is not None
        else _bounded_int("MESSENGER_OUTBOX_DEAD_RETENTION_DAYS", 180, minimum=7, maximum=3650)
    )
    webhook_days = int(
        webhook_retention_days
        if webhook_retention_days is not None
        else _bounded_int("MESSENGER_WEBHOOK_EVENT_RETENTION_DAYS", 30, minimum=1, maximum=3650)
    )
    limit = int(
        batch_size
        if batch_size is not None
        else _bounded_int("MESSENGER_OUTBOX_RETENTION_BATCH", 500, minimum=1, maximum=10_000)
    )
    now = utc_now().replace(microsecond=0)
    sent_before = (now - timedelta(days=sent_days)).isoformat()
    dead_before = (now - timedelta(days=dead_days)).isoformat()
    webhook_before = (now - timedelta(days=webhook_days)).isoformat()

    with db() as conn:
        with tx(conn):
            sent_rows = conn.execute(
                """
                SELECT id FROM messenger_delivery_outbox
                WHERE status='sent' AND COALESCE(sent_at,updated_at,created_at)<?
                ORDER BY id ASC LIMIT ?
                """.strip(),
                (sent_before, limit),
            ).fetchall()
            sent_ids = [int(row["id"] if hasattr(row, "keys") else row[0]) for row in sent_rows]
            sent_deleted = _delete_ids(conn, "messenger_delivery_outbox", sent_ids)

            dead_rows = conn.execute(
                """
                SELECT id FROM messenger_delivery_outbox
                WHERE (status='dead' OR status='rejected') AND updated_at<?
                ORDER BY id ASC LIMIT ?
                """.strip(),
                (dead_before, limit),
            ).fetchall()
            dead_ids = [int(row["id"] if hasattr(row, "keys") else row[0]) for row in dead_rows]
            dead_deleted = _delete_ids(conn, "messenger_delivery_outbox", dead_ids)

            webhook_rows = conn.execute(
                """
                SELECT platform,event_key FROM messenger_webhook_events
                WHERE status='completed' AND COALESCE(completed_at,updated_at,received_at)<?
                ORDER BY received_at ASC LIMIT ?
                """.strip(),
                (webhook_before, limit),
            ).fetchall()
            webhook_keys = [
                (
                    str(row["platform"] if hasattr(row, "keys") else row[0]),
                    str(row["event_key"] if hasattr(row, "keys") else row[1]),
                )
                for row in webhook_rows
            ]
            webhook_deleted = _delete_webhook_keys(conn, webhook_keys)

    result = RetentionResult(
        sent_deleted=sent_deleted,
        dead_deleted=dead_deleted,
        webhook_deleted=webhook_deleted,
    )
    _metric_add("sent_deleted", result.sent_deleted)
    _metric_add("dead_deleted", result.dead_deleted)
    _metric_add("webhook_deleted", result.webhook_deleted)
    _metric_add("cleanup_runs", 1)
    _metric_set("last_cleanup_at", utc_now_iso())
    return result


async def _process_item(item: delivery_outbox.ClaimedDelivery) -> None:
    try:
        await delivery_outbox._deliver_one(item)  # noqa: SLF001 - pool owns the durable delivery execution
        _metric_add("delivered", 1)
    except asyncio.CancelledError:
        await asyncio.to_thread(delivery_outbox.release_delivery_lease, item)
        _metric_add("leases_released", 1)
        raise
    except Exception as exc:  # validator: allow-wide-except
        safe_error = safe_transport_error_text(exc)
        log.error(
            "%s durable delivery failed event_key=%s attempt=%s error=%s",
            item.platform.upper(),
            item.event_key,
            int(item.attempts) + 1,
            safe_error,
        )
        permanent_rejection = bool(
            getattr(exc, "provider_write_definitely_rejected", False)
            and getattr(exc, "retryable", None) is False
        )
        if permanent_rejection:
            await asyncio.to_thread(
                delivery_outbox.mark_delivery_rejected,
                item,
                safe_error,
            )
            _metric_add("rejected", 1)
            return

        await asyncio.to_thread(
            delivery_outbox.reschedule_delivery,
            item,
            safe_error,
        )
        max_attempts = _bounded_int(
            "MESSENGER_OUTBOX_MAX_ATTEMPTS",
            8,
            minimum=1,
            maximum=100,
        )
        if int(item.attempts) + 1 >= max_attempts:
            _metric_add("dead", 1)
        else:
            _metric_add("retried", 1)


async def _platform_worker(
    *,
    platform: str,
    worker_no: int,
    stop_event: asyncio.Event,
) -> None:
    try:
        idle_sleep = max(0.1, float(os.getenv("MESSENGER_OUTBOX_IDLE_SLEEP_SEC", "0.5") or "0.5"))
    except ValueError:
        idle_sleep = 0.5
    lock_ttl = _bounded_int("MESSENGER_OUTBOX_LOCK_TTL_SEC", 900, minimum=30, maximum=86_400)

    while not stop_event.is_set():
        try:
            item = await asyncio.to_thread(
                claim_stream_head,
                platform=platform,
                lock_ttl_sec=lock_ttl,
            )
            if item is None:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=idle_sleep)
                except asyncio.TimeoutError:
                    pass
                continue
            if stop_event.is_set():
                await asyncio.to_thread(delivery_outbox.release_delivery_lease, item)
                _metric_add("leases_released", 1)
                return
            await _process_item(item)
        except asyncio.CancelledError:
            raise
        except Exception:  # validator: allow-wide-except
            log.exception("Messenger %s delivery worker %s tick failed", platform, worker_no)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=idle_sleep)
            except asyncio.TimeoutError:
                pass


async def _cleanup_loop(stop_event: asyncio.Event) -> None:
    interval = _bounded_int(
        "MESSENGER_OUTBOX_RETENTION_INTERVAL_SEC",
        3600,
        minimum=60,
        maximum=7 * 24 * 60 * 60,
    )
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(cleanup_delivery_history)
        except Exception:  # validator: allow-wide-except
            log.exception("Messenger delivery retention tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=float(interval))
        except asyncio.TimeoutError:
            pass


async def _pool_main(stop_event: asyncio.Event) -> None:
    global _worker_tasks
    counts = configured_worker_counts()
    tasks: list[asyncio.Task] = []
    for platform in _ALLOWED_PLATFORMS:
        for worker_no in range(1, counts[platform] + 1):
            tasks.append(
                tm().create(
                    _platform_worker(
                        platform=platform,
                        worker_no=worker_no,
                        stop_event=stop_event,
                    ),
                    name=f"messenger_{platform}_delivery_worker_{worker_no}",
                )
            )
    tasks.append(
        tm().create(
            _cleanup_loop(stop_event),
            name="messenger_delivery_retention_worker",
        )
    )
    _worker_tasks = tasks
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        _worker_tasks = []


def start_delivery_worker() -> asyncio.Task:
    global _pool_task, _pool_stop
    if _pool_task is not None and not _pool_task.done():
        return _pool_task
    _pool_stop = asyncio.Event()
    _pool_task = tm().create(
        _pool_main(_pool_stop),
        name="messenger_durable_delivery_pool",
    )
    return _pool_task


async def stop_delivery_worker() -> None:
    global _pool_task, _pool_stop, _worker_tasks
    task = _pool_task
    if task is None:
        return
    if _pool_stop is not None:
        _pool_stop.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    _pool_task = None
    _pool_stop = None
    _worker_tasks = []


def worker_snapshot() -> dict[str, int | bool | str]:
    counts = configured_worker_counts()
    active_by_platform = {platform: 0 for platform in _ALLOWED_PLATFORMS}
    for task in list(_worker_tasks):
        if task.done():
            continue
        name = str(task.get_name() or "")
        for platform in _ALLOWED_PLATFORMS:
            if f"messenger_{platform}_delivery_worker_" in name:
                active_by_platform[platform] += 1
    expected = _pool_stop is not None
    pool_active = bool(_pool_task is not None and not _pool_task.done())
    with _metrics_lock:
        metrics = dict(_metrics)
    return {
        "worker_expected": expected,
        "worker_active": pool_active,
        "worker_running": pool_active if expected else True,
        "vk_workers_configured": counts["vk"],
        "max_workers_configured": counts["max"],
        "vk_workers_active": active_by_platform["vk"],
        "max_workers_active": active_by_platform["max"],
        **metrics,
    }
