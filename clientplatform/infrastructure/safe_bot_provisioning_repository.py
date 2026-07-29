from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from clientplatform.domain.bot_provisioning import (
    BotProvisioningInvariantViolation,
    BotProvisioningProvider,
    BotProvisioningStatus,
    ManagedBotProvisioningRequest,
    normalize_provisioning_idempotency_key,
    normalize_provisioning_provider,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.bot_provisioning_repository import (
    _REQUEST_COLUMNS,
    _request_from_row,
    BotProvisioningRepository as _BaseBotProvisioningRepository,
    ProvisioningVerificationLease,
)
from services.db.core import PostgresCompatConnection

_SELECT_BY_IDEMPOTENCY = (
    "SELECT "
    + _REQUEST_COLUMNS
    + " FROM managed_bot_provisioning_requests "
    "WHERE business_id=? AND provider=? AND idempotency_key=? LIMIT 1"
)


def _provisioning_lock_key(subject: str) -> int:
    digest = hashlib.blake2b(
        str(subject).encode("utf-8"),
        digest_size=8,
        person=b"cp-botprov-v1",
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def _serialize_request(
    conn: object,
    *,
    business_id: str,
    provider: str,
    idempotency_key: str,
) -> None:
    if isinstance(conn, PostgresCompatConnection):
        subject = f"{business_id}:{provider}:{idempotency_key}"
        conn.execute(
            "SELECT pg_advisory_xact_lock(?)",
            (_provisioning_lock_key(subject),),
        ).fetchone()
        return
    if isinstance(conn, sqlite3.Connection) and not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def _verification_clock(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("verification timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("verification timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


class BotProvisioningRepository(_BaseBotProvisioningRepository):
    """Canonical provisioning repository with serialization and lease recovery."""

    def create_request(
        self,
        *,
        actor: TenantContext,
        idempotency_key: str,
        provider: BotProvisioningProvider | str = BotProvisioningProvider.BOTFATHER,
        requested_username: str | None = None,
        display_name: str | None = None,
        now: str | None = None,
    ) -> ManagedBotProvisioningRequest:
        current = self._resolve_actor(actor)
        normalized_provider = normalize_provisioning_provider(provider)
        normalized_key = normalize_provisioning_idempotency_key(idempotency_key)
        _serialize_request(
            self._conn,
            business_id=current.business_id,
            provider=normalized_provider.value,
            idempotency_key=normalized_key,
        )
        row = self._conn.execute(
            _SELECT_BY_IDEMPOTENCY,
            (
                current.business_id,
                normalized_provider.value,
                normalized_key,
            ),
        ).fetchone()
        if row is not None:
            return _request_from_row(row)
        return super().create_request(
            actor=current,
            idempotency_key=normalized_key,
            provider=normalized_provider,
            requested_username=requested_username,
            display_name=display_name,
            now=now,
        )

    def begin_verification(
        self,
        *,
        actor: TenantContext,
        request_id: str,
        now: str | None = None,
        stale_after_seconds: int = 300,
    ) -> ProvisioningVerificationLease:
        stale_after = int(stale_after_seconds)
        if stale_after < 30 or stale_after > 3600:
            raise ValueError("stale_after_seconds must be between 30 and 3600")
        current = self._resolve_actor(actor)
        request = self._get_for_business(
            business_id=current.business_id,
            request_id=request_id,
        )
        if request.status == BotProvisioningStatus.COMPLETED:
            raise BotProvisioningInvariantViolation(
                "completed provisioning does not require verification"
            )

        clock = _verification_clock(now)
        timestamp = clock.isoformat(timespec="seconds")
        cutoff = (clock - timedelta(seconds=stale_after)).isoformat(
            timespec="seconds"
        )
        if request.status == BotProvisioningStatus.VERIFYING:
            recovered = self._conn.execute(
                """
                UPDATE managed_bot_provisioning_requests
                SET status='ready', verification_token=NULL,
                    verification_started_at=NULL, updated_at=?,
                    failed_at=NULL, last_error_code='verification_lease_expired'
                WHERE id=? AND business_id=? AND status='verifying'
                  AND verification_started_at<=?
                """,
                (
                    timestamp,
                    request.id,
                    current.business_id,
                    cutoff,
                ),
            )
            if int(getattr(recovered, "rowcount", 0) or 0) != 1:
                raise BotProvisioningInvariantViolation(
                    "provisioning request is already being verified"
                )
            request = self._get_for_business(
                business_id=current.business_id,
                request_id=request.id,
            )

        if request.status != BotProvisioningStatus.READY:
            raise BotProvisioningInvariantViolation(
                "provisioning request must be ready before verification"
            )
        verification_token = str(uuid4())
        cursor = self._conn.execute(
            """
            UPDATE managed_bot_provisioning_requests
            SET status='verifying', attempts=attempts+1,
                verification_token=?, verification_started_at=?, updated_at=?,
                failed_at=NULL, last_error_code=NULL
            WHERE id=? AND business_id=? AND status='ready'
            """,
            (
                verification_token,
                timestamp,
                timestamp,
                request.id,
                current.business_id,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise BotProvisioningInvariantViolation(
                "provisioning request was claimed by another verifier"
            )
        return ProvisioningVerificationLease(
            request=self._get_for_business(
                business_id=current.business_id,
                request_id=request.id,
            ),
            verification_token=verification_token,
        )
