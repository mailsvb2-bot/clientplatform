from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from core.time_utils import normalize_utc_iso, utc_now_iso
from services.db import db, tx
from services.db.runtime import CONFIG
from services.job_keys import default_job_key

log = logging.getLogger(__name__)


def _sql_placeholders(count: int) -> str:
    """Return a validated SQL placeholder list for parameterized IN clauses.

    Only the placeholder fragment is generated dynamically; all values remain
    bound parameters passed separately to conn.execute().
    """
    count = int(count)
    if count <= 0:
        raise ValueError("placeholder count must be positive")
    return ",".join("?" for _ in range(count))


@dataclass
class ClaimedJob:
    id: int
    user_id: int
    job_type: str
    run_at_utc: str
    payload: str
    job_key: str
    retries: int
    lock_token: str


def _row_get(row: Any, key: str, index: int) -> Any:
    if hasattr(row, "keys"):
        try:
            return row[key]
        except KeyError:
            pass
        except TypeError:
            pass
        except IndexError:
            pass
    return row[index]


def _row_get_optional(row: Any, key: str, index: int) -> Any:
    try:
        return _row_get(row, key, index)
    except KeyError:
        return None
    except TypeError:
        return None
    except IndexError:
        return None


def _claimed_jobs_from_rows(rows: list[Any], *, fallback_token: str) -> list[ClaimedJob]:
    out: list[ClaimedJob] = []
    for row in rows:
        out.append(
            ClaimedJob(
                id=int(_row_get(row, "id", 0)),
                user_id=int(_row_get(row, "user_id", 1)),
                job_type=str(_row_get(row, "job_type", 2)),
                run_at_utc=str(_row_get(row, "run_at_utc", 3)),
                payload=str(_row_get(row, "payload", 4) or "{}"),
                job_key=str(_row_get(row, "job_key", 5) or ""),
                retries=int(_row_get(row, "retries", 6) or 0),
                lock_token=str(_row_get(row, "lock_token", 7) or fallback_token),
            )
        )
    return out


def _idempotency_created_at_epoch() -> int:
    return int(datetime.fromisoformat(utc_now_iso()).timestamp())


def _job_delivery_key_from_row(row: Any) -> tuple[int, str] | None:
    raw_user_id = _row_get_optional(row, "user_id", 0)
    raw_job_type = _row_get_optional(row, "job_type", 1)
    raw_job_key = _row_get_optional(row, "job_key", 2)

    try:
        user_id = int(raw_user_id)
    except TypeError:
        return None
    except ValueError:
        return None

    job_type = str(raw_job_type or "").strip()
    job_key = str(raw_job_key or "").strip()
    if not job_type or not job_key:
        return None
    return user_id, f"job:{job_type}:{job_key}"


def _mark_job_delivery_done(conn: Any, row: Any) -> None:
    marker = _job_delivery_key_from_row(row)
    if marker is None:
        return
    user_id, delivery_key = marker
    conn.execute(
        "INSERT OR IGNORE INTO idempotency(user_id, key, created_at) VALUES(?,?,?)",
        (user_id, delivery_key, _idempotency_created_at_epoch()),
    )


def _release_job_delivery_marker(conn: Any, job_id: int) -> None:
    """Clean up legacy pre-effect delivery markers after a lost lock.

    Older Engine.tick versions wrote the job idempotency marker before executing
    the effect. The current contract writes the marker from mark_done() only
    after successful completion, but this cleanup remains for existing rows and
    safe recovery from deployments that still have a stale pre-effect marker.
    """
    try:
        row = conn.execute(
            "SELECT user_id, job_type, job_key FROM jobs WHERE id=? LIMIT 1",
            (int(job_id),),
        ).fetchone()
    except sqlite3.Error:
        log.exception("SQLite failure while releasing idempotency marker job_id=%s", job_id)
        return
    except RuntimeError:
        log.exception("DB runtime failure while releasing idempotency marker job_id=%s", job_id)
        return
    if not row:
        return
    marker = _job_delivery_key_from_row(row)
    if marker is None:
        return
    user_id, delivery_key = marker
    try:
        conn.execute(
            "DELETE FROM idempotency WHERE user_id=? AND key=?",
            (user_id, delivery_key),
        )
    except sqlite3.Error:
        log.exception(
            "SQLite failure while deleting lost-lock marker job_id=%s delivery_key=%s",
            job_id,
            delivery_key,
        )
    except RuntimeError:
        log.exception(
            "DB runtime failure while deleting lost-lock marker job_id=%s delivery_key=%s",
            job_id,
            delivery_key,
        )


