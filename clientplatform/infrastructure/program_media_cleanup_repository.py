from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from config.settings import CONFIG
from clientplatform.domain.program_media import unwrap_program_media_reference
from clientplatform.domain.programs import normalize_content_ref
from clientplatform.domain.tenancy import normalize_uuid


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


@dataclass(frozen=True, slots=True)
class ProgramMediaCleanupJob:
    id: str
    business_id: str
    media_reference: str
    reason: str
    status: str
    attempts: int
    available_at: str
    locked_at: str | None
    lock_token: str | None
    last_error: str | None


_COLUMNS = (
    "id,business_id,media_reference,reason,status,attempts,available_at,"
    "locked_at,lock_token,last_error"
)


def _job(row: Any) -> ProgramMediaCleanupJob:
    return ProgramMediaCleanupJob(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        media_reference=str(_value(row, "media_reference", 2)),
        reason=str(_value(row, "reason", 3)),
        status=str(_value(row, "status", 4)),
        attempts=int(_value(row, "attempts", 5)),
        available_at=str(_value(row, "available_at", 6)),
        locked_at=(
            None
            if _value(row, "locked_at", 7) is None
            else str(_value(row, "locked_at", 7))
        ),
        lock_token=(
            None
            if _value(row, "lock_token", 8) is None
            else str(_value(row, "lock_token", 8))
        ),
        last_error=(
            None
            if _value(row, "last_error", 9) is None
            else str(_value(row, "last_error", 9))
        ),
    )


