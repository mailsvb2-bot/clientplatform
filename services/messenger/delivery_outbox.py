from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from core.time_utils import utc_now, utc_now_iso
from runtime.messenger_senders import provider_delivery_scope
from services.db import db, tx
from services.db.runtime import CONFIG
from services.events import log_event
from services.messenger.observability import log_action_completed
from services.messenger.reply_dispatcher import send_reply_bundle
from services.messenger.text_ui import MessengerReply

log = logging.getLogger(__name__)
_ALLOWED_PLATFORMS = {"vk", "max"}

_POSTGRES_CLAIM_SQL = """
WITH due AS (
    SELECT id
    FROM messenger_delivery_outbox
    WHERE (
        (status IN ('pending','retry') AND available_at<=?)
        OR (status='sending' AND locked_at IS NOT NULL AND locked_at<=?)
    )
    ORDER BY id ASC
    LIMIT ?
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

_SQLITE_SELECT_DUE_SQL = """
SELECT id
FROM messenger_delivery_outbox
WHERE (
    (status IN ('pending','retry') AND available_at<=?)
    OR (status='sending' AND locked_at IS NOT NULL AND locked_at<=?)
)
ORDER BY id ASC
LIMIT ?
""".strip()


@dataclass(frozen=True)
class ClaimedDelivery:
    id: int
    platform: str
    external_user_id: str
    canonical_user_id: int
    event_key: str
    action: str
    replies_json: str
    attempts: int
    lock_token: str


def _row_value(row: Any, key: str, index: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[index]


def _positive_int(name: str, default: int, *, minimum: int = 1, maximum: int = 86_400) -> int:
    raw = (os.getenv(name) or str(default)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = int(default)
    return min(max(value, minimum), maximum)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def serialize_replies(replies: list[MessengerReply]) -> str:
    return json.dumps(
        [
            {
                "kind": str(reply.kind or "text"),
                "text": str(reply.text or ""),
                "meta": _json_safe(dict(reply.meta or {})),
            }
            for reply in replies
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def deserialize_replies(raw: str) -> list[MessengerReply]:
    loaded = json.loads(str(raw or "[]"))
    if not isinstance(loaded, list):
        raise ValueError("messenger outbox replies_json must be a list")
    replies: list[MessengerReply] = []
    for item in loaded:
        if not isinstance(item, dict):
            raise ValueError("messenger outbox reply must be an object")
        meta = item.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        replies.append(
            MessengerReply(
                kind=str(item.get("kind") or "text"),
                text=str(item.get("text") or ""),
                meta={str(key): _json_safe(value) for key, value in meta.items()},
            )
        )
    return replies


def persist_reply_bundle(
    *,
    platform: str,
    external_user_id: str,
    canonical_user_id: int,
    event_key: str,
    replies: list[MessengerReply],
    action: str,
) -> bool:
    normalized_platform = str(platform or "").strip().lower()
    if normalized_platform not in _ALLOWED_PLATFORMS:
        raise ValueError(f"unsupported messenger outbox platform: {normalized_platform!r}")
    normalized_event_key = str(event_key or "").strip()
    normalized_external_id = str(external_user_id or "").strip()
    if not normalized_event_key:
        raise ValueError("messenger outbox event_key is required")
    if not normalized_external_id:
        raise ValueError("messenger outbox external_user_id is required")

    now = utc_now_iso()
    encoded = serialize_replies(list(replies))
    values = (
        normalized_platform,
        normalized_external_id,
        int(canonical_user_id),
        normalized_event_key,
        str(action or "")[:240],
        encoded,
        now,
        now,
        now,
    )
    with db() as conn:
        with tx(conn):
            insert_sql = (
                "INSERT INTO messenger_delivery_outbox("
                "platform,external_user_id,canonical_user_id,event_key,action,replies_json,"
                "status,attempts,available_at,locked_at,lock_token,last_error,created_at,updated_at,sent_at"
                ") VALUES(?,?,?,?,?,?, 'pending',0,?,NULL,NULL,'',?,?,NULL) "
                "ON CONFLICT (platform,event_key) DO NOTHING"
                if CONFIG.uses_postgres
                else
                "INSERT OR IGNORE INTO messenger_delivery_outbox("
                "platform,external_user_id,canonical_user_id,event_key,action,replies_json,"
                "status,attempts,available_at,locked_at,lock_token,last_error,created_at,updated_at,sent_at"
                ") VALUES(?,?,?,?,?,?, 'pending',0,?,NULL,NULL,'',?,?,NULL)"
            )
            cursor = conn.execute(insert_sql, values)
            inserted = int(getattr(cursor, "rowcount", 0) or 0) == 1
            conn.execute(
                "UPDATE messenger_webhook_events "
                "SET status='completed',completed_at=?,updated_at=?,last_error='' "
                "WHERE platform=? AND event_key=?",
                (now, now, normalized_platform, normalized_event_key),
            )
    return inserted


def _claimed_from_rows(rows: list[Any], token: str) -> list[ClaimedDelivery]:
    return [
        ClaimedDelivery(
            id=int(_row_value(row, "id", 0)),
            platform=str(_row_value(row, "platform", 1)),
            external_user_id=str(_row_value(row, "external_user_id", 2)),
            canonical_user_id=int(_row_value(row, "canonical_user_id", 3)),
            event_key=str(_row_value(row, "event_key", 4)),
            action=str(_row_value(row, "action", 5) or ""),
            replies_json=str(_row_value(row, "replies_json", 6) or "[]"),
            attempts=int(_row_value(row, "attempts", 7) or 0),
            lock_token=str(_row_value(row, "lock_token", 8) or token),
        )
        for row in rows
    ]


def claim_due_deliveries(*, limit: int = 1, lock_ttl_sec: int = 900) -> list[ClaimedDelivery]:
    """Atomically lease due work, including abandoned ``sending`` rows."""

    now = utc_now().replace(microsecond=0)
    now_iso = now.isoformat()
    stale_before = (now - timedelta(seconds=max(1, int(lock_ttl_sec)))).isoformat()
    token = uuid.uuid4().hex

    with db() as conn:
        with tx(conn):
            if CONFIG.uses_postgres:
                rows = conn.execute(
                    _POSTGRES_CLAIM_SQL,
                    (now_iso, stale_before, int(limit), now_iso, token, now_iso),
                ).fetchall()
                return _claimed_from_rows(list(rows), token)

            rows = conn.execute(
                _SQLITE_SELECT_DUE_SQL,
                (now_iso, stale_before, int(limit)),
            ).fetchall()
            ids = [int(_row_value(row, "id", 0)) for row in rows]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                "UPDATE messenger_delivery_outbox "
                "SET status='sending',locked_at=?,lock_token=?,updated_at=? "
                f"WHERE id IN ({placeholders}) AND "  # nosec B608 - placeholders are generated, never user input
                "((status IN ('pending','retry') AND available_at<=?) "
                "OR (status='sending' AND locked_at IS NOT NULL AND locked_at<=?))",
                [now_iso, token, now_iso, *ids, now_iso, stale_before],
            )
            claimed = conn.execute(
                "SELECT id,platform,external_user_id,canonical_user_id,event_key,action,replies_json,attempts,lock_token "
                "FROM messenger_delivery_outbox WHERE lock_token=? "
                f"AND id IN ({placeholders}) ORDER BY id",  # nosec B608 - placeholders are generated, never user input
                [token, *ids],
            ).fetchall()
            return _claimed_from_rows(list(claimed), token)


def mark_delivery_sent(item: ClaimedDelivery) -> None:
    now = utc_now_iso()
    with db() as conn:
        with tx(conn):
            conn.execute(
                "UPDATE messenger_delivery_outbox "
                "SET status='sent',sent_at=?,updated_at=?,locked_at=NULL,lock_token=NULL,last_error='' "
                "WHERE id=? AND lock_token=?",
                (now, now, int(item.id), item.lock_token),
            )


def release_delivery_lease(item: ClaimedDelivery, *, reason: str = "worker_shutdown") -> None:
    now = utc_now_iso()
    with db() as conn:
        with tx(conn):
            conn.execute(
                "UPDATE messenger_delivery_outbox "
                "SET status='retry',available_at=?,updated_at=?,locked_at=NULL,lock_token=NULL,last_error=? "
                "WHERE id=? AND lock_token=?",
                (now, now, str(reason or "worker_shutdown")[:500], int(item.id), item.lock_token),
            )


def mark_delivery_rejected(item: ClaimedDelivery, error: str) -> None:
    """Settle an explicitly rejected provider write without retrying it.

    Rejected is terminal evidence: the provider proved that this attempt did
    not create a message. It must not be replayed, and unlike an ambiguous dead
    letter it must not make the whole messenger runtime unready.
    """

    attempts = int(item.attempts) + 1
    now = utc_now_iso()
    safe_error = str(error or "")[:500]
    with db() as conn:
        with tx(conn):
            conn.execute(
                "UPDATE messenger_delivery_outbox "
                "SET status='rejected',attempts=?,updated_at=?,locked_at=NULL,"
                "lock_token=NULL,last_error=? "
                "WHERE id=? AND lock_token=?",
                (attempts, now, safe_error, int(item.id), item.lock_token),
            )
    log_event(
        item.canonical_user_id,
        f"{item.platform}_delivery_rejected",
        {
            "event_key": item.event_key,
            "attempts": attempts,
            "error": safe_error[:180],
        },
    )


def reschedule_delivery(item: ClaimedDelivery, error: str) -> None:
    attempts = int(item.attempts) + 1
    max_attempts = _positive_int("MESSENGER_OUTBOX_MAX_ATTEMPTS", 8, minimum=1, maximum=100)
    now = utc_now().replace(microsecond=0)
    terminal = attempts >= max_attempts
    delay = min(5 * (2 ** max(0, attempts - 1)), 900)
    available_at = (now + timedelta(seconds=delay)).isoformat()
    status = "dead" if terminal else "retry"
    with db() as conn:
        with tx(conn):
            conn.execute(
                "UPDATE messenger_delivery_outbox "
                "SET status=?,attempts=?,available_at=?,updated_at=?,locked_at=NULL,lock_token=NULL,last_error=? "
                "WHERE id=? AND lock_token=?",
                (status, attempts, available_at, now.isoformat(), str(error or "")[:500], int(item.id), item.lock_token),
            )
    if terminal:
        log_event(
            item.canonical_user_id,
            f"{item.platform}_delivery_dead_letter",
            {"event_key": item.event_key, "attempts": attempts, "error": str(error or "")[:180]},
        )


def reply_progress_index(outbox_id: int) -> int:
    with db() as conn:
        row = conn.execute(
            "SELECT next_reply_index FROM messenger_delivery_reply_progress WHERE outbox_id=?",
            (int(outbox_id),),
        ).fetchone()
    if row is None:
        return 0
    return max(0, int(_row_value(row, "next_reply_index", 0) or 0))


def checkpoint_reply_progress(item: ClaimedDelivery, next_reply_index: int) -> None:
    """Persist the first not-yet-delivered reply while the lease is still owned."""

    index = max(0, int(next_reply_index))
    now = utc_now_iso()
    with db() as conn:
        with tx(conn):
            cursor = conn.execute(
                "UPDATE messenger_delivery_outbox SET updated_at=? "
                "WHERE id=? AND lock_token=? AND status='sending'",
                (now, int(item.id), item.lock_token),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise RuntimeError("messenger_delivery_lease_lost")
            conn.execute(
                """
                INSERT INTO messenger_delivery_reply_progress(outbox_id,next_reply_index,updated_at)
                VALUES(?,?,?)
                ON CONFLICT(outbox_id) DO UPDATE SET
                    next_reply_index=CASE
                        WHEN excluded.next_reply_index > messenger_delivery_reply_progress.next_reply_index
                        THEN excluded.next_reply_index
                        ELSE messenger_delivery_reply_progress.next_reply_index
                    END,
                    updated_at=excluded.updated_at
                """.strip(),
                (int(item.id), index, now),
            )


async def _deliver_one(item: ClaimedDelivery) -> None:
    replies = deserialize_replies(item.replies_json)
    start_index = await asyncio.to_thread(reply_progress_index, item.id)
    if start_index > len(replies):
        raise ValueError("messenger_delivery_reply_progress_out_of_range")

    for reply_index in range(start_index, len(replies)):
        with provider_delivery_scope(f"{item.platform}:{item.event_key}:{reply_index + 1}"):
            await send_reply_bundle(
                item.platform,
                item.external_user_id,
                item.canonical_user_id,
                [replies[reply_index]],
            )
        await asyncio.to_thread(checkpoint_reply_progress, item, reply_index + 1)

    await asyncio.to_thread(mark_delivery_sent, item)
    await asyncio.to_thread(
        log_action_completed,
        platform=item.platform,
        user_id=item.canonical_user_id,
        action=item.action,
        replies=len(replies),
        status="ok",
    )


def start_delivery_worker() -> asyncio.Task:
    """Compatibility facade; the pool is the only worker implementation."""

    from services.messenger.delivery_pool import start_delivery_worker as start_pool

    return start_pool()


async def stop_delivery_worker() -> None:
    """Compatibility facade; the pool is the only worker implementation."""

    from services.messenger.delivery_pool import stop_delivery_worker as stop_pool

    await stop_pool()


def outbox_snapshot() -> dict[str, int]:
    with db() as conn:
        rows = conn.execute(
            "SELECT status,COUNT(*) AS count FROM messenger_delivery_outbox GROUP BY status"
        ).fetchall()
    counts = {str(_row_value(row, "status", 0)): int(_row_value(row, "count", 1) or 0) for row in rows}
    return {
        "pending": counts.get("pending", 0),
        "retry": counts.get("retry", 0),
        "sending": counts.get("sending", 0),
        "sent": counts.get("sent", 0),
        "rejected": counts.get("rejected", 0),
        "dead": counts.get("dead", 0),
    }
