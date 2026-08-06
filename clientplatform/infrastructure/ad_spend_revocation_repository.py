from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.ad_spend import AdSpendInvariantViolation
from clientplatform.domain.ad_spend_operations import (
    AdSpendOperationType,
    ad_spend_operation_key,
)
from clientplatform.domain.tenancy import normalize_uuid


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


def queue_stop_for_revoked_live_authorization(
    conn: Any,
    *,
    business_id: str,
    authorization_id: str,
    actor_member_id: str,
    now: datetime | str,
) -> str:
    """Atomically convert a live revocation into a durable stop operation.

    The caller must invoke this in the same transaction that persisted
    ``status='revoked'``. Any failure therefore rolls back both revocation and
    stop enqueue instead of leaving provider spend live without a durable stop.
    """

    business = normalize_uuid(business_id, field_name="business_id")
    authorization = normalize_uuid(
        authorization_id,
        field_name="ad_spend_authorization_id",
    )
    actor = normalize_uuid(actor_member_id, field_name="actor_member_id")
    timestamp = _timestamp(now)
    key = ad_spend_operation_key(
        business_id=business,
        authorization_id=authorization,
        operation_type=AdSpendOperationType.STOP,
    )

    row = conn.execute(
        """
        SELECT a.status, a.row_version, a.consent_receipt_id,
               j.external_ad_id, c.status AS connection_status
        FROM ad_spend_authorizations AS a
        JOIN ad_publication_jobs AS j
          ON j.id=a.publication_job_id AND j.business_id=a.business_id
        JOIN ad_connections AS c
          ON c.id=j.connection_id AND c.business_id=a.business_id
        WHERE a.id=? AND a.business_id=?
        LIMIT 1
        """,
        (authorization, business),
    ).fetchone()
    if row is None:
        raise AdSpendInvariantViolation("revoked spend authorization was not found")
    if str(_value(row, "status", 0)) != "revoked":
        raise AdSpendInvariantViolation(
            "provider stop enqueue requires revoked authorization"
        )
    if not _value(row, "consent_receipt_id", 2):
        raise AdSpendInvariantViolation(
            "revoked live authorization lost immutable consent receipt"
        )
    if not _value(row, "external_ad_id", 3):
        raise AdSpendInvariantViolation("provider advertisement identity is missing")
    if str(_value(row, "connection_status", 4)) != "active":
        raise AdSpendInvariantViolation("advertising connection is not active")

    row_version = int(_value(row, "row_version", 1) or 0)
    operation_id = str(uuid4())
    cursor = conn.execute(
        """
        INSERT INTO ad_spend_operations(
            id, business_id, authorization_id, operation_type, status,
            idempotency_key, attempts, available_at, provider_evidence_json,
            created_at, updated_at
        ) VALUES(?, ?, ?, 'stop', 'queued', ?, 0, ?, '{}', ?, ?)
        ON CONFLICT(business_id, idempotency_key) DO NOTHING
        """,
        (
            operation_id,
            business,
            authorization,
            key,
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    if int(getattr(cursor, "rowcount", 0) or 0) != 1:
        existing = conn.execute(
            """
            SELECT id
            FROM ad_spend_operations
            WHERE business_id=? AND idempotency_key=?
            LIMIT 1
            """,
            (business, key),
        ).fetchone()
        if existing is None:
            raise AdSpendInvariantViolation(
                "revocation stop idempotency conflict was not reconciled"
            )
        operation_id = str(_value(existing, "id", 0))

    update = conn.execute(
        """
        UPDATE ad_spend_authorizations
        SET status='stopping', updated_at=?, row_version=row_version+1
        WHERE id=? AND business_id=? AND status='revoked' AND row_version=?
          AND consent_receipt_id IS NOT NULL
        """,
        (timestamp, authorization, business, row_version),
    )
    if int(getattr(update, "rowcount", 0) or 0) != 1:
        concurrent = conn.execute(
            """
            SELECT status
            FROM ad_spend_authorizations
            WHERE id=? AND business_id=?
            LIMIT 1
            """,
            (authorization, business),
        ).fetchone()
        if concurrent is None or str(_value(concurrent, "status", 0)) != "stopping":
            raise AdSpendInvariantViolation(
                "revocation stop compare-and-set was lost"
            )

    conn.execute(
        """
        INSERT INTO ad_audit_events(
            id, business_id, actor_member_id, action,
            subject_type, subject_id, details_json, created_at
        ) VALUES(?, ?, ?, 'ad_spend_stop_queued',
                 'ad_spend_authorization', ?, ?, ?)
        """,
        (
            str(uuid4()),
            business,
            actor,
            authorization,
            json.dumps(
                {
                    "operation_id": operation_id,
                    "reason": "owner_consent_revoked",
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            timestamp,
        ),
    )
    return operation_id


__all__ = ["queue_stop_for_revoked_live_authorization"]
