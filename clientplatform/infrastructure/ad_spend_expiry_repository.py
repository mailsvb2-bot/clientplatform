from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from services.db import get_db


_EXPIRABLE = ("draft", "awaiting_consent", "authorized")


@dataclass(frozen=True, slots=True)
class AdSpendExpirySweepResult:
    scanned: int = 0
    expired: int = 0
    lost_races: int = 0


def _timestamp(value: datetime | str | None = None) -> str:
    if value is None:
        parsed = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def expire_due_ad_spend_authorizations(
    *,
    limit: int = 100,
    now: datetime | str | None = None,
) -> AdSpendExpirySweepResult:
    timestamp = _timestamp(now)
    bounded_limit = max(1, min(int(limit), 500))
    scanned = 0
    expired = 0
    lost_races = 0

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, business_id, created_by_member_id, status, row_version
            FROM ad_spend_authorizations
            WHERE status IN ('draft', 'awaiting_consent', 'authorized')
              AND authorization_expires_at<=?
            ORDER BY authorization_expires_at, id
            LIMIT ?
            """,
            (timestamp, bounded_limit),
        ).fetchall()
        for row in rows:
            scanned += 1
            authorization_id = str(_value(row, "id", 0))
            business_id = str(_value(row, "business_id", 1))
            actor_member_id = str(_value(row, "created_by_member_id", 2))
            previous_status = str(_value(row, "status", 3))
            row_version = int(_value(row, "row_version", 4) or 0)
            cursor = conn.execute(
                """
                UPDATE ad_spend_authorizations
                SET status='expired', last_error_code='authorization_expired',
                    updated_at=?, row_version=row_version+1
                WHERE id=? AND business_id=? AND status=? AND row_version=?
                  AND authorization_expires_at<=?
                """,
                (
                    timestamp,
                    authorization_id,
                    business_id,
                    previous_status,
                    row_version,
                    timestamp,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                lost_races += 1
                continue
            expired += 1
            conn.execute(
                """
                INSERT INTO ad_audit_events(
                    id, business_id, actor_member_id, action,
                    subject_type, subject_id, details_json, created_at
                ) VALUES(?, ?, ?, 'ad_spend_authorization_expired',
                         'ad_spend_authorization', ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    business_id,
                    actor_member_id,
                    authorization_id,
                    json.dumps(
                        {"previous_status": previous_status},
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    timestamp,
                ),
            )

    return AdSpendExpirySweepResult(
        scanned=scanned,
        expired=expired,
        lost_races=lost_races,
    )


__all__ = [
    "AdSpendExpirySweepResult",
    "expire_due_ad_spend_authorizations",
]
