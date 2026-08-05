from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import uuid4

from clientplatform.domain.ad_spend import AdSpendAuthorizationStatus, AdSpendInvariantViolation
from clientplatform.domain.ad_spend_operations import (
    AdSpendOperation,
    AdSpendOperationStatus,
    AdSpendOperationType,
    ad_spend_operation_key,
)
from clientplatform.domain.tenancy import PlatformRole, TenantContext, normalize_uuid
from clientplatform.infrastructure.ad_credential_vault import AdCredentialVault
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


def _now(value: datetime | str | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | str | None = None) -> str:
    return _now(value).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def _optional(row: Any, key: str, position: int) -> str | None:
    value = _value(row, key, position)
    return None if value is None else str(value)


def _safe_error(value: object) -> str:
    normalized = "_".join(str(value or "provider_error").strip().lower().split())
    filtered = "".join(ch for ch in normalized if ch.isalnum() or ch in "_.-")
    return (filtered or "provider_error")[:120]


def _safe_evidence(value: Mapping[str, object]) -> str:
    for key in value:
        lowered = str(key).lower()
        if any(marker in lowered for marker in ("token", "secret", "credential", "password", "authorization")):
            raise AdSpendInvariantViolation("provider evidence contains forbidden secret field")
    payload = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(payload.encode("utf-8")) > 16_384:
        raise AdSpendInvariantViolation("provider evidence exceeds bounded size")
    return payload


_SELECT = """
SELECT id, business_id, authorization_id, operation_type, status,
       idempotency_key, attempts, available_at, created_at, updated_at,
       locked_at, lock_token, last_error_code, completed_at, dead_at
FROM ad_spend_operations
"""


def _operation(row: Any) -> AdSpendOperation:
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
        locked_at=_optional(row, "locked_at", 10),
        lock_token=_optional(row, "lock_token", 11),
        last_error_code=_optional(row, "last_error_code", 12),
        completed_at=_optional(row, "completed_at", 13),
        dead_at=_optional(row, "dead_at", 14),
    )


@dataclass(frozen=True, slots=True)
class AdSpendOperationContext:
    operation: AdSpendOperation
    connection_id: str
    external_account_id: str
    external_login: str
    external_campaign_id: str
    external_ad_id: str
    receipt_hash: str
    authorization_expires_at: str
    hard_cap_minor: int
    daily_cap_minor: int
    currency: str


