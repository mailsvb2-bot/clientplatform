from __future__ import annotations

from datetime import datetime, timezone

from core.time_utils import normalize_utc_iso


def _iso_to_epoch(dt_iso: str) -> int:
    dt = datetime.fromisoformat(normalize_utc_iso(dt_iso))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp())


def default_job_key(user_id: int, job_type: str, run_at_utc: str, payload: dict) -> str:
    """Single source of truth for job idempotency keys.

    The DB enforces at-most-once enqueue via UNIQUE(job_key) on jobs.
    """
    ref = payload.get("session_id") or payload.get("ref_id") or payload.get("ref") or str(user_id)
    run_at_epoch = _iso_to_epoch(run_at_utc)
    return f"j:{str(job_type)}:{str(ref)}:{int(run_at_epoch)}:a0"
