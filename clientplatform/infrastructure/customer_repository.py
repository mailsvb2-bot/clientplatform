from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.customers import (
    Customer,
    CustomerIdentity,
    CustomerIdentityConflict,
    CustomerIdentityStatus,
    CustomerNotFound,
    CustomerPlatform,
    CustomerRecord,
    CustomerStatus,
    normalize_identity_subject,
    normalize_optional_handle,
    normalize_optional_person_name,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure import TenancyRepository


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _customer_from_row(row: Any) -> Customer:
    archived_at = _value(row, "archived_at", 7)
    return Customer(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        display_name=_value(row, "display_name", 2),
        status=CustomerStatus(str(_value(row, "status", 3))),
        created_by_member_id=str(_value(row, "created_by_member_id", 4)),
        created_at=str(_value(row, "created_at", 5)),
        updated_at=str(_value(row, "updated_at", 6)),
        archived_at=None if archived_at is None else str(archived_at),
    )


def _identity_from_row(row: Any) -> CustomerIdentity:
    revoked_at = _value(row, "revoked_at", 10)
    return CustomerIdentity(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        customer_id=str(_value(row, "customer_id", 2)),
        platform=CustomerPlatform(str(_value(row, "platform", 3))),
        external_subject=str(_value(row, "external_subject", 4)),
        username=_value(row, "username", 5),
        display_name=_value(row, "display_name", 6),
        status=CustomerIdentityStatus(str(_value(row, "status", 7))),
        created_at=str(_value(row, "created_at", 8)),
        updated_at=str(_value(row, "updated_at", 9)),
        revoked_at=None if revoked_at is None else str(revoked_at),
    )


class CustomerRepository:
    """Customer records that can only be accessed through a live TenantContext."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def _resolve_actor(
        self,
        actor: TenantContext,
        *,
        manage: bool,
    ) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        if manage:
            current.assert_can_manage_customer_records()
        else:
            current.assert_can_view_customer_records()
        return current

    def create_customer(
        self,
        *,
        actor: TenantContext,
        display_name: str | None = None,
        now: str | None = None,
    ) -> Customer:
        current = self._resolve_actor(actor, manage=True)
        customer_id = str(uuid4())
        normalized_name = normalize_optional_person_name(
            display_name,
            field_name="display_name",
        )
        timestamp = str(now or _utc_now())
        self._conn.execute(
            """
            INSERT INTO customers(
                id, business_id, display_name, status, created_by_member_id,
                created_at, updated_at, archived_at
            ) VALUES(?, ?, ?, 'active', ?, ?, ?, NULL)
            """,
            (
                customer_id,
                current.business_id,
                normalized_name,
                current.membership_id,
                timestamp,
                timestamp,
            ),
        )
        return self.get_customer(actor=current, customer_id=customer_id).customer

    def get_customer(
        self,
        *,
        actor: TenantContext,
        customer_id: str,
    ) -> CustomerRecord:
        current = self._resolve_actor(actor, manage=False)
        normalized_customer_id = normalize_uuid(
            customer_id,
            field_name="customer_id",
        )
        customer_row = self._conn.execute(
            """
            SELECT id, business_id, display_name, status, created_by_member_id,
                   created_at, updated_at, archived_at
            FROM customers
            WHERE id=? AND business_id=?
            LIMIT 1
            """,
            (normalized_customer_id, current.business_id),
        ).fetchone()
        if customer_row is None:
            raise CustomerNotFound("customer was not found in the active business")
        identity_rows = self._conn.execute(
            """
            SELECT id, business_id, customer_id, platform, external_subject,
                   username, display_name, status, created_at, updated_at, revoked_at
            FROM customer_identities
            WHERE business_id=? AND customer_id=?
            ORDER BY created_at, id
            """,
            (current.business_id, normalized_customer_id),
        ).fetchall()
        return CustomerRecord(
            customer=_customer_from_row(customer_row),
            identities=tuple(_identity_from_row(row) for row in identity_rows),
        )

    def list_customers(
        self,
        *,
        actor: TenantContext,
        include_archived: bool = False,
    ) -> list[Customer]:
        current = self._resolve_actor(actor, manage=False)
        if include_archived:
            rows = self._conn.execute(
                """
                SELECT id, business_id, display_name, status, created_by_member_id,
                       created_at, updated_at, archived_at
                FROM customers
                WHERE business_id=?
                ORDER BY created_at, id
                """,
                (current.business_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT id, business_id, display_name, status, created_by_member_id,
                       created_at, updated_at, archived_at
                FROM customers
                WHERE business_id=? AND status='active'
                ORDER BY created_at, id
                """,
                (current.business_id,),
            ).fetchall()
        return [_customer_from_row(row) for row in rows]

    def list_customers_with_active_identity(
        self,
        *,
        actor: TenantContext,
        platform: CustomerPlatform | str,
        limit: int = 100,
    ) -> list[Customer]:
        current = self._resolve_actor(actor, manage=False)
        normalized_platform = (
            platform
            if isinstance(platform, CustomerPlatform)
            else CustomerPlatform(str(platform).strip().lower())
        )
        normalized_limit = max(1, min(int(limit), 100))
        rows = self._conn.execute(
            """
            SELECT DISTINCT c.id, c.business_id, c.display_name, c.status,
                   c.created_by_member_id, c.created_at, c.updated_at, c.archived_at
            FROM customers c
            JOIN customer_identities ci
              ON ci.customer_id=c.id AND ci.business_id=c.business_id
             AND ci.status='active' AND ci.platform=?
            WHERE c.business_id=? AND c.status='active'
            ORDER BY c.created_at, c.id
            LIMIT ?
            """,
            (normalized_platform.value, current.business_id, normalized_limit),
        ).fetchall()
        return [_customer_from_row(row) for row in rows]

    def attach_identity(
        self,
        *,
        actor: TenantContext,
        customer_id: str,
        platform: CustomerPlatform | str,
        external_subject: str,
        username: str | None = None,
        display_name: str | None = None,
        now: str | None = None,
    ) -> CustomerIdentity:
        current = self._resolve_actor(actor, manage=True)
        normalized_customer_id = normalize_uuid(
            customer_id,
            field_name="customer_id",
        )
        normalized_platform, normalized_subject = normalize_identity_subject(
            platform,
            external_subject,
        )
        normalized_username = normalize_optional_handle(username)
        normalized_name = normalize_optional_person_name(
            display_name,
            field_name="identity_display_name",
        )
        timestamp = str(now or _utc_now())
        customer_row = self._conn.execute(
            """
            SELECT id
            FROM customers
            WHERE id=? AND business_id=? AND status='active'
            LIMIT 1
            """,
            (normalized_customer_id, current.business_id),
        ).fetchone()
        if customer_row is None:
            raise CustomerNotFound("active customer was not found in the business")

        existing = self._find_identity_row(
            business_id=current.business_id,
            platform=normalized_platform,
            external_subject=normalized_subject,
        )
        if existing is not None:
            existing_customer_id = str(_value(existing, "customer_id", 2))
            if existing_customer_id != normalized_customer_id:
                raise CustomerIdentityConflict(
                    "identity already belongs to another customer in this business"
                )
            identity_id = str(_value(existing, "id", 0))
            self._conn.execute(
                """
                UPDATE customer_identities
                SET username=?, display_name=?, status='active',
                    updated_at=?, revoked_at=NULL
                WHERE id=? AND business_id=? AND customer_id=?
                """,
                (
                    normalized_username,
                    normalized_name,
                    timestamp,
                    identity_id,
                    current.business_id,
                    normalized_customer_id,
                ),
            )
            return self._get_identity(
                business_id=current.business_id,
                identity_id=identity_id,
            )

        identity_id = str(uuid4())
        try:
            self._conn.execute(
                """
                INSERT INTO customer_identities(
                    id, business_id, customer_id, platform, external_subject,
                    username, display_name, status, created_at, updated_at, revoked_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL)
                """,
                (
                    identity_id,
                    current.business_id,
                    normalized_customer_id,
                    normalized_platform.value,
                    normalized_subject,
                    normalized_username,
                    normalized_name,
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            concurrent = self._find_identity_row(
                business_id=current.business_id,
                platform=normalized_platform,
                external_subject=normalized_subject,
            )
            if concurrent is None:
                raise
            if str(_value(concurrent, "customer_id", 2)) != normalized_customer_id:
                raise CustomerIdentityConflict(
                    "identity concurrently attached to another customer"
                ) from exc
            return _identity_from_row(concurrent)
        return self._get_identity(
            business_id=current.business_id,
            identity_id=identity_id,
        )

    def find_by_identity(
        self,
        *,
        actor: TenantContext,
        platform: CustomerPlatform | str,
        external_subject: str,
    ) -> CustomerRecord:
        current = self._resolve_actor(actor, manage=False)
        normalized_platform, normalized_subject = normalize_identity_subject(
            platform,
            external_subject,
        )
        identity_row = self._find_identity_row(
            business_id=current.business_id,
            platform=normalized_platform,
            external_subject=normalized_subject,
            active_only=True,
        )
        if identity_row is None:
            raise CustomerNotFound("active customer identity was not found")
        return self.get_customer(
            actor=current,
            customer_id=str(_value(identity_row, "customer_id", 2)),
        )

    def archive_customer(
        self,
        *,
        actor: TenantContext,
        customer_id: str,
        now: str | None = None,
    ) -> Customer:
        current = self._resolve_actor(actor, manage=True)
        normalized_customer_id = normalize_uuid(
            customer_id,
            field_name="customer_id",
        )
        timestamp = str(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE customers
            SET status='archived', updated_at=?, archived_at=?
            WHERE id=? AND business_id=? AND status='active'
            """,
            (
                timestamp,
                timestamp,
                normalized_customer_id,
                current.business_id,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise CustomerNotFound("active customer was not found in the business")
        self._conn.execute(
            """
            UPDATE customer_identities
            SET status='revoked', updated_at=?, revoked_at=?
            WHERE business_id=? AND customer_id=? AND status='active'
            """,
            (
                timestamp,
                timestamp,
                current.business_id,
                normalized_customer_id,
            ),
        )
        return self.get_customer(
            actor=current,
            customer_id=normalized_customer_id,
        ).customer

    def _find_identity_row(
        self,
        *,
        business_id: str,
        platform: CustomerPlatform,
        external_subject: str,
        active_only: bool = False,
    ) -> Any | None:
        status_clause = " AND status='active'" if active_only else ""
        return self._conn.execute(
            """
            SELECT id, business_id, customer_id, platform, external_subject,
                   username, display_name, status, created_at, updated_at, revoked_at
            FROM customer_identities
            WHERE business_id=? AND platform=? AND external_subject=?
            """
            + status_clause
            + " LIMIT 1",
            (business_id, platform.value, external_subject),
        ).fetchone()

    def _get_identity(
        self,
        *,
        business_id: str,
        identity_id: str,
    ) -> CustomerIdentity:
        normalized_identity_id = normalize_uuid(
            identity_id,
            field_name="identity_id",
        )
        row = self._conn.execute(
            """
            SELECT id, business_id, customer_id, platform, external_subject,
                   username, display_name, status, created_at, updated_at, revoked_at
            FROM customer_identities
            WHERE id=? AND business_id=?
            LIMIT 1
            """,
            (normalized_identity_id, business_id),
        ).fetchone()
        if row is None:
            raise CustomerNotFound("customer identity was not found in the business")
        return _identity_from_row(row)