class ProgramMediaCleanupRepository:
    """Durable, idempotent and leased cleanup intents for private lesson media."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def enqueue(
        self,
        *,
        business_id: str,
        media_reference: str,
        reason: str,
        delay_seconds: int = 0,
        now: datetime | None = None,
    ) -> ProgramMediaCleanupJob:
        normalized_business_id = normalize_uuid(business_id, field_name="business_id")
        normalized_reference = normalize_content_ref(media_reference)
        storage_reference = unwrap_program_media_reference(normalized_reference)
        if not storage_reference.startswith("s3://"):
            raise ValueError("program media cleanup requires an S3 reference")
        normalized_reason = " ".join(str(reason or "cleanup").split())[:160] or "cleanup"
        created = (now or _utc_now()).replace(microsecond=0)
        available = created + timedelta(seconds=max(0, min(int(delay_seconds), 86_400)))
        created_iso = created.isoformat()
        available_iso = available.isoformat()
        cleanup_id = str(uuid4())
        self._conn.execute(
            """
            INSERT INTO program_media_cleanup_queue(
                id,business_id,media_reference,reason,status,attempts,
                available_at,locked_at,lock_token,last_error,created_at,
                updated_at,dead_at
            ) VALUES(?,?,?,?,'pending',0,?,NULL,NULL,NULL,?,?,NULL)
            ON CONFLICT(media_reference) DO NOTHING
            """,
            (
                cleanup_id,
                normalized_business_id,
                normalized_reference,
                normalized_reason,
                available_iso,
                created_iso,
                created_iso,
            ),
        )
        self._conn.execute(
            """
            UPDATE program_media_cleanup_queue
            SET status='retry', available_at=?, updated_at=?, dead_at=NULL,
                last_error=NULL, locked_at=NULL, lock_token=NULL,
                reason=?, business_id=?
            WHERE media_reference=? AND status='dead'
            """,
            (
                available_iso,
                created_iso,
                normalized_reason,
                normalized_business_id,
                normalized_reference,
            ),
        )
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM program_media_cleanup_queue "
            "WHERE media_reference=? LIMIT 1",  # nosec B608 - static columns
            (normalized_reference,),
        ).fetchone()
        if row is None:  # pragma: no cover - database invariant guard
            raise RuntimeError("program media cleanup enqueue failed")
        return _job(row)

    def discard(self, *, media_reference: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM program_media_cleanup_queue WHERE media_reference=?",
            (normalize_content_ref(media_reference),),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def is_referenced(self, *, media_reference: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM lessons WHERE content_ref=? LIMIT 1",
            (normalize_content_ref(media_reference),),
        ).fetchone()
        return row is not None

    def claim_due(
        self,
        *,
        limit: int = 10,
        lock_ttl_seconds: int = 900,
        now: datetime | None = None,
    ) -> list[ProgramMediaCleanupJob]:
        claim_now = (now or _utc_now()).replace(microsecond=0)
        now_iso = claim_now.isoformat()
        stale_before = (
            claim_now - timedelta(seconds=max(1, int(lock_ttl_seconds)))
        ).isoformat()
        batch_limit = max(1, min(int(limit), 100))
        lock_token = uuid.uuid4().hex

        if CONFIG.uses_postgres:
            rows = self._conn.execute(
                """
                WITH due AS (
                    SELECT id
                    FROM program_media_cleanup_queue
                    WHERE (
                        (status IN ('pending','retry') AND available_at<=?)
                        OR (
                            status='processing' AND locked_at IS NOT NULL
                            AND locked_at<=?
                        )
                    )
                    ORDER BY available_at,id
                    LIMIT ?
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE program_media_cleanup_queue q
                SET status='processing',locked_at=?,lock_token=?,updated_at=?
                FROM due
                WHERE q.id=due.id
                RETURNING q.id
                """,
                (
                    now_iso,
                    stale_before,
                    batch_limit,
                    now_iso,
                    lock_token,
                    now_iso,
                ),
            ).fetchall()
            if not rows:
                return []
        else:
            rows = self._conn.execute(
                """
                SELECT id FROM program_media_cleanup_queue
                WHERE (
                    (status IN ('pending','retry') AND available_at<=?)
                    OR (
                        status='processing' AND locked_at IS NOT NULL
                        AND locked_at<=?
                    )
                )
                ORDER BY available_at,id
                LIMIT ?
                """,
                (now_iso, stale_before, batch_limit),
            ).fetchall()
            ids = [str(_value(row, "id", 0)) for row in rows]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            self._conn.execute(
                "UPDATE program_media_cleanup_queue "
                "SET status='processing',locked_at=?,lock_token=?,updated_at=? "
                f"WHERE id IN ({placeholders}) AND "  # nosec B608 - placeholders only
                "((status IN ('pending','retry') AND available_at<=?) OR "
                "(status='processing' AND locked_at IS NOT NULL AND locked_at<=?))",
                [now_iso, lock_token, now_iso, *ids, now_iso, stale_before],
            )

        claimed = self._conn.execute(
            f"SELECT {_COLUMNS} FROM program_media_cleanup_queue "
            "WHERE status='processing' AND lock_token=? "
            "ORDER BY available_at,id",  # nosec B608 - static columns
            (lock_token,),
        ).fetchall()
        return [_job(row) for row in claimed]

    def complete(self, job: ProgramMediaCleanupJob) -> bool:
        cursor = self._conn.execute(
            """
            DELETE FROM program_media_cleanup_queue
            WHERE id=? AND status='processing' AND lock_token=?
            """,
            (job.id, job.lock_token),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) == 1

    def reschedule(
        self,
        job: ProgramMediaCleanupJob,
        *,
        error: str,
        max_attempts: int = 8,
        now: datetime | None = None,
    ) -> ProgramMediaCleanupJob:
        failed_at = (now or _utc_now()).replace(microsecond=0)
        attempts = job.attempts + 1
        terminal = attempts >= max(1, int(max_attempts))
        delay_seconds = min(30 * (2 ** max(0, attempts - 1)), 3600)
        available_at = (failed_at + timedelta(seconds=delay_seconds)).isoformat()
        status = "dead" if terminal else "retry"
        error_text = " ".join(str(error or "cleanup_failed").split())[:240]
        cursor = self._conn.execute(
            """
            UPDATE program_media_cleanup_queue
            SET status=?,attempts=?,available_at=?,updated_at=?,last_error=?,
                dead_at=?,locked_at=NULL,lock_token=NULL
            WHERE id=? AND status='processing' AND lock_token=?
            """,
            (
                status,
                attempts,
                available_at,
                failed_at.isoformat(),
                error_text,
                failed_at.isoformat() if terminal else None,
                job.id,
                job.lock_token,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise RuntimeError("program media cleanup lease was lost")
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM program_media_cleanup_queue "
            "WHERE id=? LIMIT 1",  # nosec B608 - static columns
            (job.id,),
        ).fetchone()
        if row is None:  # pragma: no cover - database invariant guard
            raise RuntimeError("program media cleanup reschedule failed")
        return _job(row)


__all__ = [
    "ProgramMediaCleanupJob",
    "ProgramMediaCleanupRepository",
]
