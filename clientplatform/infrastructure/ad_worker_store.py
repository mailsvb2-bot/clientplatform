from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.ad_connections import (
    AdConnection,
    AdConnectionInvariantViolation,
    AdConnectionNotFound,
    AdConnectionStatus,
    AdProvider,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.ad_credential_vault import AdCredentialVault
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _optional(row: Any, key: str, position: int) -> str | None:
    value = _value(row, key, position)
    return None if value is None else str(value)


def _connection(row: Any) -> AdConnection:
    return AdConnection(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        provider=AdProvider(str(_value(row, "provider", 2))),
        external_account_id=str(_value(row, "external_account_id", 3)),
        external_login=str(_value(row, "external_login", 4)),
        permissions=tuple(json.loads(str(_value(row, "permissions_json", 5)) or "[]")),
        status=AdConnectionStatus(str(_value(row, "status", 6))),
        created_by_member_id=str(_value(row, "created_by_member_id", 7)),
        created_at=str(_value(row, "created_at", 8)),
        updated_at=str(_value(row, "updated_at", 9)),
        last_success_at=_optional(row, "last_success_at", 10),
        last_error_at=_optional(row, "last_error_at", 11),
        last_error_code=_optional(row, "last_error_code", 12),
    )


_SELECT = """
    SELECT id, business_id, provider, external_account_id, external_login,
           permissions_json, status, created_by_member_id, created_at, updated_at,
           last_success_at, last_error_at, last_error_code,
           credential_ciphertext
    FROM ad_connections
"""


class AdWorkerStore:
    """Provider-worker access that remains business-scoped without a human actor."""

    def __init__(self, conn: Any, *, vault: AdCredentialVault):
        self._conn = conn
        self._vault = vault

    def recover_stale_publication_leases(
        self,
        *,
        lock_ttl_seconds: int = 300,
        now: datetime | None = None,
    ) -> int:
        """Return abandoned publishing jobs to the idempotent retry queue."""

        timestamp_dt = now or datetime.now(timezone.utc)
        ttl = max(30, min(int(lock_ttl_seconds), 3600))
        timestamp = timestamp_dt.astimezone(timezone.utc).isoformat(timespec="seconds")
        stale = (timestamp_dt - timedelta(seconds=ttl)).astimezone(
            timezone.utc
        ).isoformat(timespec="seconds")
        cursor = self._conn.execute(
            """
            UPDATE ad_publication_jobs
            SET status='retry', available_at=?, updated_at=?,
                locked_at=NULL, lock_token=NULL,
                last_error_code='stale_publication_lease_recovered'
            WHERE status='publishing' AND locked_at IS NOT NULL AND locked_at<?
            """,
            (timestamp, timestamp, stale),
        )
        return int(getattr(cursor, "rowcount", 0) or 0)

    def has_publishing_job(
        self,
        *,
        business_id: str,
        connection_id: str,
    ) -> bool:
        """Return whether a provider call already owns a durable publication lease."""

        normalized_business = normalize_uuid(business_id, field_name="business_id")
        normalized_connection = normalize_uuid(
            connection_id,
            field_name="ad_connection_id",
        )
        row = self._conn.execute(
            """
            SELECT 1
            FROM ad_publication_jobs
            WHERE business_id=? AND connection_id=? AND status='publishing'
            LIMIT 1
            """,
            (normalized_business, normalized_connection),
        ).fetchone()
        return row is not None

    def load_active(
        self,
        *,
        business_id: str,
        connection_id: str,
    ) -> tuple[AdConnection, str]:
        normalized_business = normalize_uuid(business_id, field_name="business_id")
        normalized_connection = normalize_uuid(
            connection_id,
            field_name="ad_connection_id",
        )
        row = self._conn.execute(
            _SELECT
            + " WHERE id=? AND business_id=? AND status IN ('active', 'attention') LIMIT 1",
            (normalized_connection, normalized_business),
        ).fetchone()
        if row is None:
            raise AdConnectionNotFound("available advertising connection was not found")
        ciphertext = str(_value(row, "credential_ciphertext", 13) or "")
        if not ciphertext:
            raise AdConnectionInvariantViolation(
                "available advertising connection has no credential material"
            )
        return _connection(row), self._vault.open(ciphertext)

    def replace_token_bundle(
        self,
        *,
        connection: AdConnection,
        token_bundle_json: str,
        now: str | None = None,
    ) -> None:
        ciphertext = self._vault.seal(token_bundle_json)
        timestamp = str(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE ad_connections
            SET credential_ciphertext=?, status='active', updated_at=?,
                last_success_at=?, last_error_at=NULL, last_error_code=NULL
            WHERE id=? AND business_id=? AND status IN ('active', 'attention')
            """,
            (
                ciphertext,
                timestamp,
                timestamp,
                connection.id,
                connection.business_id,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise AdConnectionNotFound("available advertising connection was not found")

    def keep_available_after_job_failure(
        self,
        *,
        business_id: str,
        connection_id: str,
    ) -> None:
        """Undo account-level attention for a failure scoped to one ad job.

        Error timestamps and safe error codes remain available for diagnostics;
        only the account availability state is restored. Disabled and revoked
        connections are intentionally never changed by this operation.
        """

        normalized_business = normalize_uuid(business_id, field_name="business_id")
        normalized_connection = normalize_uuid(
            connection_id,
            field_name="ad_connection_id",
        )
        self._conn.execute(
            """
            UPDATE ad_connections SET status='active'
            WHERE id=? AND business_id=? AND status='attention'
            """,
            (normalized_connection, normalized_business),
        )


class AdConnectionLifecycleStore:
    """Owner-controlled credential blocking, revocation and local erasure."""

    def __init__(self, conn: Any, *, vault: AdCredentialVault):
        self._conn = conn
        self._vault = vault
        self._tenancy = TenancyRepository(conn)

    def begin_disconnect(
        self,
        *,
        actor: TenantContext,
        connection_id: str,
        now: str | None = None,
    ) -> tuple[AdConnection, str]:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_ad_connections()
        normalized_id = normalize_uuid(connection_id, field_name="ad_connection_id")
        timestamp = str(now or _utc_now())
        row = self._conn.execute(
            _SELECT + " WHERE id=? AND business_id=? LIMIT 1",
            (normalized_id, current.business_id),
        ).fetchone()
        if row is None:
            raise AdConnectionNotFound("advertising connection was not found")
        observed = _connection(row)
        ciphertext = str(_value(row, "credential_ciphertext", 13) or "")
        if observed.status == AdConnectionStatus.REVOKED or not ciphertext:
            return observed, ""

        cursor = self._conn.execute(
            """
            UPDATE ad_connections
            SET status='disabled', updated_at=?, last_error_code=NULL
            WHERE id=? AND business_id=? AND status!='revoked'
            """,
            (timestamp, normalized_id, current.business_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise AdConnectionNotFound("advertising connection was not found")
        self._cancel_unsubmitted_jobs(
            business_id=current.business_id,
            connection_id=normalized_id,
            timestamp=timestamp,
            error_code="connection_disconnect_started",
        )
        self._conn.execute(
            """
            INSERT INTO ad_audit_events(
                id, business_id, actor_member_id, action, subject_type,
                subject_id, details_json, created_at
            ) VALUES(?, ?, ?, 'ad_connection_disconnect_started',
                     'ad_connection', ?, '{}', ?)
            """,
            (
                str(uuid4()),
                current.business_id,
                current.membership_id,
                normalized_id,
                timestamp,
            ),
        )
        row = self._conn.execute(
            _SELECT + " WHERE id=? AND business_id=? LIMIT 1",
            (normalized_id, current.business_id),
        ).fetchone()
        if row is None:
            raise AdConnectionNotFound("advertising connection was not found")
        return _connection(row), self._vault.open(ciphertext)

    def load_for_disconnect(
        self,
        *,
        actor: TenantContext,
        connection_id: str,
    ) -> tuple[AdConnection, str]:
        """Compatibility read used by isolated lifecycle tests."""

        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_ad_connections()
        normalized_id = normalize_uuid(connection_id, field_name="ad_connection_id")
        row = self._conn.execute(
            _SELECT + " WHERE id=? AND business_id=? LIMIT 1",
            (normalized_id, current.business_id),
        ).fetchone()
        if row is None:
            raise AdConnectionNotFound("advertising connection was not found")
        connection = _connection(row)
        ciphertext = str(_value(row, "credential_ciphertext", 13) or "")
        if connection.status == AdConnectionStatus.REVOKED or not ciphertext:
            return connection, ""
        return connection, self._vault.open(ciphertext)

    def erase_after_provider_revocation(
        self,
        *,
        actor: TenantContext,
        connection_id: str,
        now: str | None = None,
    ) -> AdConnection:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_ad_connections()
        normalized_id = normalize_uuid(connection_id, field_name="ad_connection_id")
        timestamp = str(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE ad_connections
            SET credential_ciphertext='', status='revoked', updated_at=?,
                last_error_at=NULL, last_error_code=NULL
            WHERE id=? AND business_id=?
            """,
            (timestamp, normalized_id, current.business_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise AdConnectionNotFound("advertising connection was not found")
        self._cancel_unsubmitted_jobs(
            business_id=current.business_id,
            connection_id=normalized_id,
            timestamp=timestamp,
            error_code="connection_revoked",
        )
        self._conn.execute(
            """
            INSERT INTO ad_audit_events(
                id, business_id, actor_member_id, action, subject_type,
                subject_id, details_json, created_at
            ) VALUES(?, ?, ?, 'ad_connection_revoked', 'ad_connection', ?, '{}', ?)
            """,
            (
                str(uuid4()),
                current.business_id,
                current.membership_id,
                normalized_id,
                timestamp,
            ),
        )
        row = self._conn.execute(
            _SELECT + " WHERE id=? AND business_id=? LIMIT 1",
            (normalized_id, current.business_id),
        ).fetchone()
        if row is None:
            raise AdConnectionNotFound("advertising connection was not found")
        return _connection(row)

    def _cancel_unsubmitted_jobs(
        self,
        *,
        business_id: str,
        connection_id: str,
        timestamp: str,
        error_code: str,
    ) -> None:
        # A publishing row owns the durable provider-call lease. Never rewrite
        # it to cancelled: disconnect first disables the connection so no new
        # jobs can start, then the application waits for this lease to finish.
        self._conn.execute(
            """
            UPDATE ad_publication_jobs
            SET status='cancelled', updated_at=?, locked_at=NULL, lock_token=NULL,
                last_error_code=?
            WHERE connection_id=? AND business_id=?
              AND status IN ('draft', 'queued', 'retry')
            """,
            (timestamp, error_code, connection_id, business_id),
        )


__all__ = ["AdConnectionLifecycleStore", "AdWorkerStore"]
