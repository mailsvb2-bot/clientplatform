from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

from clientplatform.domain.ad_spend import AdSpendInvariantViolation
from clientplatform.domain.ad_spend_operations import (
    AdSpendOperation,
    AdSpendOperationStatus,
    AdSpendOperationType,
)


def _timestamp(value: datetime | str) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def _safe_evidence(value: Mapping[str, object]) -> str:
    for key in value:
        lowered = str(key).lower()
        if any(
            marker in lowered
            for marker in (
                "token",
                "secret",
                "credential",
                "password",
                "authorization",
            )
        ):
            raise AdSpendInvariantViolation(
                "provider evidence contains forbidden secret field"
            )
    payload = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(payload.encode("utf-8")) > 16_384:
        raise AdSpendInvariantViolation("provider evidence exceeds bounded size")
    return payload


def launch_is_superseded_by_stop(
    conn: Any,
    *,
    operation: AdSpendOperation,
) -> bool:
    if operation.operation_type != AdSpendOperationType.LAUNCH:
        return False
    row = conn.execute(
        """
        SELECT status
        FROM ad_spend_authorizations
        WHERE id=? AND business_id=?
        LIMIT 1
        """,
        (operation.authorization_id, operation.business_id),
    ).fetchone()
    return row is not None and str(_value(row, "status", 0)) == "stopping"


def complete_superseded_launch(
    conn: Any,
    *,
    operation: AdSpendOperation,
    provider_evidence: Mapping[str, object],
    now: datetime | str,
) -> AdSpendOperation:
    """Finish a leased launch without overwriting a newer stop transition."""

    if operation.operation_type != AdSpendOperationType.LAUNCH:
        raise AdSpendInvariantViolation("only launch can be superseded by stop")
    if operation.status != AdSpendOperationStatus.PROCESSING or not operation.lock_token:
        raise AdSpendInvariantViolation("superseded launch is not leased")
    if not launch_is_superseded_by_stop(conn, operation=operation):
        raise AdSpendInvariantViolation("launch was not superseded by stop")

    timestamp = _timestamp(now)
    evidence = {
        **dict(provider_evidence),
        "superseded_by_stop": True,
    }
    cursor = conn.execute(
        """
        UPDATE ad_spend_operations
        SET status='succeeded', provider_evidence_json=?, completed_at=?,
            updated_at=?, locked_at=NULL, lock_token=NULL,
            last_error_code=NULL
        WHERE id=? AND business_id=? AND status='processing' AND lock_token=?
        """,
        (
            _safe_evidence(evidence),
            timestamp,
            timestamp,
            operation.id,
            operation.business_id,
            operation.lock_token,
        ),
    )
    if int(getattr(cursor, "rowcount", 0) or 0) != 1:
        raise AdSpendInvariantViolation("superseded launch lease was lost")

    row = conn.execute(
        """
        SELECT id, business_id, authorization_id, operation_type, status,
               idempotency_key, attempts, available_at, created_at, updated_at,
               locked_at, lock_token, last_error_code, completed_at, dead_at
        FROM ad_spend_operations
        WHERE id=? AND business_id=?
        LIMIT 1
        """,
        (operation.id, operation.business_id),
    ).fetchone()
    if row is None:
        raise AdSpendInvariantViolation("superseded launch was not found")
    return AdSpendOperation(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        authorization_id=str(_value(row, "authorization_id", 2)),
        operation_type=str(_value(row, "operation_type", 3)),
        status=str(_value(row, "status", 4)),
        idempotency_key=str(_value(row, "idempotency_key", 5)),
        attempts=int(_value(row, "attempts", 6) or 0),
        available_at=str(_value(row, "available_at", 7)),
        created_at=str(_value(row, "created_at", 8)),
        updated_at=str(_value(row, "updated_at", 9)),
        locked_at=None if _value(row, "locked_at", 10) is None else str(_value(row, "locked_at", 10)),
        lock_token=None if _value(row, "lock_token", 11) is None else str(_value(row, "lock_token", 11)),
        last_error_code=(
            None
            if _value(row, "last_error_code", 12) is None
            else str(_value(row, "last_error_code", 12))
        ),
        completed_at=(
            None
            if _value(row, "completed_at", 13) is None
            else str(_value(row, "completed_at", 13))
        ),
        dead_at=None if _value(row, "dead_at", 14) is None else str(_value(row, "dead_at", 14)),
    )


__all__ = [
    "complete_superseded_launch",
    "launch_is_superseded_by_stop",
]
