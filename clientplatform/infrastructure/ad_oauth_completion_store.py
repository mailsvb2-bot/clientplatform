from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.ad_connections import (
    AdConnectionInvariantViolation,
    AdOAuthSession,
    AdProvider,
    oauth_state_hash,
)
from clientplatform.infrastructure.ad_credential_vault import AdCredentialVault


_DEFAULT_LEASE_SECONDS = 90
_MIN_LEASE_SECONDS = 30
_MAX_LEASE_SECONDS = 300


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


@dataclass(frozen=True, slots=True)
class AdOAuthCompletionReservation:
    session: AdOAuthSession
    verifier: str
    attempt_id: str
    attempt_expires_at: str


class AdOAuthCompletionStore:
    """Short-lived durable lease for provider OAuth completion.

    The lease is committed before any provider I/O. Provider calls therefore run
    without holding a database transaction or pool connection. A crashed worker
    cannot burn the OAuth state forever: after the bounded lease expires another
    completion attempt may reclaim it. Successful local activation still consumes
    the state atomically in the final short database transaction.
    """

    def __init__(self, conn: Any, *, vault: AdCredentialVault):
        self._conn = conn
        self._vault = vault

    def reserve(
        self,
        *,
        state: str,
        now: datetime | None = None,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
    ) -> AdOAuthCompletionReservation:
        timestamp = now or _utc_now()
        stamp = _iso(timestamp)
        lease = max(_MIN_LEASE_SECONDS, min(int(lease_seconds), _MAX_LEASE_SECONDS))
        lease_expires_at = _iso(timestamp + timedelta(seconds=lease))
        digest = oauth_state_hash(state)
        attempt_id = str(uuid4())
        cursor = self._conn.execute(
            """
            UPDATE ad_oauth_sessions
            SET completion_attempt_id=?, completion_attempt_expires_at=?
            WHERE state_hash=?
              AND consumed_at IS NULL
              AND expires_at>=?
              AND (
                    completion_attempt_id IS NULL
                    OR completion_attempt_expires_at IS NULL
                    OR completion_attempt_expires_at<?
              )
            """,
            (attempt_id, lease_expires_at, digest, stamp, stamp),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            active = self._conn.execute(
                """
                SELECT state_hash
                FROM ad_oauth_sessions
                WHERE state_hash=? AND consumed_at IS NULL AND expires_at>=?
                LIMIT 1
                """,
                (digest, stamp),
            ).fetchone()
            if active is None:
                raise AdConnectionInvariantViolation(
                    "OAuth session is invalid, expired or already used"
                )
            raise AdConnectionInvariantViolation("OAuth completion is already in progress")

        row = self._conn.execute(
            """
            SELECT state_hash, business_id, user_id, membership_id, provider,
                   verifier_ciphertext, expires_at, consumed_at, created_at,
                   completion_attempt_id, completion_attempt_expires_at
            FROM ad_oauth_sessions
            WHERE state_hash=? AND completion_attempt_id=? AND consumed_at IS NULL
            LIMIT 1
            """,
            (digest, attempt_id),
        ).fetchone()
        if row is None:
            raise AdConnectionInvariantViolation("OAuth completion lease was lost")
        session = AdOAuthSession(
            state_hash=str(_value(row, "state_hash", 0)),
            business_id=str(_value(row, "business_id", 1)),
            user_id=int(_value(row, "user_id", 2)),
            membership_id=str(_value(row, "membership_id", 3)),
            provider=AdProvider(str(_value(row, "provider", 4))),
            verifier_ciphertext=str(_value(row, "verifier_ciphertext", 5)),
            expires_at=str(_value(row, "expires_at", 6)),
            consumed_at=None,
            created_at=str(_value(row, "created_at", 8)),
        )
        return AdOAuthCompletionReservation(
            session=session,
            verifier=self._vault.open(session.verifier_ciphertext),
            attempt_id=attempt_id,
            attempt_expires_at=str(_value(row, "completion_attempt_expires_at", 10)),
        )

    def release(self, *, reservation: AdOAuthCompletionReservation) -> None:
        cursor = self._conn.execute(
            """
            UPDATE ad_oauth_sessions
            SET completion_attempt_id=NULL, completion_attempt_expires_at=NULL
            WHERE state_hash=? AND consumed_at IS NULL AND completion_attempt_id=?
            """,
            (reservation.session.state_hash, reservation.attempt_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise AdConnectionInvariantViolation("OAuth completion lease was lost")

    def consume(
        self,
        *,
        reservation: AdOAuthCompletionReservation,
        now: datetime | None = None,
    ) -> AdOAuthSession:
        stamp = _iso(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE ad_oauth_sessions
            SET consumed_at=?, completion_attempt_id=NULL,
                completion_attempt_expires_at=NULL
            WHERE state_hash=? AND consumed_at IS NULL AND completion_attempt_id=?
            """,
            (stamp, reservation.session.state_hash, reservation.attempt_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise AdConnectionInvariantViolation("OAuth completion lease was lost")
        return AdOAuthSession(
            state_hash=reservation.session.state_hash,
            business_id=reservation.session.business_id,
            user_id=reservation.session.user_id,
            membership_id=reservation.session.membership_id,
            provider=reservation.session.provider,
            verifier_ciphertext=reservation.session.verifier_ciphertext,
            expires_at=reservation.session.expires_at,
            consumed_at=stamp,
            created_at=reservation.session.created_at,
        )


__all__ = ["AdOAuthCompletionReservation", "AdOAuthCompletionStore"]