class AdSpendOperationRepository:
    def __init__(self, conn: Any, *, vault: AdCredentialVault | None = None) -> None:
        self._conn = conn
        self._vault = vault
        self._tenancy = TenancyRepository(conn)

    def _owner(self, actor: TenantContext) -> TenantContext:
        current = self._tenancy.resolve_context(user_id=actor.user_id, business_id=actor.business_id)
        if current.role != PlatformRole.OWNER:
            raise AdSpendInvariantViolation("advertising spend operation requires owner role")
        return current

    def enqueue_launch(self, *, actor: TenantContext, authorization_id: str, now: datetime | str | None = None) -> AdSpendOperation:
        current = self._owner(actor)
        return self._enqueue(
            business_id=current.business_id,
            actor_member_id=current.membership_id,
            authorization_id=authorization_id,
            operation_type=AdSpendOperationType.LAUNCH,
            expected_status=AdSpendAuthorizationStatus.AUTHORIZED,
            target_status=AdSpendAuthorizationStatus.LAUNCHING,
            now=now,
            reason="owner_launch_requested",
        )

    def enqueue_stop(self, *, actor: TenantContext, authorization_id: str, now: datetime | str | None = None) -> AdSpendOperation:
        current = self._owner(actor)
        return self._enqueue_stop(
            business_id=current.business_id,
            actor_member_id=current.membership_id,
            authorization_id=authorization_id,
            now=now,
            reason="owner_stop_requested",
        )

    def enqueue_stop_system(self, *, business_id: str, authorization_id: str, reason: str, now: datetime | str | None = None) -> AdSpendOperation:
        business = normalize_uuid(business_id, field_name="business_id")
        authorization = normalize_uuid(authorization_id, field_name="ad_spend_authorization_id")
        row = self._conn.execute(
            "SELECT created_by_member_id FROM ad_spend_authorizations WHERE id=? AND business_id=? LIMIT 1",
            (authorization, business),
        ).fetchone()
        if row is None:
            raise AdSpendInvariantViolation("ad spend authorization was not found")
        return self._enqueue_stop(
            business_id=business,
            actor_member_id=str(_value(row, "created_by_member_id", 0)),
            authorization_id=authorization,
            now=now,
            reason=_safe_error(reason),
        )

    def _enqueue_stop(self, *, business_id: str, actor_member_id: str, authorization_id: str, now: datetime | str | None, reason: str) -> AdSpendOperation:
        row = self._conn.execute(
            "SELECT status FROM ad_spend_authorizations WHERE id=? AND business_id=? LIMIT 1",
            (normalize_uuid(authorization_id, field_name="ad_spend_authorization_id"), business_id),
        ).fetchone()
        if row is None:
            raise AdSpendInvariantViolation("ad spend authorization was not found")
        status = AdSpendAuthorizationStatus(str(_value(row, "status", 0)))
        if status not in {AdSpendAuthorizationStatus.LAUNCHING, AdSpendAuthorizationStatus.ACTIVE, AdSpendAuthorizationStatus.STOPPING}:
            raise AdSpendInvariantViolation(f"stop cannot start from authorization state {status.value}")
        if status == AdSpendAuthorizationStatus.STOPPING:
            existing = self._find_by_key(
                business_id=business_id,
                key=ad_spend_operation_key(business_id=business_id, authorization_id=authorization_id, operation_type=AdSpendOperationType.STOP),
            )
            if existing is not None:
                return existing
        return self._enqueue(
            business_id=business_id,
            actor_member_id=actor_member_id,
            authorization_id=authorization_id,
            operation_type=AdSpendOperationType.STOP,
            expected_status=status,
            target_status=AdSpendAuthorizationStatus.STOPPING,
            now=now,
            reason=reason,
        )

    def _enqueue(self, *, business_id: str, actor_member_id: str, authorization_id: str, operation_type: AdSpendOperationType, expected_status: AdSpendAuthorizationStatus, target_status: AdSpendAuthorizationStatus, now: datetime | str | None, reason: str) -> AdSpendOperation:
        authorization = normalize_uuid(authorization_id, field_name="ad_spend_authorization_id")
        timestamp = _iso(now)
        key = ad_spend_operation_key(business_id=business_id, authorization_id=authorization, operation_type=operation_type)
        existing = self._find_by_key(business_id=business_id, key=key)
        if existing is not None:
            return existing
        row = self._conn.execute(
            """
            SELECT a.authorization_expires_at, a.consent_receipt_id, a.row_version,
                   j.external_ad_id, c.status AS connection_status
            FROM ad_spend_authorizations a
            JOIN ad_publication_jobs j ON j.id=a.publication_job_id AND j.business_id=a.business_id
            JOIN ad_connections c ON c.id=a.connection_id AND c.business_id=a.business_id
            WHERE a.id=? AND a.business_id=? AND a.status=? LIMIT 1
            """,
            (authorization, business_id, expected_status.value),
        ).fetchone()
        if row is None:
            raise AdSpendInvariantViolation(f"{operation_type.value} authorization state changed")
        if _optional(row, "consent_receipt_id", 1) is None:
            raise AdSpendInvariantViolation("launch or stop requires immutable consent receipt")
        if _optional(row, "external_ad_id", 3) is None:
            raise AdSpendInvariantViolation("provider DRAFT ad identity is missing")
        if str(_value(row, "connection_status", 4)) != "active":
            raise AdSpendInvariantViolation("advertising connection is not active")
        if operation_type == AdSpendOperationType.LAUNCH and _now(timestamp) >= _now(str(_value(row, "authorization_expires_at", 0))):
            raise AdSpendInvariantViolation("advertising spend authorization is expired")
        row_version = int(_value(row, "row_version", 2) or 0)
        self._conn.execute("SAVEPOINT ad_spend_operation_enqueue")
        try:
            operation_id = str(uuid4())
            self._conn.execute(
                """
                INSERT INTO ad_spend_operations(
                    id,business_id,authorization_id,operation_type,status,idempotency_key,
                    attempts,available_at,provider_evidence_json,created_at,updated_at
                ) VALUES(?,?,?,?, 'queued', ?,0,?,'{}',?,?)
                """,
                (operation_id, business_id, authorization, operation_type.value, key, timestamp, timestamp, timestamp),
            )
            cursor = self._conn.execute(
                """
                UPDATE ad_spend_authorizations SET status=?,updated_at=?,row_version=row_version+1
                WHERE id=? AND business_id=? AND status=? AND row_version=? AND consent_receipt_id IS NOT NULL
                """,
                (target_status.value, timestamp, authorization, business_id, expected_status.value, row_version),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise AdSpendInvariantViolation("authorization operation compare-and-set was lost")
            self._audit(business_id, actor_member_id, f"ad_spend_{operation_type.value}_queued", authorization, {"operation_id": operation_id, "reason": reason}, timestamp)
            self._conn.execute("RELEASE SAVEPOINT ad_spend_operation_enqueue")
            return self._get(operation_id, business_id)
        except Exception:
            self._conn.execute("ROLLBACK TO SAVEPOINT ad_spend_operation_enqueue")
            self._conn.execute("RELEASE SAVEPOINT ad_spend_operation_enqueue")
            concurrent = self._find_by_key(business_id=business_id, key=key)
            if concurrent is not None:
                return concurrent
            raise

    def recover_stale_leases(self, *, lock_ttl_seconds: int = 300, now: datetime | str | None = None) -> int:
        timestamp_dt = _now(now)
        cursor = self._conn.execute(
            """
            UPDATE ad_spend_operations SET status='retry',available_at=?,updated_at=?,locked_at=NULL,
                lock_token=NULL,last_error_code='stale_spend_operation_lease_recovered'
            WHERE status='processing' AND locked_at IS NOT NULL AND locked_at<?
            """,
            (_iso(timestamp_dt), _iso(timestamp_dt), _iso(timestamp_dt - timedelta(seconds=max(30, lock_ttl_seconds)))),
        )
        return int(getattr(cursor, "rowcount", 0) or 0)

    def claim_due(self, *, now: datetime | str | None = None) -> AdSpendOperation | None:
        timestamp = _iso(now)
        row = self._conn.execute(
            _SELECT + " WHERE status IN ('queued','retry') AND available_at<=? ORDER BY available_at,created_at,id LIMIT 1",
            (timestamp,),
        ).fetchone()
        if row is None:
            return None
        observed = _operation(row)
        lock_token = str(uuid4())
        cursor = self._conn.execute(
            """
            UPDATE ad_spend_operations SET status='processing',attempts=attempts+1,locked_at=?,lock_token=?,updated_at=?
            WHERE id=? AND business_id=? AND status IN ('queued','retry')
            """,
            (timestamp, lock_token, timestamp, observed.id, observed.business_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            return None
        return self._get(observed.id, observed.business_id)

    def load_claimed_context(self, *, operation: AdSpendOperation) -> tuple[AdSpendOperationContext, str]:
        if operation.status != AdSpendOperationStatus.PROCESSING or not operation.lock_token or self._vault is None:
            raise AdSpendInvariantViolation("leased spend operation and credential vault are required")
        row = self._conn.execute(
            """
            SELECT a.connection_id,c.external_account_id,c.external_login,a.external_campaign_id,
                   j.external_ad_id,r.receipt_hash,a.authorization_expires_at,a.hard_cap_minor,
                   a.daily_cap_minor,a.currency,c.credential_ciphertext,c.status AS connection_status
            FROM ad_spend_operations o
            JOIN ad_spend_authorizations a ON a.id=o.authorization_id AND a.business_id=o.business_id
            JOIN ad_spend_consent_receipts r ON r.authorization_id=a.id AND r.business_id=a.business_id
            JOIN ad_publication_jobs j ON j.id=a.publication_job_id AND j.business_id=a.business_id
            JOIN ad_connections c ON c.id=a.connection_id AND c.business_id=a.business_id
            WHERE o.id=? AND o.business_id=? AND o.status='processing' AND o.lock_token=? LIMIT 1
            """,
            (operation.id, operation.business_id, operation.lock_token),
        ).fetchone()
        if row is None or str(_value(row, "connection_status", 11)) != "active":
            raise AdSpendInvariantViolation("spend operation context is unavailable")
        context = AdSpendOperationContext(
            operation=operation,
            connection_id=str(_value(row, "connection_id", 0)),
            external_account_id=str(_value(row, "external_account_id", 1)),
            external_login=str(_value(row, "external_login", 2)),
            external_campaign_id=str(_value(row, "external_campaign_id", 3)),
            external_ad_id=str(_value(row, "external_ad_id", 4)),
            receipt_hash=str(_value(row, "receipt_hash", 5)),
            authorization_expires_at=str(_value(row, "authorization_expires_at", 6)),
            hard_cap_minor=int(_value(row, "hard_cap_minor", 7)),
            daily_cap_minor=int(_value(row, "daily_cap_minor", 8)),
            currency=str(_value(row, "currency", 9)),
        )
        ciphertext = str(_value(row, "credential_ciphertext", 10) or "")
        if not ciphertext or not context.receipt_hash.startswith("adconsent_"):
            raise AdSpendInvariantViolation("credential or immutable receipt is missing")
        return context, self._vault.open(ciphertext)

    def complete(self, *, operation: AdSpendOperation, provider_evidence: Mapping[str, object], now: datetime | str | None = None) -> AdSpendOperation:
        if operation.status != AdSpendOperationStatus.PROCESSING or not operation.lock_token:
            raise AdSpendInvariantViolation("spend operation is not leased")
        timestamp = _iso(now)
        target = "active" if operation.operation_type == AdSpendOperationType.LAUNCH else "stopped"
        expected = "launching" if operation.operation_type == AdSpendOperationType.LAUNCH else "stopping"
        self._conn.execute("SAVEPOINT ad_spend_operation_complete")
        try:
            cursor = self._conn.execute(
                """UPDATE ad_spend_operations SET status='succeeded',provider_evidence_json=?,completed_at=?,updated_at=?,locked_at=NULL,lock_token=NULL,last_error_code=NULL
                WHERE id=? AND business_id=? AND status='processing' AND lock_token=?""",
                (_safe_evidence(provider_evidence), timestamp, timestamp, operation.id, operation.business_id, operation.lock_token),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise AdSpendInvariantViolation("spend operation lease was lost")
            update = self._conn.execute(
                "UPDATE ad_spend_authorizations SET status=?,updated_at=?,stopped_at=?,last_error_code=NULL,row_version=row_version+1 WHERE id=? AND business_id=? AND status=?",
                (target, timestamp, timestamp if target == "stopped" else None, operation.authorization_id, operation.business_id, expected),
            )
            if int(getattr(update, "rowcount", 0) or 0) != 1:
                raise AdSpendInvariantViolation("authorization completion compare-and-set was lost")
            self._conn.execute("RELEASE SAVEPOINT ad_spend_operation_complete")
            return self._get(operation.id, operation.business_id)
        except Exception:
            self._conn.execute("ROLLBACK TO SAVEPOINT ad_spend_operation_complete")
            self._conn.execute("RELEASE SAVEPOINT ad_spend_operation_complete")
            raise

    def fail(self, *, operation: AdSpendOperation, error_code: str, retryable: bool, max_attempts: int = 8, now: datetime | str | None = None) -> AdSpendOperation:
        if operation.status != AdSpendOperationStatus.PROCESSING or not operation.lock_token:
            raise AdSpendInvariantViolation("spend operation is not leased")
        timestamp_dt = _now(now)
        terminal = not retryable or operation.attempts >= max(1, max_attempts)
        status = "failed" if terminal else "retry"
        available = _iso(timestamp_dt if terminal else timestamp_dt + timedelta(seconds=min(3600, 15 * 2 ** max(operation.attempts - 1, 0))))
        safe_error = _safe_error(error_code)
        cursor = self._conn.execute(
            "UPDATE ad_spend_operations SET status=?,available_at=?,updated_at=?,locked_at=NULL,lock_token=NULL,last_error_code=?,dead_at=? WHERE id=? AND business_id=? AND status='processing' AND lock_token=?",
            (status, available, _iso(timestamp_dt), safe_error, _iso(timestamp_dt) if terminal else None, operation.id, operation.business_id, operation.lock_token),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise AdSpendInvariantViolation("spend operation lease was lost")
        self._conn.execute("UPDATE ad_spend_authorizations SET last_error_code=?,updated_at=? WHERE id=? AND business_id=?", (safe_error, _iso(timestamp_dt), operation.authorization_id, operation.business_id))
        if terminal:
            self._conn.execute("UPDATE ad_spend_authorizations SET status='failed',row_version=row_version+1 WHERE id=? AND business_id=? AND status IN ('launching','stopping')", (operation.authorization_id, operation.business_id))
        return self._get(operation.id, operation.business_id)

    def _find_by_key(self, *, business_id: str, key: str) -> AdSpendOperation | None:
        row = self._conn.execute(_SELECT + " WHERE business_id=? AND idempotency_key=? LIMIT 1", (business_id, key)).fetchone()
        return None if row is None else _operation(row)

    def _get(self, operation_id: str, business_id: str) -> AdSpendOperation:
        row = self._conn.execute(_SELECT + " WHERE id=? AND business_id=? LIMIT 1", (operation_id, business_id)).fetchone()
        if row is None:
            raise AdSpendInvariantViolation("spend operation was not found")
        return _operation(row)

    def _audit(self, business_id: str, actor_member_id: str, action: str, authorization_id: str, details: Mapping[str, object], now: str) -> None:
        self._conn.execute(
            "INSERT INTO ad_audit_events(id,business_id,actor_member_id,action,subject_type,subject_id,details_json,created_at) VALUES(?,?,?,?, 'ad_spend_authorization', ?,?,?)",
            (str(uuid4()), business_id, actor_member_id, action, authorization_id, json.dumps(dict(details), ensure_ascii=False, sort_keys=True), now),
        )


__all__ = ["AdSpendOperationContext", "AdSpendOperationRepository"]
