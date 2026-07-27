from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from core.runtime_env import env_float, env_int
from services.db import db

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _decision_time(raw: Any, *, fallback: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return fallback
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def compute_and_store_rewards(
    window_sec: int = 3600,
    *,
    lookback_hours: int = 24,
    batch_size: int | None = None,
    max_runtime_sec: float | None = None,
) -> int:
    """Compute causal rewards in a bounded, resumable batch.

    The scheduler invokes this function in a worker thread. A hard
    ``asyncio.wait_for`` timeout cannot stop a running thread, so this function
    owns a cooperative runtime budget and returns before the scheduler timeout.
    Already-computed decisions are filtered in SQL and the oldest pending rows
    are drained first, preventing the previous unbounded N+1 scan.
    """

    resolved_batch_size = (
        int(batch_size)
        if batch_size is not None
        else env_int("REWARD_BATCH_SIZE", 25, minimum=1, maximum=1_000)
    )
    resolved_batch_size = max(1, min(int(resolved_batch_size), 1_000))
    resolved_runtime = (
        float(max_runtime_sec)
        if max_runtime_sec is not None
        else env_float("REWARD_MAX_RUNTIME_SEC", 3.5, minimum=0.25, maximum=4.0)
    )
    resolved_runtime = max(0.25, min(float(resolved_runtime), 4.0))

    now = _utc_now()
    since = (now - timedelta(hours=int(lookback_hours))).isoformat()
    deadline = time.monotonic() + resolved_runtime
    written = 0
    candidates = 0

    with db() as conn:
        try:
            rows = conn.execute(
                f"""
                SELECT
                    e.id,
                    e.user_id,
                    e.decision_id,
                    e.correlation_id,
                    COALESCE(e.timestamp_utc, e.ts, e.created_at) AS t
                FROM events AS e
                WHERE e.decision_id IS NOT NULL
                  AND e.decision_id != ''
                  AND e.name='decision_made'
                  AND COALESCE(e.timestamp_utc, e.ts, e.created_at) >= ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM decision_rewards AS dr
                      WHERE dr.decision_id=e.decision_id
                        AND dr.window_sec=?
                  )
                ORDER BY e.id ASC
                LIMIT {resolved_batch_size}
                """,
                (since, int(window_sec)),
            ).fetchall()
        except (sqlite3.Error, OSError, ValueError):
            logger.exception("RewardEngine candidate query failed")
            return 0

        candidates = len(rows)
        for row in rows:
            if time.monotonic() >= deadline:
                break

            try:
                _event_id = int(row[0])
                user_id = int(row[1])
                decision_id = str(row[2])
                correlation_id = str(row[3]) if row[3] is not None else None
                decision_at = _decision_time(row[4], fallback=now)
            except (IndexError, TypeError, ValueError):
                continue

            window_end = (decision_at + timedelta(seconds=int(window_sec))).isoformat()
            window_start = decision_at.isoformat()

            money = 0.0
            try:
                payment_row = conn.execute(
                    """
                    SELECT COALESCE(SUM(amount), 0)
                    FROM payments
                    WHERE user_id=? AND created_at >= ? AND created_at <= ?
                    """,
                    (user_id, window_start, window_end),
                ).fetchone()
                money = float(payment_row[0] or 0) if payment_row else 0.0
            except (sqlite3.Error, IndexError, TypeError, ValueError):
                money = 0.0

            state = 0.0
            try:
                mood_row = conn.execute(
                    """
                    SELECT COALESCE(SUM(CASE
                        WHEN pre_score IS NOT NULL AND post_score IS NOT NULL
                        THEN post_score - pre_score ELSE 0 END), 0)
                    FROM mood_sessions
                    WHERE user_id=? AND updated_at_utc >= ? AND updated_at_utc <= ?
                    """,
                    (user_id, window_start, window_end),
                ).fetchone()
                state += float(mood_row[0] or 0.0) if mood_row else 0.0
            except (sqlite3.Error, IndexError, TypeError, ValueError):
                state += 0.0

            try:
                rating_row = conn.execute(
                    """
                    SELECT COALESCE(AVG(rating), 0)
                    FROM state_ratings
                    WHERE user_id=? AND created_at_utc >= ? AND created_at_utc <= ?
                    """,
                    (user_id, window_start, window_end),
                ).fetchone()
                average_rating = float(rating_row[0] or 0.0) if rating_row else 0.0
                if average_rating:
                    state += max(-1.0, min(1.0, (average_rating - 5.0) / 5.0))
            except (sqlite3.Error, IndexError, TypeError, ValueError):
                state += 0.0

            retention = 0.0
            try:
                activity_row = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM events
                    WHERE user_id=?
                      AND COALESCE(timestamp_utc, ts, created_at) > ?
                      AND COALESCE(timestamp_utc, ts, created_at) <= ?
                    """,
                    (user_id, window_start, window_end),
                ).fetchone()
                retention += min(1.0, float(activity_row[0] or 0) / 3.0) if activity_row else 0.0
            except (sqlite3.Error, IndexError, TypeError, ValueError):
                retention += 0.0

            try:
                progress_row = conn.execute(
                    """
                    SELECT COALESCE(MAX(idx), 0)
                    FROM progress
                    WHERE user_id=? AND updated_at >= ? AND updated_at <= ?
                    """,
                    (user_id, window_start, window_end),
                ).fetchone()
                retention += min(1.0, float(progress_row[0] or 0) / 10.0) if progress_row else 0.0
            except (sqlite3.Error, IndexError, TypeError, ValueError):
                retention += 0.0

            reward = money + state + retention
            metadata = json.dumps(
                {"money": money, "state": state, "retention": retention},
                ensure_ascii=False,
            )
            conn.execute(
                """
                INSERT INTO decision_rewards(
                    decision_id, user_id, correlation_id,
                    reward_value, money_value, state_value, retention_value,
                    window_sec, computed_at_utc, meta
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    decision_id,
                    user_id,
                    correlation_id,
                    reward,
                    money,
                    state,
                    retention,
                    int(window_sec),
                    _utc_now_iso(),
                    metadata,
                ),
            )
            written += 1

        conn.commit()

    if candidates:
        logger.info(
            "RewardEngine batch complete: wrote=%s candidates=%s batch=%s runtime_budget=%.2fs window=%ss lookback=%sh",
            written,
            candidates,
            resolved_batch_size,
            resolved_runtime,
            window_sec,
            lookback_hours,
        )
    return written
