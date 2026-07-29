from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.bot_provisioning import (
    BotProvisioningInvariantViolation,
    BotProvisioningNotFound,
    BotProvisioningProvider,
    BotProvisioningStatus,
    ManagedBotProvisioningRequest,
    VerifiedTelegramBot,
    normalize_display_name,
    normalize_provisioning_error_code,
    normalize_provisioning_idempotency_key,
    normalize_provisioning_provider,
    normalize_requested_username,
)
from clientplatform.domain.connections import normalize_credential_reference
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.safe_connection_repository import ConnectionRepository
from clientplatform.infrastructure.safe_tenancy_repository import TenancyRepository


_REQUEST_COLUMNS = """
    id, business_id, created_by_member_id, provider, status, idempotency_key,
    requested_username, display_name, credential_reference,
    webhook_secret_reference, external_bot_id, verified_username,
    connection_id, managed_bot_id, attempts, verification_started_at,
    created_at, updated_at, completed_at, failed_at, cancelled_at,
    last_error_code
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _request_from_row(row: Any) -> ManagedBotProvisioningRequest:
    return ManagedBotProvisioningRequest(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        created_by_member_id=str(_value(row, "created_by_member_id", 2)),
        provider=BotProvisioningProvider(str(_value(row, "provider", 3))),
        status=BotProvisioningStatus(str(_value(row, "status", 4))),
        idempotency_key=str(_value(row, "idempotency_key", 5)),
        requested_username=_value(row, "requested_username", 6),
        display_name=_value(row, "display_name", 7),
        credential_reference=_value(row, "credential_reference", 8),
        webhook_secret_reference=_value(row, "webhook_secret_reference", 9),
        external_bot_id=_value(row, "external_bot_id", 10),
        verified_username=_value(row, "verified_username", 11),
        connection_id=_value(row, "connection_id", 12),
        managed_bot_id=_value(row, "managed_bot_id", 13),
        attempts=int(_value(row, "attempts", 14)),
        verification_started_at=_value(row, "verification_started_at", 15),
        created_at=str(_value(row, "created_at", 16)),
        updated_at=str(_value(row, "updated_at", 17)),
        completed_at=_value(row, "completed_at", 18),
        failed_at=_value(row, "failed_at", 19),
        cancelled_at=_value(row, "cancelled_at", 20),
        last_error_code=_value(row, "last_error_code", 21),
    )


@dataclass(frozen=True, slots=True)
class ProvisioningVerificationLease:
    request: ManagedBotProvisioningRequest
    verification_token: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "verification_token",
            normalize_uuid(self.verification_token, field_name="verification_token"),
        )


class BotProvisioningRepository:
    """Durable, tenant-scoped BotFather provisioning transitions."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def _resolve_actor(self, actor: TenantContext) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_business()
        return current

    def _get_for_business(
        self,
        *,
        business_id: str,
        request_id: str,
    ) -> ManagedBotProvisioningRequest:
        row = self._conn.execute(
            f"SELECT {_REQUEST_COLUMNS} FROM managed_bot_provisioning_requests "
            "WHERE id=? AND business_id=? LIMIT 1",
            (
                normalize_uuid(request_id, field_name="bot_provisioning_request_id"),
                normalize_uuid(business_id, field_name="business_id"),
            ),
        ).fetchone()
        if row is None:
            raise BotProvisioningNotFound("managed bot provisioning request was not found")
        return _request_from_row(row)

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
        normalized_username = normalize_requested_username(requested_username)
        normalized_display_name = normalize_display_name(display_name)
        timestamp = str(now or _utc_now())
        request_id = str(uuid4())
        try:
            self._conn.execute(
                """
                INSERT INTO managed_bot_provisioning_requests(
                    id, business_id, created_by_member_id, provider, status,
                    idempotency_key, requested_username, display_name,
                    attempts, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'awaiting_secret', ?, ?, ?, 0, ?, ?)
                """,
                (
                    request_id,
                    current.business_id,
                    current.membership_id,
                    normalized_provider.value,
                    normalized_key,
                    normalized_username,
                    normalized_display_name,
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError:
            row = self._conn.execute(
                f"SELECT {_REQUEST_COLUMNS} FROM managed_bot_provisioning_requests "
                "WHERE business_id=? AND provider=? AND idempotency_key=? LIMIT 1",
                (
                    current.business_id,
                    normalized_provider.value,
                    normalized_key,
                ),
            ).fetchone()
            if row is None:
                raise
            return _request_from_row(row)
        return self._get_for_business(
            business_id=current.business_id,
            request_id=request_id,
        )

    def submit_secret_references(
        self,
        *,
        actor: TenantContext,
        request_id: str,
        credential_reference: str,
        webhook_secret_reference: str,
        now: str | None = None,
    ) -> ManagedBotProvisioningRequest:
        current = self._resolve_actor(actor)
        request = self._get_for_business(
            business_id=current.business_id,
            request_id=request_id,
        )
        if request.status == BotProvisioningStatus.COMPLETED:
            return request
        if request.status in {
            BotProvisioningStatus.VERIFYING,
            BotProvisioningStatus.CANCELLED,
        }:
            raise BotProvisioningInvariantViolation(
                "secret references cannot change in the current provisioning state"
            )
        token_reference = normalize_credential_reference(credential_reference)
        webhook_reference = normalize_credential_reference(webhook_secret_reference)
        timestamp = str(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE managed_bot_provisioning_requests
            SET credential_reference=?, webhook_secret_reference=?, status='ready',
                verification_token=NULL, verification_started_at=NULL,
                failed_at=NULL, last_error_code=NULL, updated_at=?
            WHERE id=? AND business_id=?
              AND status IN ('awaiting_secret','ready','failed')
            """,
            (
                token_reference,
                webhook_reference,
                timestamp,
                request.id,
                current.business_id,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise BotProvisioningInvariantViolation(
                "provisioning request cannot accept secret references"
            )
        return self._get_for_business(
            business_id=current.business_id,
            request_id=request.id,
        )

    def begin_verification(
        self,
        *,
        actor: TenantContext,
        request_id: str,
        now: str | None = None,
    ) -> ProvisioningVerificationLease:
        current = self._resolve_actor(actor)
        request = self._get_for_business(
            business_id=current.business_id,
            request_id=request_id,
        )
        if request.status == BotProvisioningStatus.COMPLETED:
            raise BotProvisioningInvariantViolation(
                "completed provisioning does not require verification"
            )
        if request.status != BotProvisioningStatus.READY:
            raise BotProvisioningInvariantViolation(
                "provisioning request must be ready before verification"
            )
        verification_token = str(uuid4())
        timestamp = str(now or _utc_now())
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

    def complete_verified(
        self,
        *,
        actor: TenantContext,
        lease: ProvisioningVerificationLease,
        verified_bot: VerifiedTelegramBot,
        now: str | None = None,
    ) -> ManagedBotProvisioningRequest:
        current = self._resolve_actor(actor)
        current.assert_business(lease.request.business_id)
        request = self._get_for_business(
            business_id=current.business_id,
            request_id=lease.request.id,
        )
        if request.status == BotProvisioningStatus.COMPLETED:
            return request
        token_row = self._conn.execute(
            """
            SELECT verification_token
            FROM managed_bot_provisioning_requests
            WHERE id=? AND business_id=? AND status='verifying'
            LIMIT 1
            """,
            (request.id, current.business_id),
        ).fetchone()
        observed_token = None if token_row is None else str(_value(token_row, "verification_token", 0))
        if observed_token != lease.verification_token:
            raise BotProvisioningInvariantViolation("provisioning verification lease was lost")
        if request.requested_username is not None and (
            request.requested_username != verified_bot.username
        ):
            raise BotProvisioningInvariantViolation(
                "verified Telegram username differs from the requested username"
            )
        if request.credential_reference is None or request.webhook_secret_reference is None:
            raise BotProvisioningInvariantViolation(
                "provisioning secret references are unavailable"
            )
        timestamp = str(now or _utc_now())
        connections = ConnectionRepository(self._conn)
        connection = connections.create_connection(
            actor=current,
            platform="telegram",
            connection_type="telegram_managed_bot",
            external_account_id=verified_bot.external_bot_id,
            credential_reference=request.credential_reference,
            permissions=("telegram:receive", "telegram:send", "telegram:webhook"),
            now=timestamp,
        )
        connections.activate_connection(
            actor=current,
            connection_id=connection.id,
            now=timestamp,
        )
        managed_bot = connections.register_managed_bot(
            actor=current,
            connection_id=connection.id,
            external_bot_id=verified_bot.external_bot_id,
            webhook_secret_reference=request.webhook_secret_reference,
            username=verified_bot.username,
            display_name=verified_bot.display_name or request.display_name,
            now=timestamp,
        )
        cursor = self._conn.execute(
            """
            UPDATE managed_bot_provisioning_requests
            SET status='completed', external_bot_id=?, verified_username=?,
                connection_id=?, managed_bot_id=?, verification_token=NULL,
                updated_at=?, completed_at=?, failed_at=NULL,
                cancelled_at=NULL, last_error_code=NULL
            WHERE id=? AND business_id=? AND status='verifying'
              AND verification_token=?
            """,
            (
                verified_bot.external_bot_id,
                verified_bot.username,
                connection.id,
                managed_bot.id,
                timestamp,
                timestamp,
                request.id,
                current.business_id,
                lease.verification_token,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise BotProvisioningInvariantViolation("provisioning verification lease was lost")
        return self._get_for_business(
            business_id=current.business_id,
            request_id=request.id,
        )

    def fail_verification(
        self,
        *,
        actor: TenantContext,
        lease: ProvisioningVerificationLease,
        error_code: str,
        now: str | None = None,
    ) -> ManagedBotProvisioningRequest:
        current = self._resolve_actor(actor)
        current.assert_business(lease.request.business_id)
        timestamp = str(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE managed_bot_provisioning_requests
            SET status='failed', verification_token=NULL, updated_at=?,
                failed_at=?, last_error_code=?
            WHERE id=? AND business_id=? AND status='verifying'
              AND verification_token=?
            """,
            (
                timestamp,
                timestamp,
                normalize_provisioning_error_code(error_code),
                lease.request.id,
                current.business_id,
                lease.verification_token,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            request = self._get_for_business(
                business_id=current.business_id,
                request_id=lease.request.id,
            )
            if request.status == BotProvisioningStatus.COMPLETED:
                return request
            raise BotProvisioningInvariantViolation("provisioning verification lease was lost")
        return self._get_for_business(
            business_id=current.business_id,
            request_id=lease.request.id,
        )

    def cancel(
        self,
        *,
        actor: TenantContext,
        request_id: str,
        now: str | None = None,
    ) -> ManagedBotProvisioningRequest:
        current = self._resolve_actor(actor)
        request = self._get_for_business(
            business_id=current.business_id,
            request_id=request_id,
        )
        if request.status == BotProvisioningStatus.CANCELLED:
            return request
        if request.status in {
            BotProvisioningStatus.VERIFYING,
            BotProvisioningStatus.COMPLETED,
        }:
            raise BotProvisioningInvariantViolation(
                "active or completed provisioning cannot be cancelled"
            )
        timestamp = str(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE managed_bot_provisioning_requests
            SET status='cancelled', verification_token=NULL,
                credential_reference=NULL, webhook_secret_reference=NULL,
                updated_at=?, cancelled_at=?
            WHERE id=? AND business_id=?
              AND status IN ('awaiting_secret','ready','failed')
            """,
            (timestamp, timestamp, request.id, current.business_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise BotProvisioningInvariantViolation(
                "provisioning request cannot be cancelled"
            )
        return self._get_for_business(
            business_id=current.business_id,
            request_id=request.id,
        )

    def get(
        self,
        *,
        actor: TenantContext,
        request_id: str,
    ) -> ManagedBotProvisioningRequest:
        current = self._resolve_actor(actor)
        return self._get_for_business(
            business_id=current.business_id,
            request_id=request_id,
        )

    def list_for_business(
        self,
        *,
        actor: TenantContext,
        limit: int = 50,
    ) -> list[ManagedBotProvisioningRequest]:
        current = self._resolve_actor(actor)
        bounded_limit = max(1, min(int(limit), 200))
        rows = self._conn.execute(
            f"SELECT {_REQUEST_COLUMNS} FROM managed_bot_provisioning_requests "
            "WHERE business_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
            (current.business_id, bounded_limit),
        ).fetchall()
        return [_request_from_row(row) for row in rows]
