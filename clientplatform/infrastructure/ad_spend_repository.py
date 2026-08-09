from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import uuid4

from clientplatform.domain.ad_connections import AdConnectionStatus, AdPublicationStatus
from clientplatform.domain.ad_spend import (
    AdSpendAuthorization,
    AdSpendAuthorizationStatus,
    AdSpendConsentReceipt,
    AdSpendInvariantViolation,
    ProviderBudgetSnapshot,
)
from clientplatform.domain.tenancy import PlatformRole, TenantContext, normalize_uuid
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _optional(row: Any, key: str, position: int) -> str | None:
    value = _value(row, key, position)
    return None if value is None else str(value)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _snapshot_from_json(raw: object) -> ProviderBudgetSnapshot:
    try:
        payload = json.loads(str(raw))
    except json.JSONDecodeError as exc:
        raise AdSpendInvariantViolation("stored provider snapshot is invalid") from exc
    if not isinstance(payload, dict):
        raise AdSpendInvariantViolation("stored provider snapshot is invalid")
    try:
        return ProviderBudgetSnapshot(**payload)
    except (TypeError, ValueError, AdSpendInvariantViolation) as exc:
        raise AdSpendInvariantViolation("stored provider snapshot is invalid") from exc


def _receipt_from_row(row: Any) -> AdSpendConsentReceipt:
    return AdSpendConsentReceipt(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        authorization_id=str(_value(row, "authorization_id", 2)),
        actor_member_id=str(_value(row, "actor_member_id", 3)),
        actor_user_id=int(_value(row, "actor_user_id", 4)),
        terms_json=str(_value(row, "terms_json", 5)),
        terms_hash=str(_value(row, "terms_hash", 6)),
        snapshot_hash=str(_value(row, "snapshot_hash", 7)),
        consented_at=str(_value(row, "consented_at", 8)),
        receipt_hash=str(_value(row, "receipt_hash", 9)),
        version=str(_value(row, "version", 10)),
    )


_AUTHORIZATION_SELECT = """
    SELECT id, business_id, connection_id, publication_job_id,
           external_campaign_id, region_ids_json, currency,
           hard_cap_minor, daily_cap_minor, authorization_expires_at,
           stop_condition, snapshot_json, snapshot_hash, status,
           consent_receipt_id, created_by_member_id, created_at, updated_at,
           revoked_at, stopped_at, last_error_code, row_version
    FROM ad_spend_authorizations
"""
_RECEIPT_SELECT = """
    SELECT id, business_id, authorization_id, actor_member_id, actor_user_id,
           terms_json, terms_hash, snapshot_hash, consented_at, receipt_hash, version
    FROM ad_spend_consent_receipts
"""


