from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from core.time_utils import utc_now
from services.db import db, tx
from services.db.runtime import CONFIG
from services.messenger.platforms import normalize_platform


@dataclass(frozen=True)
class InboundFailureResult:
    event_key: str
    attempts: int
    retryable: bool
    dead_lettered: bool
    recorded: bool


def _stable_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _event_key(event_key: str | None, payload: dict[str, Any]) -> str:
    return (event_key or "").strip() or _stable_hash(payload)


def _row_value(row: Any, key: str, index: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[index]


def _lock_event(conn: Any, *, platform: str, event_key: str) -> None:
    if CONFIG.uses_postgres:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(?)::bigint)",
            (f"messenger-inbound:{platform}:{event_key}",),
        )


def claim_inbound_event(
    platform: str,
    event_key: str | None,
    payload: dict[str, Any],
    *,
    stale_after_sec: int = 120,
) -> bool:
    """Claim one provider event for processing.

    Completed and dead-lettered events are permanent duplicates. Failed events
    and stale processing claims may be retried. A Postgres advisory transaction
    lock serializes claims for the same provider key across processes; SQLite
    uses its write transaction.
    """

    key = _event_key(event_key, payload)
    norm = normalize_platform(platform)
    now = utc_now().replace(microsecond=0)
    now_iso = now.isoformat()
    stale_before = (now - timedelta(seconds=max(1, int(stale_after_sec)))).isoformat()
    payload_hash = _stable_hash(payload)

    with db() as conn:
        with tx(conn):
            _lock_event(conn, platform=norm, event_key=key)
            row = conn.execute(
                """
                SELECT status, updated_at, attempts
                FROM messenger_webhook_events
                WHERE platform=? AND event_key=?
                LIMIT 1
                """.strip(),
                (norm, key),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO messenger_webhook_events(
                        platform, event_key, received_at, payload_hash,
                        status, attempts, updated_at, completed_at, last_error
                    ) VALUES(?,?,?,?, 'processing',1,?,NULL,'')
                    """.strip(),
                    (norm, key, now_iso, payload_hash, now_iso),
                )
                return True

            status = str(_row_value(row, "status", 0) or "completed").strip().lower()
            updated_at = str(_row_value(row, "updated_at", 1) or "")
            attempts = int(_row_value(row, "attempts", 2) or 0)
            if status in {"completed", "dead_letter"}:
                return False
            if status == "processing" and updated_at and updated_at > stale_before:
                return False

            conn.execute(
                """
                UPDATE messenger_webhook_events
                SET status='processing', attempts=?, updated_at=?, payload_hash=?, last_error=''
                WHERE platform=? AND event_key=?
                """.strip(),
                (attempts + 1, now_iso, payload_hash, norm, key),
            )
            return True


def record_inbound_failure(
    platform: str,
    event_key: str | None,
    payload: dict[str, Any],
    error: str,
    *,
    max_attempts: int = 5,
) -> InboundFailureResult:
    """Persist an ingress failure and turn repeated poison events into dead letters.

    The first failures remain retryable so the provider can redeliver temporary
    or newly-supported payload shapes. Once ``max_attempts`` is reached, the
    event becomes a permanent dead letter and subsequent duplicates are
    acknowledged without incrementing the counter forever.
    """

    key = _event_key(event_key, payload)
    norm = normalize_platform(platform)
    limit = max(1, int(max_attempts))
    now = utc_now().replace(microsecond=0).isoformat()
    payload_hash = _stable_hash(payload)
    last_error = str(error or "unknown inbound failure")[:500]

    with db() as conn:
        with tx(conn):
            _lock_event(conn, platform=norm, event_key=key)
            row = conn.execute(
                """
                SELECT status, attempts
                FROM messenger_webhook_events
                WHERE platform=? AND event_key=?
                LIMIT 1
                """.strip(),
                (norm, key),
            ).fetchone()
            if row is not None:
                current_status = str(_row_value(row, "status", 0) or "").strip().lower()
                current_attempts = int(_row_value(row, "attempts", 1) or 0)
                if current_status in {"completed", "dead_letter"}:
                    return InboundFailureResult(
                        event_key=key,
                        attempts=current_attempts,
                        retryable=False,
                        dead_lettered=current_status == "dead_letter",
                        recorded=False,
                    )
                attempts = current_attempts + 1
            else:
                attempts = 1

            dead_lettered = attempts >= limit
            status = "dead_letter" if dead_lettered else "failed"
            completed_at = now if dead_lettered else None
            if row is None:
                conn.execute(
                    """
                    INSERT INTO messenger_webhook_events(
                        platform, event_key, received_at, payload_hash,
                        status, attempts, updated_at, completed_at, last_error
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """.strip(),
                    (norm, key, now, payload_hash, status, attempts, now, completed_at, last_error),
                )
            else:
                conn.execute(
                    """
                    UPDATE messenger_webhook_events
                    SET status=?, attempts=?, updated_at=?, payload_hash=?, completed_at=?, last_error=?
                    WHERE platform=? AND event_key=?
                    """.strip(),
                    (status, attempts, now, payload_hash, completed_at, last_error, norm, key),
                )

    return InboundFailureResult(
        event_key=key,
        attempts=attempts,
        retryable=not dead_lettered,
        dead_lettered=dead_lettered,
        recorded=True,
    )


def complete_inbound_event(platform: str, event_key: str | None, payload: dict[str, Any]) -> None:
    key = _event_key(event_key, payload)
    norm = normalize_platform(platform)
    now = utc_now().replace(microsecond=0).isoformat()
    with db() as conn:
        with tx(conn):
            conn.execute(
                """
                UPDATE messenger_webhook_events
                SET status='completed', completed_at=?, updated_at=?, last_error=''
                WHERE platform=? AND event_key=?
                """.strip(),
                (now, now, norm, key),
            )


def fail_inbound_event(platform: str, event_key: str | None, payload: dict[str, Any], error: str) -> None:
    key = _event_key(event_key, payload)
    norm = normalize_platform(platform)
    now = utc_now().replace(microsecond=0).isoformat()
    with db() as conn:
        with tx(conn):
            conn.execute(
                """
                UPDATE messenger_webhook_events
                SET status='failed', updated_at=?, last_error=?
                WHERE platform=? AND event_key=?
                """.strip(),
                (now, str(error or "")[:500], norm, key),
            )


def fail_claimed_inbound_event(
    platform: str,
    event_key: str | None,
    payload: dict[str, Any],
    error: str,
    *,
    max_attempts: int = 5,
    permanent: bool = False,
) -> InboundFailureResult:
    """Settle a claimed processing attempt without double-counting it.

    ``claim_inbound_event`` increments the attempt counter when it grants a
    lease. A failure settlement must therefore reuse that counter instead of
    incrementing it a second time. Permanent domain rejections are dead-lettered
    immediately so webhook providers receive HTTP 200 and do not amplify an
    unrecoverable user action through repeated delivery.
    """

    key = _event_key(event_key, payload)
    norm = normalize_platform(platform)
    limit = max(1, int(max_attempts))
    now = utc_now().replace(microsecond=0).isoformat()
    payload_hash = _stable_hash(payload)
    last_error = str(error or "unknown inbound failure")[:500]

    with db() as conn:
        with tx(conn):
            _lock_event(conn, platform=norm, event_key=key)
            row = conn.execute(
                """
                SELECT status, attempts
                FROM messenger_webhook_events
                WHERE platform=? AND event_key=?
                LIMIT 1
                """.strip(),
                (norm, key),
            ).fetchone()
            if row is not None:
                current_status = str(
                    _row_value(row, "status", 0) or ""
                ).strip().lower()
                attempts = max(1, int(_row_value(row, "attempts", 1) or 0))
                if current_status in {"completed", "dead_letter"}:
                    return InboundFailureResult(
                        event_key=key,
                        attempts=attempts,
                        retryable=False,
                        dead_lettered=current_status == "dead_letter",
                        recorded=False,
                    )
            else:
                attempts = 1

            dead_lettered = bool(permanent or attempts >= limit)
            status = "dead_letter" if dead_lettered else "failed"
            completed_at = now if dead_lettered else None
            if row is None:
                conn.execute(
                    """
                    INSERT INTO messenger_webhook_events(
                        platform, event_key, received_at, payload_hash,
                        status, attempts, updated_at, completed_at, last_error
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """.strip(),
                    (
                        norm,
                        key,
                        now,
                        payload_hash,
                        status,
                        attempts,
                        now,
                        completed_at,
                        last_error,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE messenger_webhook_events
                    SET status=?, updated_at=?, payload_hash=?, completed_at=?,
                        last_error=?
                    WHERE platform=? AND event_key=?
                    """.strip(),
                    (
                        status,
                        now,
                        payload_hash,
                        completed_at,
                        last_error,
                        norm,
                        key,
                    ),
                )

    return InboundFailureResult(
        event_key=key,
        attempts=attempts,
        retryable=not dead_lettered,
        dead_lettered=dead_lettered,
        recorded=True,
    )


def register_inbound_event(platform: str, event_key: str | None, payload: dict[str, Any]) -> bool:
    """Compatibility API: claim and immediately complete a dedupe-only event."""

    claimed = claim_inbound_event(platform, event_key, payload)
    if claimed:
        complete_inbound_event(platform, event_key, payload)
    return claimed
