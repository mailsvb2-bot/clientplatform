from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.ad_connections import (
    AdConnection,
    AdConnectionInvariantViolation,
    AdConnectionNotFound,
    AdConnectionStatus,
    AdOAuthSession,
    AdProvider,
    AdPublicationJob,
    AdPublicationStatus,
    normalize_external_account_id,
    normalize_external_campaign_id,
    normalize_external_login,
    normalize_region_ids,
    oauth_state_hash,
    publication_idempotency_key,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.ad_credential_vault import AdCredentialVault
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


_DIRECT_OWNER_CONFLICT = "direct_account_owned_by_another_business"
_EXPECTED_DIRECT_IDENTITY_ERRORS = {
    "direct_identity_reverification_pending",
    "direct_identity_reverification_ambiguous",
    "direct_identity_reverification_required",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _connection_from_row(row: Any) -> AdConnection:
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


def _optional(row: Any, key: str, position: int) -> str | None:
    value = _value(row, key, position)
    return None if value is None else str(value)


def _job_from_row(row: Any) -> AdPublicationJob:
    return AdPublicationJob(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        promotion_campaign_id=str(_value(row, "promotion_campaign_id", 2)),
        connection_id=str(_value(row, "connection_id", 3)),
        external_campaign_id=str(_value(row, "external_campaign_id", 4)),
        external_campaign_name=str(_value(row, "external_campaign_name", 5)),
        region_ids=tuple(json.loads(str(_value(row, "region_ids_json", 6)))),
        source_url=str(_value(row, "source_url", 7)),
        title=str(_value(row, "title", 8)),
        text=str(_value(row, "text", 9)),
        status=AdPublicationStatus(str(_value(row, "status", 10))),
        idempotency_key=str(_value(row, "idempotency_key", 11)),
        external_ad_group_id=_optional(row, "external_ad_group_id", 12),
        external_ad_id=_optional(row, "external_ad_id", 13),
        attempts=int(_value(row, "attempts", 14) or 0),
        last_error_code=_optional(row, "last_error_code", 15),
        created_by_member_id=str(_value(row, "created_by_member_id", 16)),
        created_at=str(_value(row, "created_at", 17)),
        updated_at=str(_value(row, "updated_at", 18)),
        submitted_at=_optional(row, "submitted_at", 19),
    )


def _translate_direct_identity_integrity_error(exc: sqlite3.IntegrityError) -> None:
    text = str(exc).strip().lower()
    if (
        "uq_ad_connections_direct_global_owner" in text
        or "unique constraint failed: ad_connections.provider, ad_connections.external_account_id" in text
    ):
        raise AdConnectionInvariantViolation(_DIRECT_OWNER_CONFLICT) from exc
    for code in _EXPECTED_DIRECT_IDENTITY_ERRORS:
        if code in text:
            raise AdConnectionInvariantViolation(code) from exc
    raise exc


_CONNECTION_SELECT = """
    SELECT id, business_id, provider, external_account_id, external_login,
           permissions_json, status, created_by_member_id, created_at, updated_at,
           last_success_at, last_error_at, last_error_code
    FROM ad_connections
"""
_JOB_SELECT = """
    SELECT id, business_id, promotion_campaign_id, connection_id,
           external_campaign_id, external_campaign_name, region_ids_json,
           source_url, title, text, status, idempotency_key,
           external_ad_group_id, external_ad_id, attempts, last_error_code,
           created_by_member_id, created_at, updated_at, submitted_at
    FROM ad_publication_jobs
"""


class AdConnectionRepository:
    """Tenant-safe OAuth connections and an idempotent advertising outbox."""

    def __init__(self, conn: Any, *, vault: AdCredentialVault):
        self._conn = conn
        self._vault = vault
        self._tenancy = TenancyRepository(conn)

    def _actor(self, actor: TenantContext, *, connections: bool = False) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        if connections:
            current.assert_can_manage_ad_connections()
        else:
            current.assert_can_manage_promotions()
        return current

    def create_oauth_session(
        self,
        *,
        actor: TenantContext,
        provider: AdProvider,
        state: str,
        verifier: str,
        ttl_seconds: int = 600,
        now: datetime | None = None,
    ) -> AdOAuthSession:
        current = self._actor(actor, connections=True)
        timestamp = now or _utc_now()
        ttl = max(120, min(int(ttl_seconds), 1800))
        state_digest = oauth_state_hash(state)
        verifier_ciphertext = self._vault.seal(verifier)
        expires_at = _iso(timestamp + timedelta(seconds=ttl))
        created_at = _iso(timestamp)
        self._conn.execute(
            "DELETE FROM ad_oauth_sessions WHERE expires_at<? OR consumed_at IS NOT NULL",
            (created_at,),
        )
        self._conn.execute(
            """
            INSERT INTO ad_oauth_sessions(
                state_hash, business_id, user_id, membership_id, provider,
                verifier_ciphertext, expires_at, consumed_at, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                state_digest,
                current.business_id,
                current.user_id,
                current.membership_id,
                provider.value,
                verifier_ciphertext,
                expires_at,
                created_at,
            ),
        )
        self._audit(
            business_id=current.business_id,
            actor_member_id=current.membership_id,
            action="ad_oauth_started",
            subject_type="provider",
            subject_id=provider.value,
            details={"expires_at": expires_at},
            now=created_at,
        )
        return AdOAuthSession(
            state_hash=state_digest,
            business_id=current.business_id,
            user_id=current.user_id,
            membership_id=current.membership_id,
            provider=provider,
            verifier_ciphertext=verifier_ciphertext,
            expires_at=expires_at,
            created_at=created_at,
        )

    def consume_oauth_session(
        self,
        *,
        state: str,
        now: datetime | None = None,
    ) -> tuple[AdOAuthSession, str]:
        timestamp = _iso(now or _utc_now())
        digest = oauth_state_hash(state)
        row = self._conn.execute(
            """
            SELECT state_hash, business_id, user_id, membership_id, provider,
                   verifier_ciphertext, expires_at, consumed_at, created_at
            FROM ad_oauth_sessions
            WHERE state_hash=? AND consumed_at IS NULL AND expires_at>=?
            LIMIT 1
            """,
            (digest, timestamp),
        ).fetchone()
        if row is None:
            raise AdConnectionInvariantViolation("OAuth session is invalid, expired or already used")
        cursor = self._conn.execute(
            """
            UPDATE ad_oauth_sessions SET consumed_at=?
            WHERE state_hash=? AND consumed_at IS NULL
            """,
            (timestamp, digest),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise AdConnectionInvariantViolation("OAuth session was already consumed")
        session = AdOAuthSession(
            state_hash=str(_value(row, "state_hash", 0)),
            business_id=str(_value(row, "business_id", 1)),
            user_id=int(_value(row, "user_id", 2)),
            membership_id=str(_value(row, "membership_id", 3)),
            provider=AdProvider(str(_value(row, "provider", 4))),
            verifier_ciphertext=str(_value(row, "verifier_ciphertext", 5)),
            expires_at=str(_value(row, "expires_at", 6)),
            consumed_at=timestamp,
            created_at=str(_value(row, "created_at", 8)),
        )
        return session, self._vault.open(session.verifier_ciphertext)

    def activate_oauth_connection(
        self,
        *,
        session: AdOAuthSession,
        external_account_id: str,
        external_login: str,
        token_bundle_json: str,
        permissions: tuple[str, ...],
        now: datetime | None = None,
    ) -> AdConnection:
        account_id = normalize_external_account_id(external_account_id)
        login = normalize_external_login(external_login)
        ciphertext = self._vault.seal(token_bundle_json)
        timestamp = _iso(now or _utc_now())
        connection_id = str(uuid4())
        encoded_permissions = json.dumps(sorted(set(permissions)), ensure_ascii=False)
        try:
            self._conn.execute(
                """
                INSERT INTO ad_connections(
                    id, business_id, provider, external_account_id, external_login,
                    identity_source, credential_ciphertext, permissions_json, status,
                    created_by_member_id, created_at, updated_at,
                    last_success_at, last_error_at, last_error_code
                ) VALUES(?, ?, ?, ?, ?, 'direct_client_id', ?, ?, 'active', ?, ?, ?, ?, NULL, NULL)
                ON CONFLICT(business_id, provider, external_account_id) DO UPDATE SET
                    external_login=excluded.external_login,
                    identity_source='direct_client_id',
                    credential_ciphertext=excluded.credential_ciphertext,
                    permissions_json=excluded.permissions_json,
                    status='active',
                    updated_at=excluded.updated_at,
                    last_success_at=excluded.last_success_at,
                    last_error_at=NULL,
                    last_error_code=NULL
                """,
                (
                    connection_id,
                    session.business_id,
                    session.provider.value,
                    account_id,
                    login,
                    ciphertext,
                    encoded_permissions,
                    session.membership_id,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            _translate_direct_identity_integrity_error(exc)
        connection = self._find_connection(
            business_id=session.business_id,
            provider=session.provider,
            external_account_id=account_id,
        )
        if connection is None:
            raise AdConnectionNotFound("advertising connection activation failed")
        self._audit(
            business_id=session.business_id,
            actor_member_id=session.membership_id,
            action="ad_connection_activated",
            subject_type="ad_connection",
            subject_id=connection.id,
            details={"provider": session.provider.value, "external_account_id": account_id},
            now=timestamp,
        )
        return connection

    def list_connections(self, *, actor: TenantContext) -> list[AdConnection]:
        current = self._actor(actor, connections=True)
        rows = self._conn.execute(
            _CONNECTION_SELECT
            + " WHERE business_id=? ORDER BY created_at DESC, id DESC",
            (current.business_id,),
        ).fetchall()
        return [_connection_from_row(row) for row in rows]

    def get_connection(self, *, actor: TenantContext, connection_id: str) -> AdConnection:
        current = self._actor(actor)
        return self._get_connection(
            business_id=current.business_id,
            connection_id=normalize_uuid(connection_id, field_name="ad_connection_id"),
        )

    def token_bundle(self, *, connection: AdConnection) -> str:
        row = self._conn.execute(
            """
            SELECT credential_ciphertext FROM ad_connections
            WHERE id=? AND business_id=? AND status='active' LIMIT 1
            """,
            (connection.id, connection.business_id),
        ).fetchone()
        if row is None:
            raise AdConnectionNotFound("active advertising connection was not found")
        return self._vault.open(str(_value(row, "credential_ciphertext", 0)))

    def disable_connection(
        self,
        *,
        actor: TenantContext,
        connection_id: str,
        now: datetime | None = None,
    ) -> AdConnection:
        current = self._actor(actor, connections=True)
        normalized_id = normalize_uuid(connection_id, field_name="ad_connection_id")
        timestamp = _iso(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE ad_connections SET status='disabled', updated_at=?
            WHERE id=? AND business_id=? AND status!='revoked'
            """,
            (timestamp, normalized_id, current.business_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise AdConnectionNotFound("advertising connection was not found")
        self._audit(
            business_id=current.business_id,
            actor_member_id=current.membership_id,
            action="ad_connection_disabled",
            subject_type="ad_connection",
            subject_id=normalized_id,
            details={},
            now=timestamp,
        )
        return self._get_connection(
            business_id=current.business_id,
            connection_id=normalized_id,
        )

    def create_or_get_job(
        self,
        *,
        actor: TenantContext,
        promotion_campaign_id: str,
        connection_id: str,
        external_campaign_id: str,
        external_campaign_name: str,
        region_ids: tuple[int, ...],
        source_url: str,
        title: str,
        text: str,
        creative_id: str,
        now: datetime | None = None,
    ) -> AdPublicationJob:
        current = self._actor(actor)
        campaign_id = normalize_uuid(
            promotion_campaign_id,
            field_name="promotion_campaign_id",
        )
        selected_connection = self._get_connection(
            business_id=current.business_id,
            connection_id=normalize_uuid(connection_id, field_name="ad_connection_id"),
        )
        if selected_connection.status != AdConnectionStatus.ACTIVE:
            raise AdConnectionInvariantViolation("advertising account is not active")
        provider_campaign_id = normalize_external_campaign_id(external_campaign_id)
        regions = normalize_region_ids(region_ids)
        href = str(source_url or "").strip()
        if not href.startswith("https://") or len(href) > 1024:
            raise ValueError("advertising destination must be an HTTPS URL")
        normalized_title = " ".join(str(title or "").split())[:56].strip()
        normalized_text = " ".join(str(text or "").split())[:81].strip()
        if not normalized_title or not normalized_text:
            raise ValueError("advertising title and text must not be empty")
        key = publication_idempotency_key(
            business_id=current.business_id,
            promotion_campaign_id=campaign_id,
            connection_id=selected_connection.id,
            external_campaign_id=provider_campaign_id,
            region_ids=regions,
            creative_id=creative_id,
        )
        timestamp = _iso(now or _utc_now())
        job_id = str(uuid4())
        self._conn.execute(
            """
            INSERT INTO ad_publication_jobs(
                id, business_id, promotion_campaign_id, connection_id,
                external_campaign_id, external_campaign_name, region_ids_json,
                source_url, title, text, status, idempotency_key,
                external_ad_group_id, external_ad_id, attempts, available_at,
                locked_at, lock_token, last_error_code, created_by_member_id,
                created_at, updated_at, submitted_at, dead_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, NULL, NULL, 0, ?,
                     NULL, NULL, NULL, ?, ?, ?, NULL, NULL)
            ON CONFLICT(business_id, idempotency_key) DO NOTHING
            """,
            (
                job_id,
                current.business_id,
                campaign_id,
                selected_connection.id,
                provider_campaign_id,
                " ".join(str(external_campaign_name or "").split())[:255],
                json.dumps(regions),
                href,
                normalized_title,
                normalized_text,
                key,
                timestamp,
                current.membership_id,
                timestamp,
                timestamp,
            ),
        )
        row = self._conn.execute(
            _JOB_SELECT + " WHERE business_id=? AND idempotency_key=? LIMIT 1",
            (current.business_id, key),
        ).fetchone()
        if row is None:
            raise AdConnectionNotFound("advertising publication draft was not created")
        return _job_from_row(row)

    def queue_job(
        self,
        *,
        actor: TenantContext,
        job_id: str,
        now: datetime | None = None,
    ) -> AdPublicationJob:
        current = self._actor(actor)
        normalized_id = normalize_uuid(job_id, field_name="ad_publication_job_id")
        timestamp = _iso(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE ad_publication_jobs
            SET status='queued', available_at=?, updated_at=?,
                last_error_code=NULL, locked_at=NULL, lock_token=NULL
            WHERE id=? AND business_id=? AND status IN ('draft', 'failed')
            """,
            (timestamp, timestamp, normalized_id, current.business_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) == 0:
            job = self._get_job(business_id=current.business_id, job_id=normalized_id)
            if job.status not in {
                AdPublicationStatus.QUEUED,
                AdPublicationStatus.PUBLISHING,
                AdPublicationStatus.RETRY,
                AdPublicationStatus.SUBMITTED,
            }:
                raise AdConnectionInvariantViolation("advertising draft cannot be queued")
            return job
        self._audit(
            business_id=current.business_id,
            actor_member_id=current.membership_id,
            action="ad_publication_confirmed",
            subject_type="ad_publication_job",
            subject_id=normalized_id,
            details={},
            now=timestamp,
        )
        return self._get_job(business_id=current.business_id, job_id=normalized_id)

    def list_jobs(self, *, actor: TenantContext, limit: int = 20) -> list[AdPublicationJob]:
        current = self._actor(actor)
        rows = self._conn.execute(
            _JOB_SELECT
            + " WHERE business_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
            (current.business_id, max(1, min(int(limit), 100))),
        ).fetchall()
        return [_job_from_row(row) for row in rows]

    def claim_due_job(
        self,
        *,
        lock_ttl_seconds: int = 300,
        now: datetime | None = None,
    ) -> tuple[AdPublicationJob, str] | None:
        timestamp_dt = now or _utc_now()
        timestamp = _iso(timestamp_dt)
        stale = _iso(timestamp_dt - timedelta(seconds=max(30, lock_ttl_seconds)))
        row = self._conn.execute(
            _JOB_SELECT
            + """
              WHERE status IN ('queued', 'retry') AND available_at<=?
                AND (locked_at IS NULL OR locked_at<?)
              ORDER BY available_at, created_at, id
              LIMIT 1
            """,
            (timestamp, stale),
        ).fetchone()
        if row is None:
            return None
        job = _job_from_row(row)
        lock_token = str(uuid4())
        cursor = self._conn.execute(
            """
            UPDATE ad_publication_jobs
            SET status='publishing', attempts=attempts+1, locked_at=?,
                lock_token=?, updated_at=?
            WHERE id=? AND business_id=? AND status IN ('queued', 'retry')
              AND (locked_at IS NULL OR locked_at<?)
            """,
            (
                timestamp,
                lock_token,
                timestamp,
                job.id,
                job.business_id,
                stale,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            return None
        return self._get_job(business_id=job.business_id, job_id=job.id), lock_token

    def complete_job(
        self,
        *,
        job: AdPublicationJob,
        lock_token: str,
        external_ad_group_id: str,
        external_ad_id: str,
        now: datetime | None = None,
    ) -> AdPublicationJob:
        timestamp = _iso(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE ad_publication_jobs
            SET status='submitted', external_ad_group_id=?, external_ad_id=?,
                submitted_at=?, updated_at=?, locked_at=NULL, lock_token=NULL,
                last_error_code=NULL
            WHERE id=? AND business_id=? AND status='publishing' AND lock_token=?
            """,
            (
                str(external_ad_group_id),
                str(external_ad_id),
                timestamp,
                timestamp,
                job.id,
                job.business_id,
                lock_token,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise AdConnectionInvariantViolation("advertising publication lease was lost")
        self._mark_connection_success(job.connection_id, job.business_id, timestamp)
        self._audit(
            business_id=job.business_id,
            actor_member_id=job.created_by_member_id,
            action="ad_publication_submitted",
            subject_type="ad_publication_job",
            subject_id=job.id,
            details={"provider_ad_group_id": str(external_ad_group_id), "provider_ad_id": str(external_ad_id)},
            now=timestamp,
        )
        return self._get_job(business_id=job.business_id, job_id=job.id)

    def fail_job(
        self,
        *,
        job: AdPublicationJob,
        lock_token: str,
        error_code: str,
        retryable: bool,
        max_attempts: int = 5,
        now: datetime | None = None,
    ) -> AdPublicationJob:
        timestamp_dt = now or _utc_now()
        terminal = not retryable or job.attempts >= max(1, max_attempts)
        status = "failed" if terminal else "retry"
        delay_seconds = min(3600, 30 * (2 ** max(job.attempts - 1, 0)))
        available_at = _iso(timestamp_dt if terminal else timestamp_dt + timedelta(seconds=delay_seconds))
        timestamp = _iso(timestamp_dt)
        safe_error = re_safe_error(error_code)
        cursor = self._conn.execute(
            """
            UPDATE ad_publication_jobs
            SET status=?, available_at=?, last_error_code=?, updated_at=?,
                locked_at=NULL, lock_token=NULL, dead_at=?
            WHERE id=? AND business_id=? AND status='publishing' AND lock_token=?
            """,
            (
                status,
                available_at,
                safe_error,
                timestamp,
                timestamp if terminal else None,
                job.id,
                job.business_id,
                lock_token,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise AdConnectionInvariantViolation("advertising publication lease was lost")
        self._mark_connection_error(job.connection_id, job.business_id, timestamp, safe_error)
        return self._get_job(business_id=job.business_id, job_id=job.id)

    def _find_connection(
        self,
        *,
        business_id: str,
        provider: AdProvider,
        external_account_id: str,
    ) -> AdConnection | None:
        row = self._conn.execute(
            _CONNECTION_SELECT
            + " WHERE business_id=? AND provider=? AND external_account_id=? LIMIT 1",
            (business_id, provider.value, external_account_id),
        ).fetchone()
        return None if row is None else _connection_from_row(row)

    def _get_connection(self, *, business_id: str, connection_id: str) -> AdConnection:
        row = self._conn.execute(
            _CONNECTION_SELECT + " WHERE id=? AND business_id=? LIMIT 1",
            (connection_id, business_id),
        ).fetchone()
        if row is None:
            raise AdConnectionNotFound("advertising connection was not found")
        return _connection_from_row(row)

    def _get_job(self, *, business_id: str, job_id: str) -> AdPublicationJob:
        row = self._conn.execute(
            _JOB_SELECT + " WHERE id=? AND business_id=? LIMIT 1",
            (job_id, business_id),
        ).fetchone()
        if row is None:
            raise AdConnectionNotFound("advertising publication job was not found")
        return _job_from_row(row)

    def _mark_connection_success(self, connection_id: str, business_id: str, now: str) -> None:
        # A disconnect barrier sets status=disabled before an in-flight provider
        # lease is allowed to finish. Never resurrect that connection here.
        self._conn.execute(
            """
            UPDATE ad_connections SET status='active', last_success_at=?,
                last_error_at=NULL, last_error_code=NULL, updated_at=?
            WHERE id=? AND business_id=? AND status IN ('active', 'attention')
            """,
            (now, now, connection_id, business_id),
        )

    def _mark_connection_error(
        self,
        connection_id: str,
        business_id: str,
        now: str,
        error_code: str,
    ) -> None:
        # Likewise, a late worker failure must not turn a disabled/revoked
        # connection back into an available attention state.
        self._conn.execute(
            """
            UPDATE ad_connections SET status='attention', last_error_at=?,
                last_error_code=?, updated_at=?
            WHERE id=? AND business_id=? AND status IN ('active', 'attention')
            """,
            (now, error_code, now, connection_id, business_id),
        )

    def _audit(
        self,
        *,
        business_id: str,
        actor_member_id: str,
        action: str,
        subject_type: str,
        subject_id: str,
        details: dict[str, object],
        now: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO ad_audit_events(
                id, business_id, actor_member_id, action, subject_type,
                subject_id, details_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                business_id,
                actor_member_id,
                action,
                subject_type,
                subject_id,
                json.dumps(details, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )


def re_safe_error(value: object) -> str:
    normalized = "_".join(str(value or "provider_error").strip().lower().split())
    filtered = "".join(character for character in normalized if character.isalnum() or character in "_.-")
    return (filtered or "provider_error")[:120]


__all__ = ["AdConnectionRepository", "re_safe_error"]
