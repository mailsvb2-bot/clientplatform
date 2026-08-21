from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.safe_tenancy_repository import TenancyRepository


_SETUP_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,160}")
_SETUP_PLATFORMS = frozenset({ConnectionPlatform.VK, ConnectionPlatform.MAX})


class NativeMessengerSetupRejected(RuntimeError):
    """A secure native-messenger setup capability is invalid or unavailable."""


@dataclass(frozen=True, slots=True)
class IssuedNativeMessengerSetup:
    session_id: str
    token: str
    business_id: str
    platform: ConnectionPlatform
    expires_at: str


@dataclass(frozen=True, slots=True)
class NativeMessengerSetupGrant:
    business_id: str
    business_name: str
    platform: ConnectionPlatform
    actor: TenantContext


@dataclass(frozen=True, slots=True)
class NativeMessengerSetupReference:
    session_id: str
    business_id: str
    platform: ConnectionPlatform
    token_digest: str
    expires_at: str
    actor: TenantContext


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _token(value: str) -> str:
    normalized = str(value or "").strip()
    if _SETUP_TOKEN_RE.fullmatch(normalized) is None:
        raise NativeMessengerSetupRejected("messenger setup capability is invalid")
    return normalized


def _digest(value: str) -> str:
    return hashlib.sha256(_token(value).encode("utf-8")).hexdigest()


def _session_id(value: str) -> str:
    try:
        return str(UUID(str(value or "").strip()))
    except (ValueError, AttributeError) as exc:
        raise NativeMessengerSetupRejected(
            "messenger setup session id is invalid"
        ) from exc


def _platform(value: ConnectionPlatform | str) -> ConnectionPlatform:
    platform = (
        value
        if isinstance(value, ConnectionPlatform)
        else ConnectionPlatform(str(value).strip().lower())
    )
    if platform not in _SETUP_PLATFORMS:
        raise ValueError("messenger setup supports only VK or MAX")
    return platform


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