def _add_job_postgres(
    *,
    user_id: int,
    job_type: str,
    run_at_utc: str,
    encoded_payload: str,
    job_key: str,
) -> bool:
    """Idempotently enqueue a Postgres job with DB-enforced job_key uniqueness.

    The schema owns the invariant through the partial unique index
    idx_jobs_job_key on jobs(job_key) where job_key is not null. The advisory
    lock is retained only as a low-cost contention guard for concurrent workers;
    correctness no longer depends on a pre-insert existence check.
    """
    with db() as conn:
        with tx(conn):
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(?)::bigint)", (str(job_key),))
            cur = conn.execute(
                """
                INSERT INTO jobs(
                    user_id, job_type, run_at_utc, payload,
                    job_key, retries, locked_at, lock_token, done_at, last_error
                ) VALUES(?,?,?,?,?, 0, NULL, NULL, NULL, NULL)
                ON CONFLICT (job_key) WHERE job_key IS NOT NULL DO NOTHING
                """.strip(),
                (int(user_id), str(job_type), run_at_utc, encoded_payload, str(job_key)),
            )
            return int(getattr(cur, "rowcount", 0) or 0) == 1


def add_job(
    user_id: int,
    job_type: str,
    run_at_utc: str,
    payload: dict | None = None,
    *,
    job_key: str | None = None,
) -> bool:
    """Enqueue a job (UTC ISO timebase).

    - run_at_utc is always normalized to tz-aware UTC ISO.
    - job_key is used for idempotent enqueue; duplicates become no-op.
    """
    run_at_utc = normalize_utc_iso(run_at_utc)
    payload_obj = payload or {}
    if job_key is None:
        job_key = default_job_key(int(user_id), str(job_type), run_at_utc, payload_obj)
    encoded_payload = json.dumps(payload_obj, ensure_ascii=False)

    if CONFIG.uses_postgres:
        return _add_job_postgres(
            user_id=int(user_id),
            job_type=str(job_type),
            run_at_utc=run_at_utc,
            encoded_payload=encoded_payload,
            job_key=str(job_key),
        )

    with db() as conn:
        with tx(conn):
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO jobs(
                    user_id, job_type, run_at_utc, payload,
                    job_key, retries, locked_at, lock_token, done_at, last_error
                ) VALUES(?,?,?,?,?, 0, NULL, NULL, NULL, NULL)
                """.strip(),
                (int(user_id), str(job_type), run_at_utc, encoded_payload, str(job_key)),
            )
            return int(getattr(cur, "rowcount", 0) or 0) == 1


def cancel_jobs(user_id: int, job_types: list[str] | None = None, prefix: str | None = None) -> None:
    user_id = int(user_id)
    if not job_types and not prefix:
        return
    with db() as conn:
        with tx(conn):
            if job_types:
                placeholders = _sql_placeholders(len(job_types))
                conn.execute(
                    # placeholders are generated from a validated count;
                    # values are still passed as bound parameters.
                    f"DELETE FROM jobs WHERE user_id=? AND done_at IS NULL AND job_type IN ({placeholders})",  # nosec B608
                    [user_id, *job_types],
                )
            if prefix:
                conn.execute(
                    "DELETE FROM jobs WHERE user_id=? AND done_at IS NULL AND job_type LIKE ?",
                    (user_id, f"{prefix}%"),
                )


def cancel_funnel(user_id: int) -> None:
    cancel_jobs(int(user_id), prefix="funnel_")


def cancel_funnel2(user_id: int) -> None:
    cancel_jobs(int(user_id), prefix="funnel2_")


def cancel_post_prompt(user_id: int, session_id: int | str) -> None:
    user_id = int(user_id)
    sid = str(session_id).strip()
    if not sid:
        return
    pat1 = f'"session_id":"{sid}"'
    pat2 = f'"session_id": "{sid}"'
    with db() as conn:
        with tx(conn):
            conn.execute(
                "DELETE FROM jobs WHERE user_id=? AND done_at IS NULL AND job_type=? AND (payload LIKE ? OR payload LIKE ?)",
                (user_id, "post_prompt", f"%{pat1}%", f"%{pat2}%"),
            )


def _claim_due_jobs_postgres(*, now_utc_iso: str, stale_before: str, limit: int, token: str, job_type: str | None = None) -> list[ClaimedJob]:
    """Atomically claim due jobs using native Postgres row locks."""
    job_type_filter = ""
    params: list[Any] = [now_utc_iso, stale_before]
    if job_type is not None:
        # Do not bind a standalone ``? IS NULL`` parameter here. PostgreSQL
        # cannot infer the type of that parameter because it has no typed
        # expression context, even when the Python value itself is a string.
        job_type_filter = "AND job_type=?"
        params.append(str(job_type))
    params.extend((int(limit), now_utc_iso, token))

    query = f"""
        WITH due AS (
            SELECT id
            FROM jobs
            WHERE done_at IS NULL
              AND run_at_utc <= ?
              AND (locked_at IS NULL OR locked_at <= ?)
              {job_type_filter}
            ORDER BY id ASC
            LIMIT ?
            FOR UPDATE SKIP LOCKED
        )
        UPDATE jobs
        SET locked_at=?, lock_token=?
        FROM due
        WHERE jobs.id = due.id
          AND jobs.done_at IS NULL
        RETURNING jobs.id, jobs.user_id, jobs.job_type, jobs.run_at_utc,
                  jobs.payload, jobs.job_key, jobs.retries, jobs.lock_token
        """.strip()  # nosec B608 - filter is selected only from static SQL above
    with db() as conn:
        with tx(conn):
            rows = conn.execute(query, params).fetchall()
    return _claimed_jobs_from_rows(list(rows), fallback_token=token)


def _claim_due_jobs_sqlite(*, now_utc_iso: str, stale_before: str, limit: int, token: str, job_type: str | None = None) -> list[ClaimedJob]:
    with db() as conn:
        with tx(conn):
            rows = conn.execute(
                """
                SELECT id, user_id, job_type, run_at_utc, payload, job_key, retries
                FROM jobs
                WHERE done_at IS NULL
                  AND run_at_utc <= ?
                  AND (locked_at IS NULL OR locked_at <= ?)
                  AND (? IS NULL OR job_type=?)
                ORDER BY id ASC
                LIMIT ?
                """.strip(),
                (now_utc_iso, stale_before, job_type, job_type, int(limit)),
            ).fetchall()

            if not rows:
                return []

            ids = [int(_row_get(row, "id", 0)) for row in rows]
            placeholders = _sql_placeholders(len(ids))

            update_sql = (
                "UPDATE jobs "
                "SET locked_at=?, lock_token=? "
                f"WHERE id IN ({placeholders}) "  # nosec B608
                "AND done_at IS NULL "
                "AND (locked_at IS NULL OR locked_at <= ?)"
            )
            conn.execute(
                update_sql,
                [now_utc_iso, token, *ids, stale_before],
            )

            select_sql = (
                "SELECT id, user_id, job_type, run_at_utc, payload, job_key, retries, lock_token "
                "FROM jobs "
                "WHERE lock_token=? "
                f"AND id IN ({placeholders}) "  # nosec B608
                "ORDER BY id ASC"
            )
            claimed = conn.execute(
                select_sql,
                [token, *ids],
            ).fetchall()

    return _claimed_jobs_from_rows(list(claimed), fallback_token=token)


def claim_due_jobs(now_utc_iso: str, *, limit: int = 50, lock_ttl_sec: int = 120, job_type: str | None = None) -> list[ClaimedJob]:
    """Claim due jobs with a DB lock.

    In production/Postgres this is a native atomic UPDATE ... RETURNING claim with
    FOR UPDATE SKIP LOCKED. SQLite keeps the previous local/dev implementation.
    """
    now_utc_iso = normalize_utc_iso(now_utc_iso)
    now_dt = datetime.fromisoformat(now_utc_iso)
    stale_before = (now_dt - timedelta(seconds=int(lock_ttl_sec))).replace(microsecond=0).isoformat()
    token = uuid.uuid4().hex

    if CONFIG.uses_postgres:
        return _claim_due_jobs_postgres(now_utc_iso=now_utc_iso, stale_before=stale_before, limit=int(limit), token=token, job_type=job_type)
    return _claim_due_jobs_sqlite(now_utc_iso=now_utc_iso, stale_before=stale_before, limit=int(limit), token=token, job_type=job_type)


def lock_job(job_id: int, lock_token: str) -> bool:
    with db() as conn:
        with tx(conn):
            row = conn.execute(
                "SELECT 1 FROM jobs WHERE id=? AND lock_token=? AND done_at IS NULL",
                (int(job_id), str(lock_token)),
            ).fetchone()
            if row:
                return True
            _release_job_delivery_marker(conn, int(job_id))
            return False


def mark_done(job_id: int, lock_token: str, *, last_error: str | None = None) -> bool:
    with db() as conn:
        with tx(conn):
            row = conn.execute(
                "SELECT user_id, job_type, job_key FROM jobs WHERE id=? AND lock_token=? AND done_at IS NULL LIMIT 1",
                (int(job_id), str(lock_token)),
            ).fetchone()
            if not row:
                return False
            cur = conn.execute(
                """
                UPDATE jobs
                SET done_at=?, locked_at=NULL, lock_token=NULL, last_error=COALESCE(?, last_error)
                WHERE id=? AND lock_token=? AND done_at IS NULL
                """.strip(),
                (utc_now_iso(), last_error, int(job_id), str(lock_token)),
            )
            updated = int(getattr(cur, "rowcount", 0) or 0) == 1
            if updated and last_error is None:
                _mark_job_delivery_done(conn, row)
            return updated


def reschedule(job: ClaimedJob, retry_at_utc: str, *, last_error: str | None = None) -> bool:
    """Reschedule a claimed job.

    - increments retries
    - updates job_key to a new attempt suffix so retries are not blocked by idempotency
    """
    retry_at_utc = normalize_utc_iso(retry_at_utc)
    raw_key = str(job.job_key or "")
    base = re.sub(r":(?:a|attempt)\d+$", "", raw_key)
    new_retries = int(job.retries) + 1
    new_key = (
        f"{base}:attempt{new_retries}"
        if base
        else f"retry:{job.id}:attempt{new_retries}"
    )

    with db() as conn:
        with tx(conn):
            cur = conn.execute(
                """
                UPDATE jobs
                SET run_at_utc=?, retries=?, job_key=?, locked_at=NULL, lock_token=NULL, last_error=?
                WHERE id=? AND lock_token=? AND done_at IS NULL
                """.strip(),
                (retry_at_utc, int(new_retries), str(new_key), last_error, int(job.id), str(job.lock_token)),
            )
            return int(getattr(cur, "rowcount", 0) or 0) == 1