class AdSpendRepository:
    """Tenant-safe persistence for immutable ad-spend consent and CAS transitions."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def _owner(self, actor: TenantContext) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        if current.role != PlatformRole.OWNER:
            raise AdSpendInvariantViolation(
                "advertising spend authorization requires business owner role"
            )
        return current

    def create_or_get_draft(
        self,
        *,
        actor: TenantContext,
        publication_job_id: str,
        snapshot: ProviderBudgetSnapshot,
        region_ids: tuple[int, ...],
        hard_cap_minor: int,
        daily_cap_minor: int,
        authorization_expires_at: datetime | str,
        now: datetime | str,
        authorization_id: str | None = None,
    ) -> AdSpendAuthorization:
        current = self._owner(actor)
        job_id = normalize_uuid(
            publication_job_id,
            field_name="publication_job_id",
        )
        self._assert_submitted_draft(
            business_id=current.business_id,
            publication_job_id=job_id,
            snapshot=snapshot,
        )
        authorization = AdSpendAuthorization.draft(
            authorization_id=authorization_id or str(uuid4()),
            business_id=current.business_id,
            publication_job_id=job_id,
            region_ids=region_ids,
            hard_cap_minor=hard_cap_minor,
            daily_cap_minor=daily_cap_minor,
            authorization_expires_at=authorization_expires_at,
            snapshot=snapshot,
            created_by_member_id=current.membership_id,
            now=now,
        )
        request_key = self._request_key(authorization)
        cursor = self._conn.execute(
            """
            INSERT INTO ad_spend_authorizations(
                id, business_id, connection_id, publication_job_id,
                external_campaign_id, region_ids_json, currency,
                hard_cap_minor, daily_cap_minor, authorization_expires_at,
                stop_condition, snapshot_json, snapshot_hash, status,
                request_key, consent_receipt_id, created_by_member_id,
                created_at, updated_at, revoked_at, stopped_at,
                last_error_code, row_version
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, NULL,
                     ?, ?, ?, NULL, NULL, NULL, 0)
            ON CONFLICT(business_id, request_key) DO NOTHING
            """,
            (
                authorization.id,
                authorization.business_id,
                authorization.connection_id,
                authorization.publication_job_id,
                authorization.external_campaign_id,
                _canonical_json(list(authorization.region_ids)),
                authorization.currency,
                authorization.hard_cap_minor,
                authorization.daily_cap_minor,
                authorization.authorization_expires_at,
                authorization.stop_condition.value,
                _canonical_json(authorization.snapshot.payload()),
                authorization.snapshot.snapshot_hash,
                request_key,
                authorization.created_by_member_id,
                authorization.created_at,
                authorization.updated_at,
            ),
        )
        created = int(getattr(cursor, "rowcount", 0) or 0) == 1
        stored, _ = self._get_by_request_key(
            business_id=current.business_id,
            request_key=request_key,
        )
        if created:
            self._audit(
                actor=current,
                action="ad_spend_authorization_created",
                authorization=stored,
                details={
                    "hard_cap_minor": stored.hard_cap_minor,
                    "daily_cap_minor": stored.daily_cap_minor,
                    "currency": stored.currency,
                    "snapshot_hash": stored.snapshot.snapshot_hash,
                },
            )
        return stored

    def get(
        self,
        *,
        actor: TenantContext,
        authorization_id: str,
    ) -> AdSpendAuthorization:
        current = self._owner(actor)
        authorization, _ = self._get_with_version(
            business_id=current.business_id,
            authorization_id=authorization_id,
        )
        return authorization

    def list_authorizations(
        self,
        *,
        actor: TenantContext,
        limit: int = 50,
    ) -> list[AdSpendAuthorization]:
        current = self._owner(actor)
        rows = self._conn.execute(
            _AUTHORIZATION_SELECT
            + " WHERE business_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
            (current.business_id, max(1, min(int(limit), 100))),
        ).fetchall()
        return [self._authorization_from_row(row) for row in rows]

    def request_consent(
        self,
        *,
        actor: TenantContext,
        authorization_id: str,
        now: datetime | str,
    ) -> AdSpendAuthorization:
        current = self._owner(actor)
        authorization, version = self._get_with_version(
            business_id=current.business_id,
            authorization_id=authorization_id,
        )
        if authorization.status in {
            AdSpendAuthorizationStatus.AWAITING_CONSENT,
            AdSpendAuthorizationStatus.AUTHORIZED,
        }:
            return authorization
        transitioned = authorization.request_consent(actor=current, now=now)
        cursor = self._conn.execute(
            """
            UPDATE ad_spend_authorizations
            SET status='awaiting_consent', updated_at=?, row_version=row_version+1
            WHERE id=? AND business_id=? AND status='draft' AND row_version=?
            """,
            (
                transitioned.updated_at,
                authorization.id,
                current.business_id,
                version,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            concurrent, _ = self._get_with_version(
                business_id=current.business_id,
                authorization_id=authorization.id,
            )
            if concurrent.status in {
                AdSpendAuthorizationStatus.AWAITING_CONSENT,
                AdSpendAuthorizationStatus.AUTHORIZED,
            }:
                return concurrent
            raise AdSpendInvariantViolation("consent request compare-and-set was lost")
        stored, _ = self._get_with_version(
            business_id=current.business_id,
            authorization_id=authorization.id,
        )
        self._audit(
            actor=current,
            action="ad_spend_consent_requested",
            authorization=stored,
            details={"terms_hash": stored.terms_hash},
        )
        return stored

    def authorize(
        self,
        *,
        actor: TenantContext,
        authorization_id: str,
        receipt_id: str,
        now: datetime | str,
    ) -> tuple[AdSpendAuthorization, AdSpendConsentReceipt]:
        current = self._owner(actor)
        authorization, version = self._get_with_version(
            business_id=current.business_id,
            authorization_id=authorization_id,
        )
        if (
            authorization.status == AdSpendAuthorizationStatus.AUTHORIZED
            and authorization.consent_receipt is not None
        ):
            return authorization, authorization.consent_receipt
        authorized, proposed = authorization.authorize(
            actor=current,
            receipt_id=receipt_id,
            now=now,
        )

        self._conn.execute("SAVEPOINT ad_spend_authorize")
        try:
            receipt_cursor = self._conn.execute(
                """
                INSERT INTO ad_spend_consent_receipts(
                    id, business_id, authorization_id, actor_member_id,
                    actor_user_id, terms_json, terms_hash, snapshot_hash,
                    consented_at, receipt_hash, version, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(business_id, authorization_id) DO NOTHING
                """,
                (
                    proposed.id,
                    proposed.business_id,
                    proposed.authorization_id,
                    proposed.actor_member_id,
                    proposed.actor_user_id,
                    proposed.terms_json,
                    proposed.terms_hash,
                    proposed.snapshot_hash,
                    proposed.consented_at,
                    proposed.receipt_hash,
                    proposed.version,
                    proposed.consented_at,
                ),
            )
            inserted_receipt = int(getattr(receipt_cursor, "rowcount", 0) or 0) == 1
            stored_receipt = self._get_receipt(
                business_id=current.business_id,
                authorization_id=authorization.id,
            )
            if not inserted_receipt:
                concurrent, _ = self._get_with_version(
                    business_id=current.business_id,
                    authorization_id=authorization.id,
                )
                if (
                    concurrent.status == AdSpendAuthorizationStatus.AUTHORIZED
                    and concurrent.consent_receipt is not None
                ):
                    self._conn.execute("RELEASE SAVEPOINT ad_spend_authorize")
                    return concurrent, concurrent.consent_receipt
                raise AdSpendInvariantViolation(
                    "consent receipt already exists without authorized state"
                )
            if stored_receipt.receipt_hash != proposed.receipt_hash:
                raise AdSpendInvariantViolation("stored consent receipt does not match")

            update_cursor = self._conn.execute(
                """
                UPDATE ad_spend_authorizations
                SET status='authorized', consent_receipt_id=?, updated_at=?,
                    row_version=row_version+1
                WHERE id=? AND business_id=? AND status='awaiting_consent'
                  AND row_version=? AND consent_receipt_id IS NULL
                """,
                (
                    stored_receipt.id,
                    authorized.updated_at,
                    authorization.id,
                    current.business_id,
                    version,
                ),
            )
            if int(getattr(update_cursor, "rowcount", 0) or 0) != 1:
                raise AdSpendInvariantViolation(
                    "spend authorization compare-and-set was lost"
                )
            stored, _ = self._get_with_version(
                business_id=current.business_id,
                authorization_id=authorization.id,
            )
            if stored.consent_receipt is None:
                raise AdSpendInvariantViolation("authorized state lost its consent receipt")
            self._audit(
                actor=current,
                action="ad_spend_consent_granted",
                authorization=stored,
                details={
                    "receipt_hash": stored_receipt.receipt_hash,
                    "terms_hash": stored_receipt.terms_hash,
                    "snapshot_hash": stored_receipt.snapshot_hash,
                },
            )
            self._conn.execute("RELEASE SAVEPOINT ad_spend_authorize")
            return stored, stored.consent_receipt
        except Exception:  # validator: allow-wide-except
            self._conn.execute("ROLLBACK TO SAVEPOINT ad_spend_authorize")
            self._conn.execute("RELEASE SAVEPOINT ad_spend_authorize")
            concurrent, _ = self._get_with_version(
                business_id=current.business_id,
                authorization_id=authorization.id,
            )
            if (
                concurrent.status == AdSpendAuthorizationStatus.AUTHORIZED
                and concurrent.consent_receipt is not None
            ):
                return concurrent, concurrent.consent_receipt
            raise

    def revoke(
        self,
        *,
        actor: TenantContext,
        authorization_id: str,
        now: datetime | str,
    ) -> AdSpendAuthorization:
        current = self._owner(actor)
        authorization, version = self._get_with_version(
            business_id=current.business_id,
            authorization_id=authorization_id,
        )
        if authorization.status == AdSpendAuthorizationStatus.REVOKED:
            return authorization
        revoked = authorization.revoke(actor=current, now=now)
        cursor = self._conn.execute(
            """
            UPDATE ad_spend_authorizations
            SET status='revoked', revoked_at=?, updated_at=?, row_version=row_version+1
            WHERE id=? AND business_id=? AND status=? AND row_version=?
            """,
            (
                revoked.revoked_at,
                revoked.updated_at,
                authorization.id,
                current.business_id,
                authorization.status.value,
                version,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            concurrent, _ = self._get_with_version(
                business_id=current.business_id,
                authorization_id=authorization.id,
            )
            if concurrent.status == AdSpendAuthorizationStatus.REVOKED:
                return concurrent
            raise AdSpendInvariantViolation("revocation compare-and-set was lost")
        stored, _ = self._get_with_version(
            business_id=current.business_id,
            authorization_id=authorization.id,
        )
        self._audit(
            actor=current,
            action="ad_spend_authorization_revoked",
            authorization=stored,
            details={},
        )
        return stored

    def _assert_submitted_draft(
        self,
        *,
        business_id: str,
        publication_job_id: str,
        snapshot: ProviderBudgetSnapshot,
    ) -> None:
        row = self._conn.execute(
            """
            SELECT j.connection_id,
                   j.external_campaign_id,
                   j.status AS job_status,
                   c.status AS connection_status,
                   c.provider AS connection_provider,
                   c.external_account_id AS connection_external_account_id
            FROM ad_publication_jobs AS j
            JOIN ad_connections AS c
              ON c.id=j.connection_id AND c.business_id=j.business_id
            WHERE j.id=? AND j.business_id=?
            LIMIT 1
            """,
            (publication_job_id, business_id),
        ).fetchone()
        if row is None:
            raise AdSpendInvariantViolation(
                "submitted advertising draft was not found for business"
            )
        connection_id = str(_value(row, "connection_id", 0))
        campaign_id = str(_value(row, "external_campaign_id", 1))
        job_status = AdPublicationStatus(str(_value(row, "job_status", 2)))
        connection_status = AdConnectionStatus(
            str(_value(row, "connection_status", 3))
        )
        connection_provider = str(_value(row, "connection_provider", 4))
        connection_external_account_id = str(
            _value(row, "connection_external_account_id", 5)
        )
        if job_status != AdPublicationStatus.SUBMITTED:
            raise AdSpendInvariantViolation(
                "advertising spend requires a provider-created DRAFT"
            )
        if connection_status != AdConnectionStatus.ACTIVE:
            raise AdSpendInvariantViolation("advertising connection is not active")
        if snapshot.connection_id != connection_id:
            raise AdSpendInvariantViolation("provider snapshot connection does not match job")
        if snapshot.provider.value != connection_provider:
            raise AdSpendInvariantViolation(
                "provider snapshot provider does not match connection"
            )
        if snapshot.external_account_id != connection_external_account_id:
            raise AdSpendInvariantViolation(
                "provider snapshot account does not match connection"
            )
        if snapshot.external_campaign_id != campaign_id:
            raise AdSpendInvariantViolation("provider snapshot campaign does not match job")

    def _request_key(self, authorization: AdSpendAuthorization) -> str:
        payload = {
            "business_id": authorization.business_id,
            "connection_id": authorization.connection_id,
            "publication_job_id": authorization.publication_job_id,
            "external_campaign_id": authorization.external_campaign_id,
            "region_ids": list(authorization.region_ids),
            "currency": authorization.currency,
            "hard_cap_minor": authorization.hard_cap_minor,
            "daily_cap_minor": authorization.daily_cap_minor,
            "authorization_expires_at": authorization.authorization_expires_at,
            "stop_condition": authorization.stop_condition.value,
            "snapshot_hash": authorization.snapshot.snapshot_hash,
        }
        digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
        return "adspend_" + digest

    def _get_by_request_key(
        self,
        *,
        business_id: str,
        request_key: str,
    ) -> tuple[AdSpendAuthorization, int]:
        row = self._conn.execute(
            _AUTHORIZATION_SELECT
            + " WHERE business_id=? AND request_key=? LIMIT 1",
            (business_id, request_key),
        ).fetchone()
        if row is None:
            raise AdSpendInvariantViolation("ad spend authorization was not persisted")
        return self._authorization_from_row(row), int(_value(row, "row_version", 21))

    def _get_with_version(
        self,
        *,
        business_id: str,
        authorization_id: str,
    ) -> tuple[AdSpendAuthorization, int]:
        normalized = normalize_uuid(
            authorization_id,
            field_name="ad_spend_authorization_id",
        )
        row = self._conn.execute(
            _AUTHORIZATION_SELECT + " WHERE id=? AND business_id=? LIMIT 1",
            (normalized, business_id),
        ).fetchone()
        if row is None:
            raise AdSpendInvariantViolation(
                "ad spend authorization was not found for business"
            )
        return self._authorization_from_row(row), int(_value(row, "row_version", 21))

    def _authorization_from_row(self, row: Any) -> AdSpendAuthorization:
        receipt_id = _optional(row, "consent_receipt_id", 14)
        receipt = None
        if receipt_id is not None:
            receipt = self._get_receipt(
                business_id=str(_value(row, "business_id", 1)),
                authorization_id=str(_value(row, "id", 0)),
            )
        snapshot = _snapshot_from_json(_value(row, "snapshot_json", 11))
        stored_snapshot_hash = str(_value(row, "snapshot_hash", 12))
        if snapshot.snapshot_hash != stored_snapshot_hash:
            raise AdSpendInvariantViolation("stored provider snapshot hash does not match")
        try:
            regions = tuple(json.loads(str(_value(row, "region_ids_json", 5))))
        except (json.JSONDecodeError, TypeError) as exc:
            raise AdSpendInvariantViolation("stored advertising regions are invalid") from exc
        return AdSpendAuthorization(
            id=str(_value(row, "id", 0)),
            business_id=str(_value(row, "business_id", 1)),
            connection_id=str(_value(row, "connection_id", 2)),
            publication_job_id=str(_value(row, "publication_job_id", 3)),
            external_campaign_id=str(_value(row, "external_campaign_id", 4)),
            region_ids=regions,
            currency=str(_value(row, "currency", 6)),
            hard_cap_minor=int(_value(row, "hard_cap_minor", 7)),
            daily_cap_minor=int(_value(row, "daily_cap_minor", 8)),
            authorization_expires_at=str(
                _value(row, "authorization_expires_at", 9)
            ),
            stop_condition=str(_value(row, "stop_condition", 10)),
            snapshot=snapshot,
            status=str(_value(row, "status", 13)),
            consent_receipt=receipt,
            created_by_member_id=str(_value(row, "created_by_member_id", 15)),
            created_at=str(_value(row, "created_at", 16)),
            updated_at=str(_value(row, "updated_at", 17)),
            revoked_at=_optional(row, "revoked_at", 18),
            stopped_at=_optional(row, "stopped_at", 19),
            last_error_code=_optional(row, "last_error_code", 20),
        )

    def _get_receipt(
        self,
        *,
        business_id: str,
        authorization_id: str,
    ) -> AdSpendConsentReceipt:
        row = self._conn.execute(
            _RECEIPT_SELECT
            + " WHERE business_id=? AND authorization_id=? LIMIT 1",
            (business_id, authorization_id),
        ).fetchone()
        if row is None:
            raise AdSpendInvariantViolation("consent receipt was not found")
        return _receipt_from_row(row)

    def _audit(
        self,
        *,
        actor: TenantContext,
        action: str,
        authorization: AdSpendAuthorization,
        details: dict[str, object],
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO ad_audit_events(
                id, business_id, actor_member_id, action,
                subject_type, subject_id, details_json, created_at
            ) VALUES(?, ?, ?, ?, 'ad_spend_authorization', ?, ?, ?)
            """,
            (
                str(uuid4()),
                authorization.business_id,
                actor.membership_id,
                action,
                authorization.id,
                _canonical_json(details),
                authorization.updated_at,
            ),
        )


__all__ = ["AdSpendRepository"]