class NativeMessengerSetupRepository:
    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def _current_actor(self, actor: TenantContext) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_business()
        return current

    def _lock_issue_boundary(self, business_id: str) -> None:
        """Serialize setup replacement for one business across DB dialects."""

        cursor = self._conn.execute(
            """
            UPDATE businesses
            SET updated_at=updated_at
            WHERE id=? AND status='active'
            """,
            (business_id,),
        )
        if int(getattr(cursor, "rowcount", 1) or 0) != 1:
            raise NativeMessengerSetupRejected("active business is unavailable")

    def issue(
        self,
        *,
        actor: TenantContext,
        platform: ConnectionPlatform | str,
        ttl_seconds: int = 600,
        now: datetime | None = None,
        session_id: str | None = None,
        token: str | None = None,
    ) -> IssuedNativeMessengerSetup:
        """Issue a single-use setup capability while persisting only its digest."""

        current = self._current_actor(actor)
        selected_platform = _platform(platform)
        lifetime = max(60, min(int(ttl_seconds), 1800))
        created = (now or _utc_now()).astimezone(timezone.utc).replace(microsecond=0)
        created_iso = _iso(created)
        expires_at = _iso(created + timedelta(seconds=lifetime))
        selected_session_id = _session_id(session_id or str(uuid4()))
        raw_token = _token(token) if token is not None else secrets.token_urlsafe(32)
        digest = _digest(raw_token)
        self._lock_issue_boundary(current.business_id)
        self._conn.execute(
            """
            UPDATE messenger_connection_setup_sessions
            SET consumed_at=?
            WHERE business_id=? AND platform=? AND consumed_at IS NULL
            """,
            (created_iso, current.business_id, selected_platform.value),
        )
        self._conn.execute(
            """
            INSERT INTO messenger_connection_setup_sessions(
                id,business_id,platform,token_digest,created_by_member_id,
                created_at,expires_at,consumed_at
            ) VALUES(?,?,?,?,?,?,?,NULL)
            """,
            (
                selected_session_id,
                current.business_id,
                selected_platform.value,
                digest,
                current.membership_id,
                created_iso,
                expires_at,
            ),
        )
        return IssuedNativeMessengerSetup(
            session_id=selected_session_id,
            token=raw_token,
            business_id=current.business_id,
            platform=selected_platform,
            expires_at=expires_at,
        )

    def ensure_recoverable_reference(
        self,
        *,
        actor: TenantContext,
        platform: ConnectionPlatform | str,
        session_id: str,
        token: str,
        ttl_seconds: int = 600,
        now: datetime | None = None,
    ) -> NativeMessengerSetupReference:
        """Create one replay-stable digest-only session or reuse the exact prior one.

        The deterministic session id is non-secret. An exact replay never
        invalidates the previously materialized outbox command. A new session id
        still invalidates an older setup capability for the same business/channel.
        """

        current = self._current_actor(actor)
        selected_platform = _platform(platform)
        selected_session_id = _session_id(session_id)
        lifetime = max(60, min(int(ttl_seconds), 1800))
        created = (now or _utc_now()).astimezone(timezone.utc).replace(microsecond=0)
        created_iso = _iso(created)
        expires_at = _iso(created + timedelta(seconds=lifetime))
        digest = _digest(token)

        self._lock_issue_boundary(current.business_id)
        cursor = self._conn.execute(
            """
            INSERT INTO messenger_connection_setup_sessions(
                id,business_id,platform,token_digest,created_by_member_id,
                created_at,expires_at,consumed_at
            ) VALUES(?,?,?,?,?,?,?,NULL)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                selected_session_id,
                current.business_id,
                selected_platform.value,
                digest,
                current.membership_id,
                created_iso,
                expires_at,
            ),
        )
        inserted = int(getattr(cursor, "rowcount", 0) or 0) == 1
        row = self._conn.execute(
            """
            SELECT id,business_id,platform,token_digest,expires_at,
                   created_by_member_id
            FROM messenger_connection_setup_sessions
            WHERE id=?
            LIMIT 1
            """,
            (selected_session_id,),
        ).fetchone()
        if row is None:
            raise NativeMessengerSetupRejected(
                "recoverable messenger setup session was not persisted"
            )
        if (
            str(_value(row, "business_id", 1)) != current.business_id
            or _platform(str(_value(row, "platform", 2))) != selected_platform
            or str(_value(row, "created_by_member_id", 5)) != current.membership_id
        ):
            raise NativeMessengerSetupRejected(
                "messenger setup idempotency key belongs to different work"
            )
        if inserted:
            self._conn.execute(
                """
                UPDATE messenger_connection_setup_sessions
                SET consumed_at=?
                WHERE business_id=? AND platform=? AND id<>?
                  AND consumed_at IS NULL
                """,
                (
                    created_iso,
                    current.business_id,
                    selected_platform.value,
                    selected_session_id,
                ),
            )
        return NativeMessengerSetupReference(
            session_id=str(_value(row, "id", 0)),
            business_id=current.business_id,
            platform=selected_platform,
            token_digest=str(_value(row, "token_digest", 3)),
            expires_at=str(_value(row, "expires_at", 4)),
            actor=current,
        )

    def inspect_reference(
        self,
        *,
        session_id: str,
        business_id: str,
        now: datetime | None = None,
    ) -> NativeMessengerSetupReference:
        """Resolve a non-secret session reference with live tenant authorization."""

        selected_session_id = _session_id(session_id)
        timestamp = _iso(now or _utc_now())
        row = self._conn.execute(
            """
            SELECT s.id,s.business_id,s.platform,s.token_digest,s.expires_at,
                   m.user_id
            FROM messenger_connection_setup_sessions s
            JOIN businesses b ON b.id=s.business_id AND b.status='active'
            JOIN business_members m
              ON m.id=s.created_by_member_id AND m.business_id=s.business_id
             AND m.status='active'
            WHERE s.id=? AND s.business_id=? AND s.consumed_at IS NULL
              AND s.expires_at>?
            LIMIT 1
            """,
            (selected_session_id, str(business_id or "").strip(), timestamp),
        ).fetchone()
        if row is None:
            raise NativeMessengerSetupRejected(
                "messenger setup session is expired, consumed or unavailable"
            )
        resolved_business_id = str(_value(row, "business_id", 1))
        user_id = int(_value(row, "user_id", 5))
        actor = self._tenancy.resolve_context(
            user_id=user_id,
            business_id=resolved_business_id,
        )
        actor.assert_can_manage_business()
        return NativeMessengerSetupReference(
            session_id=str(_value(row, "id", 0)),
            business_id=resolved_business_id,
            platform=_platform(str(_value(row, "platform", 2))),
            token_digest=str(_value(row, "token_digest", 3)),
            expires_at=str(_value(row, "expires_at", 4)),
            actor=actor,
        )

    def inspect(
        self,
        *,
        token: str,
        now: datetime | None = None,
    ) -> NativeMessengerSetupGrant:
        return self._load(token=token, now=now, consume=False)

    def consume(
        self,
        *,
        token: str,
        now: datetime | None = None,
    ) -> NativeMessengerSetupGrant:
        return self._load(token=token, now=now, consume=True)

    def _load(
        self,
        *,
        token: str,
        now: datetime | None,
        consume: bool,
    ) -> NativeMessengerSetupGrant:
        digest = _digest(token)
        timestamp = _iso(now or _utc_now())
        row = self._conn.execute(
            """
            SELECT s.id,s.business_id,s.platform,s.created_by_member_id,
                   s.expires_at,b.name,m.user_id
            FROM messenger_connection_setup_sessions s
            JOIN businesses b ON b.id=s.business_id AND b.status='active'
            JOIN business_members m
              ON m.id=s.created_by_member_id AND m.business_id=s.business_id
             AND m.status='active'
            WHERE s.token_digest=? AND s.consumed_at IS NULL AND s.expires_at>?
            LIMIT 1
            """,
            (digest, timestamp),
        ).fetchone()
        if row is None:
            raise NativeMessengerSetupRejected(
                "messenger setup capability is expired, consumed or invalid"
            )
        business_id = str(_value(row, "business_id", 1))
        user_id = int(_value(row, "user_id", 6))
        actor = self._tenancy.resolve_context(
            user_id=user_id,
            business_id=business_id,
        )
        actor.assert_can_manage_business()
        if consume:
            cursor = self._conn.execute(
                """
                UPDATE messenger_connection_setup_sessions
                SET consumed_at=?
                WHERE id=? AND business_id=? AND token_digest=?
                  AND consumed_at IS NULL AND expires_at>?
                """,
                (
                    timestamp,
                    str(_value(row, "id", 0)),
                    business_id,
                    digest,
                    timestamp,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise NativeMessengerSetupRejected(
                    "messenger setup capability was already consumed"
                )
        return NativeMessengerSetupGrant(
            business_id=business_id,
            business_name=str(_value(row, "name", 5)),
            platform=_platform(str(_value(row, "platform", 2))),
            actor=actor,
        )


__all__ = [
    "IssuedNativeMessengerSetup",
    "NativeMessengerSetupGrant",
    "NativeMessengerSetupReference",
    "NativeMessengerSetupRejected",
    "NativeMessengerSetupRepository",
]
