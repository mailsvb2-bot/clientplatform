from __future__ import annotations

"""Time-bounded, audited platform-support access to one exact business.

This module is a capability boundary, not a tenant role. It never creates a
``business_members`` row and never constructs a synthetic ``TenantContext``.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from clientplatform.domain.tenancy import Business, normalize_uuid
from clientplatform.infrastructure import TenancyRepository
from services.admin import is_platform_admin
from services.db import get_db

_DEFAULT_TTL_SECONDS = 1800
_MIN_TTL_SECONDS = 300
_MAX_TTL_SECONDS = 7200


class PlatformSupportPermissionDenied(PermissionError):
    """The caller is not an explicitly configured platform operator."""


class PlatformSupportSessionUnavailable(PermissionError):
    """The requested support capability cannot authorize this operation."""


class PlatformSupportSessionConflict(ValueError):
    """An idempotency key was reused for a different support request."""


@dataclass(frozen=True, slots=True)
class PlatformSupportSession:
    id: str
    operator_user_id: int
    business_id: str
    ticket_ref: str
    reason: str
    idempotency_key: str
    request_fingerprint: str
    status: str
    issued_at: str
    expires_at: str
    revoked_at: str | None
    revoked_by_user_id: int | None

    def effective_status(self, *, now_utc: datetime | None = None) -> str:
        if self.revoked_at is not None or self.status == "revoked":
            return "revoked"
        if _clock(now_utc) >= _parse_timestamp(self.expires_at):
            return "expired"
        return "active"


@dataclass(frozen=True, slots=True)
class PlatformSupportBusinessSnapshot:
    session_id: str
    business_id: str
    business_name: str
    business_status: str
    business_created_at: str
    business_updated_at: str
    session_expires_at: str


def _clock(value: datetime | None) -> datetime:
    current = value or datetime.now(tz=UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    return current.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("platform support timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("platform support timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _operator(user_id: int | None) -> int:
    if user_id is None or not is_platform_admin(user_id):
        raise PlatformSupportPermissionDenied("platform support access required")
    return int(user_id)


def _text(value: object, *, field: str, maximum: int) -> str:
    normalized = " ".join(str(value or "").replace("\x00", " ").split())
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters")
    return normalized


def _session_id(value: object) -> str:
    try:
        return str(UUID(str(value or "").strip()))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("session_id must be a valid UUID") from exc


def _ttl(value: int) -> int:
    if isinstance(value, bool):
        raise ValueError("ttl_seconds must be an integer")
    try:
        ttl = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("ttl_seconds must be an integer") from exc
    if not _MIN_TTL_SECONDS <= ttl <= _MAX_TTL_SECONDS:
        raise ValueError(
            f"ttl_seconds must be between {_MIN_TTL_SECONDS} and {_MAX_TTL_SECONDS}"
        )
    return ttl


def _fingerprint(*, business_id: str, ticket_ref: str, reason: str, ttl_seconds: int) -> str:
    payload = json.dumps(
        {
            "business_id": business_id,
            "reason": reason,
            "ticket_ref": ticket_ref,
            "ttl_seconds": ttl_seconds,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def _session_from_row(row: Any) -> PlatformSupportSession:
    revoked_at = _value(row, "revoked_at", 10)
    revoked_by = _value(row, "revoked_by_user_id", 11)
    return PlatformSupportSession(
        id=str(_value(row, "id", 0)),
        operator_user_id=int(_value(row, "operator_user_id", 1)),
        business_id=str(_value(row, "business_id", 2)),
        ticket_ref=str(_value(row, "ticket_ref", 3)),
        reason=str(_value(row, "reason", 4)),
        idempotency_key=str(_value(row, "idempotency_key", 5)),
        request_fingerprint=str(_value(row, "request_fingerprint", 6)),
        status=str(_value(row, "status", 7)),
        issued_at=str(_value(row, "issued_at", 8)),
        expires_at=str(_value(row, "expires_at", 9)),
        revoked_at=None if revoked_at is None else str(revoked_at),
        revoked_by_user_id=None if revoked_by is None else int(revoked_by),
    )


_SESSION_SELECT = """
    SELECT id, operator_user_id, business_id, ticket_ref, reason,
           idempotency_key, request_fingerprint, status, issued_at, expires_at,
           revoked_at, revoked_by_user_id
    FROM clientplatform_platform_support_sessions
