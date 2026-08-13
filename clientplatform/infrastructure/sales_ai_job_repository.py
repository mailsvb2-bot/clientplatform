from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.sales_ai_jobs import (
    SalesAIJob,
    SalesAIJobLeaseLost,
    SalesAIJobStatus,
    normalize_sales_ai_source_order,
)
from clientplatform.domain.tenancy import normalize_uuid


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def _job_from_row(row: Any) -> SalesAIJob:
    return SalesAIJob(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        lead_id=str(_value(row, "lead_id", 2)),
        source_event_dedupe_key=str(_value(row, "source_event_dedupe_key", 3)),
        source_order_key=str(_value(row, "source_order_key", 4)),
        status=SalesAIJobStatus(str(_value(row, "status", 5))),
        attempts=int(_value(row, "attempts", 6)),
        available_at=str(_value(row, "available_at", 7)),
        locked_at=_value(row, "locked_at", 8),
        lock_token=_value(row, "lock_token", 9),
        last_error_code=_value(row, "last_error_code", 10),
        created_at=str(_value(row, "created_at", 11)),
        updated_at=str(_value(row, "updated_at", 12)),
        completed_at=_value(row, "completed_at", 13),
        dead_at=_value(row, "dead_at", 14),
    )


_SELECT = """
    SELECT id, business_id, lead_id, source_event_dedupe_key, source_order_key,
           status, attempts, available_at, locked_at, lock_token, last_error_code,
           created_at, updated_at, completed_at, dead_at
    FROM clientplatform_sales_ai_jobs
"""


