from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.safe_tenancy_repository import TenancyRepository
from services.db.core import PostgresCompatConnection


_SETUP_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,160}")
_SETUP_PLATFORMS = frozenset({ConnectionPlatform.VK, ConnectionPlatform.MAX})


class NativeMessengerSetupRejected(RuntimeError):
    """A secure native-messenger setup capability is invalid or unavailable."""


@dataclass(frozen=True, slots=True)
class IssuedNativeMessengerSetup:
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


def _setup_issue_lock_key(*, business_id: str, platform: str) -> int:
    digest = hashlib.blake2b(
        f"{business_id}:{platform}".encode("utf-8"),
        digest_size=8,
        person=b"cp-msgsetup-v1",
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def _serialize_setup_issue(
    conn: object,
    *,
    business_id: str,
    platform: str,
) -> None:
    """Serialize replacement of the active setup capability.

    PostgreSQL needs an explicit transaction-scoped lock because concurrent
    transactions cannot see each other's uncommitted replacement rows. SQLite
    uses its write transaction for the equivalent local/dev guarantee.
    """

    if isinstance(conn, PostgresCompatConnection):
        conn.execute(
            "SELECT pg_advisory_xact_lock(?)",
            (_setup_issue_lock_key(business_id=business_id, platform=platform),),
        ).fetchone()
        return
    if isinstance(conn, sqlite3.Connection) and not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def _platform(value: ConnectionPlatform | str) -> ConnectionPlatform:
    platform = value if isinstance(value, ConnectionPlatform) else ConnectionPlatform(str(value).strip().lower())
    if platform not in _SETUP_PLATFORMS:
        raise ValueError("messenger setup supports only VK or MAX")
    return platform


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


class NativeMessengerSetupRepository:
    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def issue(
        self,
        *,
        actor: TenantContext,
        platform: ConnectionPlatform | str,
        ttl_seconds: int = 600,
        now: datetime | None = None,
    ) -> IssuedNativeMessengerSetup:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_business()
        selected_platform = _platform(platform)
        _serialize_setup_issue(
            self._conn,
            business_id=current.business_id,
            platform=selected_platform.value,
        )
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_business()
        lifetime = max(60, min(int(ttl_seconds), 1800))
        created = now or _utc_now()
        created_iso = _iso(created)
        expires_at = _iso(created + timedelta(seconds=lifetime))
        raw_token = secrets.token_urlsafe(32)
        digest = _digest(raw_token)
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
                str(uuid4()),
                current.business_id,
                selected_platform.value,
                digest,
                current.membership_id,
                created_iso,
                expires_at,
            ),
        )
        return IssuedNativeMessengerSetup(
            token=raw_token,
            business_id=current.business_id,
            platform=selected_platform,
            expires_at=expires_at,
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
    "NativeMessengerSetupRejected",
    "NativeMessengerSetupRepository",
]