"""


def _load_owned_session(conn: Any, *, operator_user_id: int, session_id: str) -> PlatformSupportSession:
    row = conn.execute(
        _SESSION_SELECT + " WHERE id=? AND operator_user_id=? LIMIT 1",
        (session_id, operator_user_id),
    ).fetchone()
    if row is None:
        raise PlatformSupportSessionUnavailable("support session is unavailable")
    return _session_from_row(row)


def _lock_scoped_session(
    conn: Any,
    *,
    operator_user_id: int,
    session_id: str,
    business_id: str,
) -> PlatformSupportSession:
    """Serialize support access with revoke on the exact capability row.

    A no-op UPDATE is portable across the canonical SQLite/PostgreSQL DB layer:
    SQLite serializes the writer transaction, while PostgreSQL takes a row lock.
    Revoke and allowed support reads therefore cannot pass each other between
    authorization and the audited read.
    """

    cursor = conn.execute(
        """
        UPDATE clientplatform_platform_support_sessions
        SET status=status
        WHERE id=? AND operator_user_id=? AND business_id=?
        """,
        (session_id, operator_user_id, business_id),
    )
    if int(getattr(cursor, "rowcount", 0) or 0) != 1:
        raise PlatformSupportSessionUnavailable("support session is unavailable")
    return _load_owned_session(
        conn,
        operator_user_id=operator_user_id,
        session_id=session_id,
    )


def _assert_active(session: PlatformSupportSession, *, now_utc: datetime) -> None:
    if session.effective_status(now_utc=now_utc) != "active":
        raise PlatformSupportSessionUnavailable("support session is not active")


def _audit(
    conn: Any,
    *,
    session: PlatformSupportSession,
    event_type: str,
    subject_type: str,
    subject_id: str | None,
    detail: dict[str, object],
    created_at: str,
    deterministic_suffix: str | None = None,
) -> None:
    event_id = (
        str(uuid5(NAMESPACE_URL, f"clientplatform:platform-support:{session.id}:{deterministic_suffix}"))
        if deterministic_suffix is not None
        else str(uuid4())
    )
    conn.execute(
        """
        INSERT INTO clientplatform_platform_support_audit_events(
            id, session_id, operator_user_id, business_id, event_type,
            subject_type, subject_id, detail, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (
            event_id,
            session.id,
            session.operator_user_id,
            session.business_id,
            event_type,
            subject_type,
            subject_id,
            json.dumps(detail, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
            created_at,
        ),
    )


def issue_support_session(
    user_id: int | None,
    *,
    business_id: str,
    ticket_ref: str,
    reason: str,
    idempotency_key: str,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    now_utc: datetime | None = None,
) -> PlatformSupportSession:
    operator_user_id = _operator(user_id)
    normalized_business_id = normalize_uuid(business_id, field_name="business_id")
    normalized_ticket = _text(ticket_ref, field="ticket_ref", maximum=160)
    normalized_reason = _text(reason, field="reason", maximum=500)
    normalized_key = _text(idempotency_key, field="idempotency_key", maximum=200)
    ttl = _ttl(ttl_seconds)
    current = _clock(now_utc)
    issued_at = _stamp(current)
    expires_at = _stamp(current + timedelta(seconds=ttl))
    request_fingerprint = _fingerprint(
        business_id=normalized_business_id,
        ticket_ref=normalized_ticket,
        reason=normalized_reason,
        ttl_seconds=ttl,
    )
    session_id = str(
        uuid5(
            NAMESPACE_URL,
            f"clientplatform:platform-support-session:{operator_user_id}:{normalized_key}",
        )
    )

    with get_db() as conn:
        # The canonical tenancy repository owns the exact business lookup. No
        # membership or synthetic TenantContext is created for the operator.
        TenancyRepository(conn).get_business_for_platform_support(
            business_id=normalized_business_id
        )
        conn.execute(
            """
            INSERT INTO clientplatform_platform_support_sessions(
                id, operator_user_id, business_id, ticket_ref, reason,
                idempotency_key, request_fingerprint, status, issued_at, expires_at,
                revoked_at, revoked_by_user_id
            ) VALUES(?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL, NULL)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                session_id,
                operator_user_id,
                normalized_business_id,
                normalized_ticket,
                normalized_reason,
                normalized_key,
                request_fingerprint,
                issued_at,
                expires_at,
            ),
        )
        session = _load_owned_session(
            conn,
            operator_user_id=operator_user_id,
            session_id=session_id,
        )
        if (
            session.business_id != normalized_business_id
            or session.ticket_ref != normalized_ticket
            or session.reason != normalized_reason
            or session.idempotency_key != normalized_key
            or session.request_fingerprint != request_fingerprint
        ):
            raise PlatformSupportSessionConflict(
                "support session idempotency key belongs to different work"
            )
        _audit(
            conn,
            session=session,
            event_type="issued",
            subject_type="support_session",
            subject_id=session.id,
            detail={
                "expires_at": session.expires_at,
                "reason": session.reason,
                "request_fingerprint": session.request_fingerprint,
                "ticket_ref": session.ticket_ref,
            },
            created_at=session.issued_at,
            deterministic_suffix="issued",
        )
        return session


def read_support_session(
    user_id: int | None,
    *,
    session_id: str,
    business_id: str,
    now_utc: datetime | None = None,
) -> PlatformSupportSession:
    operator_user_id = _operator(user_id)
    normalized_session_id = _session_id(session_id)
    normalized_business_id = normalize_uuid(business_id, field_name="business_id")
    current = _clock(now_utc)
    with get_db() as conn:
        session = _lock_scoped_session(
            conn,
            operator_user_id=operator_user_id,
            session_id=normalized_session_id,
            business_id=normalized_business_id,
        )
        _audit(
            conn,
            session=session,
            event_type="session_read",
            subject_type="support_session",
            subject_id=session.id,
            detail={"effective_status": session.effective_status(now_utc=current)},
            created_at=_stamp(current),
        )
        return session


def read_support_business(
    user_id: int | None,
    *,
    session_id: str,
    business_id: str,
    now_utc: datetime | None = None,
) -> PlatformSupportBusinessSnapshot:
    operator_user_id = _operator(user_id)
    normalized_session_id = _session_id(session_id)
    normalized_business_id = normalize_uuid(business_id, field_name="business_id")
    current = _clock(now_utc)
    with get_db() as conn:
        session = _lock_scoped_session(
            conn,
            operator_user_id=operator_user_id,
            session_id=normalized_session_id,
            business_id=normalized_business_id,
        )
        _assert_active(session, now_utc=current)
        business: Business = TenancyRepository(conn).get_business_for_platform_support(
            business_id=session.business_id
        )
        _audit(
            conn,
            session=session,
            event_type="business_metadata_read",
            subject_type="business",
            subject_id=business.id,
            detail={"business_status": business.status.value},
            created_at=_stamp(current),
        )
        return PlatformSupportBusinessSnapshot(
            session_id=session.id,
            business_id=business.id,
            business_name=business.name,
            business_status=business.status.value,
            business_created_at=business.created_at,
            business_updated_at=business.updated_at,
            session_expires_at=session.expires_at,
        )


def revoke_support_session(
    user_id: int | None,
    *,
    session_id: str,
    business_id: str,
    now_utc: datetime | None = None,
) -> PlatformSupportSession:
    operator_user_id = _operator(user_id)
    normalized_session_id = _session_id(session_id)
    normalized_business_id = normalize_uuid(business_id, field_name="business_id")
    current = _clock(now_utc)
    revoked_at = _stamp(current)
    with get_db() as conn:
        session = _lock_scoped_session(
            conn,
            operator_user_id=operator_user_id,
            session_id=normalized_session_id,
            business_id=normalized_business_id,
        )
        if session.revoked_at is None:
            conn.execute(
                """
                UPDATE clientplatform_platform_support_sessions
                SET status='revoked', revoked_at=?, revoked_by_user_id=?
                WHERE id=? AND operator_user_id=? AND business_id=? AND status='active'
                """,
                (
                    revoked_at,
                    operator_user_id,
                    session.id,
                    operator_user_id,
                    session.business_id,
                ),
            )
        revoked = _load_owned_session(
            conn,
            operator_user_id=operator_user_id,
            session_id=normalized_session_id,
        )
        if revoked.revoked_at is None or revoked.status != "revoked":
            raise RuntimeError("support session revocation was not persisted")
        _audit(
            conn,
            session=revoked,
            event_type="revoked",
            subject_type="support_session",
            subject_id=revoked.id,
            detail={"revoked_by_user_id": operator_user_id},
            created_at=revoked.revoked_at,
            deterministic_suffix="revoked",
        )
        return revoked


__all__ = [
    "PlatformSupportBusinessSnapshot",
    "PlatformSupportPermissionDenied",
    "PlatformSupportSession",
    "PlatformSupportSessionConflict",
    "PlatformSupportSessionUnavailable",
    "issue_support_session",
    "read_support_business",
    "read_support_session",
    "revoke_support_session",
]