class SalesAIJobRepository:
    """Durable bounded queue for advisory model work; safe under multiple workers."""

    def __init__(self, conn: Any):
        self._conn = conn

    def enqueue(
        self,
        *,
        business_id: str,
        lead_id: str,
        source_event_dedupe_key: str,
        source_order: int | str,
        debounce_seconds: int = 2,
        now: datetime | None = None,
    ) -> SalesAIJob:
        business = normalize_uuid(business_id, field_name="business_id")
        lead = normalize_uuid(lead_id, field_name="sales_lead_id")
        key = str(source_event_dedupe_key or "").strip()
        if not key or len(key) > 240 or any(ord(char) < 32 or ord(char) == 127 for char in key):
            raise ValueError("source_event_dedupe_key must be 1..240 printable characters")
        order_key = normalize_sales_ai_source_order(source_order)
        if (
            isinstance(debounce_seconds, bool)
            or not isinstance(debounce_seconds, int)
            or not 0 <= debounce_seconds <= 30
        ):
            raise ValueError("debounce_seconds must be an integer between 0 and 30")
        current = now or _utc_now()
        timestamp = _stamp(current)
        available_at = _stamp(current + timedelta(seconds=debounce_seconds))
        candidate_id = str(uuid4())
        self._conn.execute(
            """
            INSERT INTO clientplatform_sales_ai_heads(
                business_id, lead_id, latest_source_order_key, updated_at
            ) VALUES(?,?,?,?)
            ON CONFLICT(business_id, lead_id) DO UPDATE SET
                latest_source_order_key=CASE
                    WHEN excluded.latest_source_order_key > clientplatform_sales_ai_heads.latest_source_order_key
                    THEN excluded.latest_source_order_key
                    ELSE clientplatform_sales_ai_heads.latest_source_order_key
                END,
                updated_at=CASE
                    WHEN excluded.latest_source_order_key >= clientplatform_sales_ai_heads.latest_source_order_key
                    THEN excluded.updated_at
                    ELSE clientplatform_sales_ai_heads.updated_at
                END
            """,
            (business, lead, order_key, timestamp),
        )
        self._conn.execute(
            """
            UPDATE clientplatform_sales_ai_jobs
            SET status='done', completed_at=?, updated_at=?,
                locked_at=NULL, lock_token=NULL, last_error_code='superseded_by_newer_source'
            WHERE business_id=? AND lead_id=? AND source_order_key<?
              AND status IN ('pending','retry')
            """,
            (timestamp, timestamp, business, lead, order_key),
        )
        self._conn.execute(
            """
            INSERT INTO clientplatform_sales_ai_jobs(
                id, business_id, lead_id, source_event_dedupe_key, source_order_key,
                status, attempts, available_at, locked_at, lock_token,
                last_error_code, created_at, updated_at, completed_at, dead_at
            ) VALUES(?,?,?,?,?,'pending',0,?,NULL,NULL,NULL,?,?,NULL,NULL)
            ON CONFLICT(business_id, lead_id, source_event_dedupe_key) DO NOTHING
            """,
            (candidate_id, business, lead, key, order_key, available_at, timestamp, timestamp),
        )
        row = self._conn.execute(
            _SELECT + " WHERE business_id=? AND lead_id=? AND source_event_dedupe_key=? LIMIT 1",
            (business, lead, key),
        ).fetchone()
        if row is None:
            raise RuntimeError("sales AI job enqueue failed")
        return _job_from_row(row)

    def claim_due(
        self,
        *,
        limit: int,
        lock_ttl_seconds: int,
        now: datetime | None = None,
    ) -> list[SalesAIJob]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        if (
            isinstance(lock_ttl_seconds, bool)
            or not isinstance(lock_ttl_seconds, int)
            or not 30 <= lock_ttl_seconds <= 3600
        ):
            raise ValueError("lock_ttl_seconds must be an integer between 30 and 3600")
        current = now or _utc_now()
        timestamp = _stamp(current)
        stale = _stamp(current - timedelta(seconds=lock_ttl_seconds))
        rows = self._conn.execute(
            _SELECT
            + """
              WHERE (
                    (status IN ('pending','retry') AND available_at<=?)
                    OR (status='processing' AND locked_at IS NOT NULL AND locked_at<=?)
              )
              ORDER BY available_at, created_at, id
              LIMIT ?
            """,
            (timestamp, stale, limit),
        ).fetchall()
        claimed: list[SalesAIJob] = []
        for row in rows:
            candidate = _job_from_row(row)
            token = str(uuid4())
            cursor = self._conn.execute(
                """
                UPDATE clientplatform_sales_ai_jobs
                SET status='processing', attempts=attempts+1,
                    locked_at=?, lock_token=?, updated_at=?, last_error_code=NULL
                WHERE id=? AND business_id=? AND (
                    (status IN ('pending','retry') AND available_at<=?)
                    OR (status='processing' AND locked_at IS NOT NULL AND locked_at<=?)
                )
                """,
                (
                    timestamp,
                    token,
                    timestamp,
                    candidate.id,
                    candidate.business_id,
                    timestamp,
                    stale,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                continue
            latest = self.get(job_id=candidate.id, business_id=candidate.business_id)
            claimed.append(latest)
        return claimed

    def get(self, *, job_id: str, business_id: str) -> SalesAIJob:
        job = normalize_uuid(job_id, field_name="sales_ai_job_id")
        business = normalize_uuid(business_id, field_name="business_id")
        row = self._conn.execute(
            _SELECT + " WHERE id=? AND business_id=? LIMIT 1",
            (job, business),
        ).fetchone()
        if row is None:
            raise ValueError("sales AI job was not found")
        return _job_from_row(row)

    def lock_processing_lease(self, job: SalesAIJob) -> SalesAIJob:
        """Acquire a row lock for the exact processing lease.

        Used only at the external-AI egress boundary. Holding this transaction
        prevents another worker from reclaiming the same job while the provider
        request is in flight, even if wall-clock lease TTL is exceeded.
        """
        if job.status != SalesAIJobStatus.PROCESSING or not job.lock_token:
            raise SalesAIJobLeaseLost("sales AI job is not held by this worker")
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_sales_ai_jobs
            SET updated_at=updated_at
            WHERE id=? AND business_id=? AND status='processing' AND lock_token=?
            """,
            (job.id, job.business_id, job.lock_token),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise SalesAIJobLeaseLost("sales AI job lease changed before egress")
        return self.get(job_id=job.id, business_id=job.business_id)

    def has_newer_source(self, job: SalesAIJob) -> bool:
        latest = self.latest_source_order(
            business_id=job.business_id,
            lead_id=job.lead_id,
        )
        return latest is not None and latest > job.source_order_key

    def latest_source_order(self, *, business_id: str, lead_id: str) -> str | None:
        business = normalize_uuid(business_id, field_name="business_id")
        lead = normalize_uuid(lead_id, field_name="sales_lead_id")
        row = self._conn.execute(
            """
            SELECT latest_source_order_key
            FROM clientplatform_sales_ai_heads
            WHERE business_id=? AND lead_id=?
            LIMIT 1
            """,
            (business, lead),
        ).fetchone()
        return None if row is None else str(_value(row, "latest_source_order_key", 0))

    def lock_if_latest_source(self, job: SalesAIJob) -> bool:
        """Atomically establish that this job is still the per-lead AI head.

        The no-op UPDATE intentionally acquires the same row-level/write lock that
        enqueue() must update for a newer source, closing the check/write race.
        """

        cursor = self._conn.execute(
            """
            UPDATE clientplatform_sales_ai_heads
            SET latest_source_order_key=latest_source_order_key
            WHERE business_id=? AND lead_id=? AND latest_source_order_key=?
            """,
            (job.business_id, job.lead_id, job.source_order_key),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) == 1

    def load_customer_message(self, job: SalesAIJob) -> str:
        row = self._conn.execute(
            """
            SELECT payload_json
            FROM clientplatform_sales_events
            WHERE business_id=? AND lead_id=? AND event_type='customer_message'
              AND dedupe_key=?
            LIMIT 1
            """,
            (job.business_id, job.lead_id, job.source_event_dedupe_key),
        ).fetchone()
        if row is None:
            raise ValueError("sales AI source customer message was not found")
        try:
            payload = json.loads(str(_value(row, "payload_json", 0) or "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError("sales AI source customer message is invalid JSON") from exc
        text = " ".join(str((payload or {}).get("text") or "").replace("\x00", " ").split())
        if not text or len(text) > 12000:
            raise ValueError("sales AI source customer message must be 1..12000 characters")
        return text

    def mark_done(self, job: SalesAIJob, *, now: datetime | None = None) -> SalesAIJob:
        if job.status != SalesAIJobStatus.PROCESSING or not job.lock_token:
            raise SalesAIJobLeaseLost("sales AI job is not held by this worker")
        timestamp = _stamp(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_sales_ai_jobs
            SET status='done', completed_at=?, updated_at=?,
                locked_at=NULL, lock_token=NULL, last_error_code=NULL
            WHERE id=? AND business_id=? AND status='processing' AND lock_token=?
            """,
            (timestamp, timestamp, job.id, job.business_id, job.lock_token),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise SalesAIJobLeaseLost("sales AI job lease was lost before completion")
        return self.get(job_id=job.id, business_id=job.business_id)

    def cancel(self, job: SalesAIJob, *, reason: str, now: datetime | None = None) -> SalesAIJob:
        if job.status != SalesAIJobStatus.PROCESSING or not job.lock_token:
            raise SalesAIJobLeaseLost("sales AI job is not held by this worker")
        code = re.sub(r"[^a-z0-9_]+", "_", str(reason or "sales_ai_cancelled").lower()).strip("_")[:120]
        timestamp = _stamp(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_sales_ai_jobs
            SET status='done', completed_at=?, updated_at=?, last_error_code=?,
                locked_at=NULL, lock_token=NULL
            WHERE id=? AND business_id=? AND status='processing' AND lock_token=?
            """,
            (timestamp, timestamp, code or "sales_ai_cancelled", job.id, job.business_id, job.lock_token),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise SalesAIJobLeaseLost("sales AI job lease was lost before cancellation")
        return self.get(job_id=job.id, business_id=job.business_id)

    def purge_expired_raw_messages(
        self,
        *,
        raw_message_ttl_hours: int,
        now: datetime | None = None,
    ) -> int:
        if isinstance(raw_message_ttl_hours, bool) or not isinstance(raw_message_ttl_hours, int) or not 1 <= raw_message_ttl_hours <= 720:
            raise ValueError("raw_message_ttl_hours must be 1..720")
        cutoff = _stamp((now or _utc_now()) - timedelta(hours=raw_message_ttl_hours))
        redacted = json.dumps(
            {"redacted": True, "reason": "sales_ai_raw_message_ttl"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_sales_events
            SET payload_json=?
            WHERE event_type='customer_message' AND occurred_at<?
              AND payload_json NOT LIKE '%\"redacted\":true%'
            """,
            (redacted, cutoff),
        )
        return max(int(getattr(cursor, "rowcount", 0) or 0), 0)

    def retry_or_dead(
        self,
        job: SalesAIJob,
        *,
        error_code: str,
        max_attempts: int,
        now: datetime | None = None,
    ) -> SalesAIJob:
        if job.status != SalesAIJobStatus.PROCESSING or not job.lock_token:
            raise SalesAIJobLeaseLost("sales AI job is not held by this worker")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 20:
            raise ValueError("max_attempts must be an integer between 1 and 20")
        code = re.sub(r"[^a-z0-9_]+", "_", str(error_code or "sales_ai_failed").lower()).strip("_")[:120]
        code = code or "sales_ai_failed"
        current = now or _utc_now()
        timestamp = _stamp(current)
        if job.attempts >= max_attempts:
            status = "dead"
            available = job.available_at
            dead_at = timestamp
        else:
            status = "retry"
            delay = min(300, 2 ** min(max(job.attempts, 1), 8))
            available = _stamp(current + timedelta(seconds=delay))
            dead_at = None
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_sales_ai_jobs
            SET status=?, available_at=?, updated_at=?, last_error_code=?,
                locked_at=NULL, lock_token=NULL, dead_at=?
            WHERE id=? AND business_id=? AND status='processing' AND lock_token=?
            """,
            (
                status,
                available,
                timestamp,
                code,
                dead_at,
                job.id,
                job.business_id,
                job.lock_token,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise SalesAIJobLeaseLost("sales AI job lease was lost before retry transition")
        return self.get(job_id=job.id, business_id=job.business_id)


__all__ = ["SalesAIJobRepository"]
