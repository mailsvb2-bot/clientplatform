from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from clientplatform.domain.support_cases import (
    SupportCase,
    SupportCaseCategory,
    SupportCaseStatus,
    normalize_support_case_id,
    normalize_support_category,
    normalize_support_summary,
)
from clientplatform.domain.tenancy import TenantContext, normalize_user_id
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


class SupportCaseConflict(RuntimeError):
    """A support-case idempotency or ownership invariant was violated."""


class SupportCaseUnavailable(RuntimeError):
    """The requested support case is unavailable for the requested operation."""


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def _case_from_row(row: Any) -> SupportCase:
    claimed_by = _value(row, "claimed_by_operator_user_id", 6)
    claimed_at = _value(row, "claimed_at", 10)
    resolved_at = _value(row, "resolved_at", 11)
    return SupportCase(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        category=SupportCaseCategory(str(_value(row, "category", 2))),
        summary=str(_value(row, "summary", 3)),
        status=SupportCaseStatus(str(_value(row, "status", 4))),
        created_by_member_id=str(_value(row, "created_by_member_id", 5)),
        claimed_by_operator_user_id=None if claimed_by is None else int(claimed_by),
        created_at=str(_value(row, "created_at", 7)),
        updated_at=str(_value(row, "updated_at", 8)),
        claimed_at=None if claimed_at is None else str(claimed_at),
        resolved_at=None if resolved_at is None else str(resolved_at),
    )


_CASE_SELECT = """
    SELECT id, business_id, category, summary, status,
           created_by_member_id, claimed_by_operator_user_id,
           created_at, updated_at, idempotency_key, claimed_at, resolved_at,
           request_fingerprint
    FROM clientplatform_support_cases
"""


def _request_fingerprint(*, category: SupportCaseCategory, summary: str) -> str:
    material = json.dumps(
        {"category": category.value, "summary": summary},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _idempotency_key(value: object) -> str:
    normalized = " ".join(str(value or "").replace("\x00", " ").split()).strip()
    if not 1 <= len(normalized) <= 200:
        raise ValueError("idempotency_key must be 1..200 characters")
    return normalized


def _audit(
    conn: Any,
    *,
    case: SupportCase,
    event_type: str,
    actor_kind: str,
    actor_ref: str,
    created_at: str,
    operation_key: str,
) -> None:
    event_id = str(
        uuid5(
            NAMESPACE_URL,
            f"clientplatform:support-case-audit:{case.id}:{event_type}:{actor_ref}:{operation_key}",
        )
    )
    detail = json.dumps(
        {"category": case.category.value, "status": case.status.value},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    conn.execute(
        """
        INSERT INTO clientplatform_support_case_audit_events(
            id, case_id, business_id, event_type, actor_kind,
            actor_ref, detail, created_at
        ) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO NOTHING
        """,
        (
            event_id,
            case.id,
            case.business_id,
            event_type,
            actor_kind,
            actor_ref,
            detail,
            created_at,
        ),
    )


class SupportCaseRepository:
    """Canonical data owner for tenant-created platform support cases."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def _current(self, actor: TenantContext) -> TenantContext:
        return self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )

    def _get_exact(self, *, case_id: str) -> SupportCase:
        normalized = normalize_support_case_id(case_id)
        row = self._conn.execute(
            _CASE_SELECT + " WHERE id=? LIMIT 1",
            (normalized,),
        ).fetchone()
        if row is None:
            raise SupportCaseUnavailable("support case is unavailable")
        return _case_from_row(row)

    def _get_tenant(self, *, actor: TenantContext, case_id: str) -> SupportCase:
        current = self._current(actor)
        normalized = normalize_support_case_id(case_id)
        row = self._conn.execute(
            _CASE_SELECT + " WHERE id=? AND business_id=? LIMIT 1",
            (normalized, current.business_id),
        ).fetchone()
        if row is None:
            raise SupportCaseUnavailable("support case is unavailable")
        return _case_from_row(row)

    def create(
        self,
        *,
        actor: TenantContext,
        category: SupportCaseCategory | str,
        summary: object,
        idempotency_key: object,
        now: str | None = None,
    ) -> SupportCase:
        current = self._current(actor)
        normalized_category = normalize_support_category(category)
        normalized_summary = normalize_support_summary(summary)
        normalized_key = _idempotency_key(idempotency_key)
        fingerprint = _request_fingerprint(
            category=normalized_category,
            summary=normalized_summary,
        )
        case_id = str(
            uuid5(
                NAMESPACE_URL,
                "clientplatform:support-case:"
                f"{current.business_id}:{current.membership_id}:{normalized_key}",
            )
        )
        timestamp = str(now or _utc_now())
        self._conn.execute(
            """
            INSERT INTO clientplatform_support_cases(
                id, business_id, category, summary, status,
                created_by_member_id, claimed_by_operator_user_id,
                idempotency_key, request_fingerprint,
                created_at, updated_at, claimed_at, resolved_at
            ) VALUES(?,?,?,?, 'open', ?,NULL,?,?, ?,?,NULL,NULL)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                case_id,
                current.business_id,
                normalized_category.value,
                normalized_summary,
                current.membership_id,
                normalized_key,
                fingerprint,
                timestamp,
                timestamp,
            ),
        )
        case = self._get_tenant(actor=current, case_id=case_id)
        row = self._conn.execute(
            """
            SELECT idempotency_key, request_fingerprint
            FROM clientplatform_support_cases WHERE id=? AND business_id=? LIMIT 1
            """,
            (case.id, case.business_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("support case idempotency evidence disappeared")
        if (
            str(_value(row, "idempotency_key", 0)) != normalized_key
            or str(_value(row, "request_fingerprint", 1)) != fingerprint
        ):
            raise SupportCaseConflict("support case idempotency key conflicts with existing work")
        _audit(
            self._conn,
            case=case,
            event_type="created",
            actor_kind="tenant_member",
            actor_ref=current.membership_id,
            created_at=case.created_at,
            operation_key=normalized_key,
        )
        return case

    def list_for_tenant(self, *, actor: TenantContext, limit: int = 20) -> list[SupportCase]:
        current = self._current(actor)
        bounded = max(1, min(int(limit), 50))
        rows = self._conn.execute(
            _CASE_SELECT
            + " WHERE business_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
            (current.business_id, bounded),
        ).fetchall()
        return [_case_from_row(row) for row in rows]

    def list_platform_queue(self, *, limit: int = 50) -> list[SupportCase]:
        bounded = max(1, min(int(limit), 100))
        rows = self._conn.execute(
            _CASE_SELECT
            + " WHERE status IN ('open','claimed') ORDER BY created_at, id LIMIT ?",
            (bounded,),
        ).fetchall()
        return [_case_from_row(row) for row in rows]

    def require_claimed_for_platform_session(
        self,
        *,
        operator_user_id: int,
        case_id: str,
    ) -> SupportCase:
        """Lock and authorize the exact case before issuing an M6-002 capability."""

        operator_id = normalize_user_id(operator_user_id)
        case = self._lock_case(case_id=case_id)
        if case.status != SupportCaseStatus.CLAIMED:
            raise SupportCaseUnavailable(
                "support case must be claimed before tenant inspection"
            )
        if case.claimed_by_operator_user_id != operator_id:
            raise SupportCaseConflict("support case is owned by another operator")
        return case

    def _lock_case(self, *, case_id: str) -> SupportCase:
        normalized = normalize_support_case_id(case_id)
        cursor = self._conn.execute(
            "UPDATE clientplatform_support_cases SET status=status WHERE id=?",
            (normalized,),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise SupportCaseUnavailable("support case is unavailable")
        return self._get_exact(case_id=normalized)

    def _operation_replayed(
        self,
        *,
        case: SupportCase,
        event_type: str,
        operator_user_id: int,
        operation_key: str,
    ) -> bool:
        event_id = str(
            uuid5(
                NAMESPACE_URL,
                "clientplatform:support-case-audit:"
                f"{case.id}:{event_type}:{operator_user_id}:{operation_key}",
            )
        )
        row = self._conn.execute(
            "SELECT 1 FROM clientplatform_support_case_audit_events WHERE id=? LIMIT 1",
            (event_id,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _assert_replay_state(
        *,
        case: SupportCase,
        event_type: str,
        operator_user_id: int,
    ) -> None:
        if event_type == "claimed":
            valid = (
                case.status == SupportCaseStatus.CLAIMED
                and case.claimed_by_operator_user_id == operator_user_id
            )
        elif event_type == "released":
            valid = (
                case.status == SupportCaseStatus.OPEN
                and case.claimed_by_operator_user_id is None
            )
        elif event_type == "resolved":
            valid = (
                case.status == SupportCaseStatus.RESOLVED
                and case.claimed_by_operator_user_id == operator_user_id
            )
        else:
            raise ValueError("unsupported support case replay event")
        if not valid:
            raise SupportCaseUnavailable("support case operation replay is stale")

    def claim_platform(
        self,
        *,
        operator_user_id: int,
        case_id: str,
        idempotency_key: object,
        now: str | None = None,
    ) -> SupportCase:
        operator_id = normalize_user_id(operator_user_id)
        key = _idempotency_key(idempotency_key)
        case = self._lock_case(case_id=case_id)
        if self._operation_replayed(
            case=case,
            event_type="claimed",
            operator_user_id=operator_id,
            operation_key=key,
        ):
            self._assert_replay_state(
                case=case, event_type="claimed", operator_user_id=operator_id
            )
            return case
        if case.status == SupportCaseStatus.RESOLVED:
            raise SupportCaseUnavailable("resolved support case cannot be claimed")
        if case.status == SupportCaseStatus.CLAIMED:
            raise SupportCaseConflict("support case is already claimed")
        timestamp = str(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_support_cases
            SET status='claimed', claimed_by_operator_user_id=?,
                claimed_at=?, updated_at=?
            WHERE id=? AND status='open'
            """,
            (operator_id, timestamp, timestamp, case.id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise SupportCaseConflict("support case was claimed concurrently")
        claimed = self._get_exact(case_id=case.id)
        _audit(
            self._conn,
            case=claimed,
            event_type="claimed",
            actor_kind="platform_operator",
            actor_ref=str(operator_id),
            created_at=timestamp,
            operation_key=key,
        )
        return claimed

    def release_platform(
        self,
        *,
        operator_user_id: int,
        case_id: str,
        idempotency_key: object,
        now: str | None = None,
    ) -> SupportCase:
        operator_id = normalize_user_id(operator_user_id)
        key = _idempotency_key(idempotency_key)
        case = self._lock_case(case_id=case_id)
        if self._operation_replayed(
            case=case,
            event_type="released",
            operator_user_id=operator_id,
            operation_key=key,
        ):
            self._assert_replay_state(
                case=case, event_type="released", operator_user_id=operator_id
            )
            return case
        if case.status != SupportCaseStatus.CLAIMED:
            raise SupportCaseUnavailable("only a claimed support case can be released")
        if case.claimed_by_operator_user_id != operator_id:
            raise SupportCaseConflict("support case is owned by another operator")
        timestamp = str(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_support_cases
            SET status='open', claimed_by_operator_user_id=NULL,
                claimed_at=NULL, updated_at=?
            WHERE id=? AND status='claimed' AND claimed_by_operator_user_id=?
            """,
            (timestamp, case.id, operator_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise SupportCaseConflict("support case release lost its ownership race")
        released = self._get_exact(case_id=case.id)
        _audit(
            self._conn,
            case=released,
            event_type="released",
            actor_kind="platform_operator",
            actor_ref=str(operator_id),
            created_at=timestamp,
            operation_key=key,
        )
        return released

    def resolve_platform(
        self,
        *,
        operator_user_id: int,
        case_id: str,
        idempotency_key: object,
        now: str | None = None,
    ) -> SupportCase:
        operator_id = normalize_user_id(operator_user_id)
        key = _idempotency_key(idempotency_key)
        case = self._lock_case(case_id=case_id)
        if self._operation_replayed(
            case=case,
            event_type="resolved",
            operator_user_id=operator_id,
            operation_key=key,
        ):
            self._assert_replay_state(
                case=case, event_type="resolved", operator_user_id=operator_id
            )
            return case
        if case.status == SupportCaseStatus.RESOLVED:
            raise SupportCaseUnavailable("support case is already resolved")
        if case.status != SupportCaseStatus.CLAIMED:
            raise SupportCaseUnavailable("support case must be claimed before resolve")
        if case.claimed_by_operator_user_id != operator_id:
            raise SupportCaseConflict("support case is owned by another operator")
        timestamp = str(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_support_cases
            SET status='resolved', resolved_at=?, updated_at=?
            WHERE id=? AND status='claimed' AND claimed_by_operator_user_id=?
            """,
            (timestamp, timestamp, case.id, operator_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise SupportCaseConflict("support case resolve lost its ownership race")
        resolved = self._get_exact(case_id=case.id)
        _audit(
            self._conn,
            case=resolved,
            event_type="resolved",
            actor_kind="platform_operator",
            actor_ref=str(operator_id),
            created_at=timestamp,
            operation_key=key,
        )
        return resolved


__all__ = [
    "SupportCaseConflict",
    "SupportCaseRepository",
    "SupportCaseUnavailable",
]
