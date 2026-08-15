from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.connections import (
    ConnectionPlatform,
    ConnectionStatus,
    normalize_credential_reference,
    normalize_external_account_id,
)
from clientplatform.domain.customers import (
    CustomerIdentity,
    CustomerIdentityStatus,
    CustomerPlatform,
    normalize_identity_subject,
    normalize_optional_handle,
    normalize_optional_person_name,
)
from clientplatform.domain.messenger_channels import (
    CustomerChannelIdentityConflict,
    CustomerChannelLinkRejected,
    CustomerIngressContext,
    IssuedCustomerLink,
    MessengerIngressRoute,
    MessengerRouteNotFound,
    normalize_customer_link_token,
    normalize_customer_platform,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


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


def _route_from_row(row: Any) -> MessengerIngressRoute:
    revoked_at = _value(row, "revoked_at", 10)
    return MessengerIngressRoute(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        connection_id=str(_value(row, "connection_id", 2)),
        platform=ConnectionPlatform(str(_value(row, "platform", 3))),
        external_route_id=str(_value(row, "external_route_id", 4)),
        webhook_secret_reference=str(_value(row, "webhook_secret_reference", 5)),
        status=str(_value(row, "status", 6)),
        created_by_member_id=str(_value(row, "created_by_member_id", 7)),
        created_at=str(_value(row, "created_at", 8)),
        updated_at=str(_value(row, "updated_at", 9)),
        revoked_at=None if revoked_at is None else str(revoked_at),
    )


class MessengerChannelRepository:
    """Canonical business-scoped VK/MAX routes and shared customer identities."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def _resolve_manager(self, actor: TenantContext) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_business()
        return current

    def register_route(
        self,
        *,
        actor: TenantContext,
        connection_id: str,
        external_route_id: str,
        webhook_secret_reference: str,
        now: datetime | None = None,
    ) -> MessengerIngressRoute:
        current = self._resolve_manager(actor)
        normalized_connection_id = normalize_uuid(connection_id, field_name="connection_id")
        route_id = normalize_external_account_id(external_route_id)
        secret_ref = normalize_credential_reference(webhook_secret_reference)
        connection = self._conn.execute(
            """
            SELECT platform, external_account_id, status
            FROM connections
            WHERE id=? AND business_id=?
            LIMIT 1
            """,
            (normalized_connection_id, current.business_id),
        ).fetchone()
        if connection is None:
            raise MessengerRouteNotFound("connection was not found in the active business")
        platform = ConnectionPlatform(str(_value(connection, "platform", 0)))
        if platform not in {ConnectionPlatform.VK, ConnectionPlatform.MAX}:
            raise CustomerChannelLinkRejected("messenger ingress route requires VK or MAX connection")
        if ConnectionStatus(str(_value(connection, "status", 2))) != ConnectionStatus.ACTIVE:
            raise CustomerChannelLinkRejected("messenger ingress connection must be active")
        expected_route = str(_value(connection, "external_account_id", 1)).strip()
        if expected_route != route_id:
            raise CustomerChannelLinkRejected("provider route does not match the canonical connection")
        timestamp = _iso(now or _utc_now())
        existing = self._conn.execute(
            """
            SELECT id, business_id, connection_id, platform, external_route_id,
                   webhook_secret_reference, status, created_by_member_id,
                   created_at, updated_at, revoked_at
            FROM messenger_ingress_routes
            WHERE business_id=? AND connection_id=?
            LIMIT 1
            """,
            (current.business_id, normalized_connection_id),
        ).fetchone()
        if existing is not None:
            route = _route_from_row(existing)
            if (
                route.platform != platform
                or route.external_route_id != route_id
                or route.webhook_secret_reference != secret_ref
            ):
                raise CustomerChannelLinkRejected("existing messenger route has different immutable binding")
            return route
        route_uuid = str(uuid4())
        self._conn.execute(
            """
            INSERT INTO messenger_ingress_routes(
                id, business_id, connection_id, platform, external_route_id,
                webhook_secret_reference, status, created_by_member_id,
                created_at, updated_at, revoked_at
            ) VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, NULL)
            """,
            (
                route_uuid,
                current.business_id,
                normalized_connection_id,
                platform.value,
                route_id,
                secret_ref,
                current.membership_id,
                timestamp,
                timestamp,
            ),
        )
        return self.resolve_route(route_id=route_uuid, expected_platform=platform)

    def resolve_route(
        self,
        *,
        route_id: str,
        expected_platform: ConnectionPlatform | str,
    ) -> MessengerIngressRoute:
        normalized_route_id = normalize_uuid(route_id, field_name="messenger_route_id")
        platform = ConnectionPlatform(str(expected_platform).strip().lower())
        row = self._conn.execute(
            """
            SELECT r.id, r.business_id, r.connection_id, r.platform,
                   r.external_route_id, r.webhook_secret_reference, r.status,
                   r.created_by_member_id, r.created_at, r.updated_at, r.revoked_at
            FROM messenger_ingress_routes r
            JOIN connections c
              ON c.id=r.connection_id AND c.business_id=r.business_id
             AND c.platform=r.platform AND c.status='active'
            JOIN businesses b
              ON b.id=r.business_id AND b.status='active'
            WHERE r.id=? AND r.platform=? AND r.status='active'
            LIMIT 1
            """,
            (normalized_route_id, platform.value),
        ).fetchone()
        if row is None:
            raise MessengerRouteNotFound("active messenger route was not found")
        return _route_from_row(row)

    def ensure_customer_identity(
        self,
        *,
        context: CustomerIngressContext,
        external_subject: str,
        username: str | None = None,
        display_name: str | None = None,
        now: datetime | None = None,
    ) -> CustomerIdentity:
        platform, subject = normalize_identity_subject(context.platform, external_subject)
        if platform not in {CustomerPlatform.TELEGRAM, CustomerPlatform.VK, CustomerPlatform.MAX}:
            raise ValueError("messenger ingress requires Telegram, VK or MAX identity")
        existing = self._find_identity(
            business_id=context.business_id,
            platform=platform,
            external_subject=subject,
        )
        if existing is not None:
            return existing
        owner = self._conn.execute(
            """
            SELECT id
            FROM business_members
            WHERE business_id=? AND role='owner' AND status='active'
            ORDER BY created_at, id
            LIMIT 1
            """,
            (context.business_id,),
        ).fetchone()
        if owner is None:
            raise CustomerChannelLinkRejected("business has no active owner")
        timestamp = _iso(now or _utc_now())
        customer_id = str(uuid4())
        identity_id = str(uuid4())
        normalized_name = normalize_optional_person_name(display_name, field_name="display_name")
        normalized_username = normalize_optional_handle(username)
        self._conn.execute(
            """
            INSERT INTO customers(
                id, business_id, display_name, status, created_by_member_id,
                created_at, updated_at, archived_at
            ) VALUES(?, ?, ?, 'active', ?, ?, ?, NULL)
            """,
            (
                customer_id,
                context.business_id,
                normalized_name,
                str(_value(owner, "id", 0)),
                timestamp,
                timestamp,
            ),
        )
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
                    context.business_id,
                    customer_id,
                    platform.value,
                    subject,
                    normalized_username,
                    normalized_name,
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError:
            self._conn.execute(
                "DELETE FROM customers WHERE id=? AND business_id=?",
                (customer_id, context.business_id),
            )
            concurrent = self._find_identity(
                business_id=context.business_id,
                platform=platform,
                external_subject=subject,
            )
            if concurrent is None:
                raise
            return concurrent
        result = self._find_identity(
            business_id=context.business_id,
            platform=platform,
            external_subject=subject,
        )
        if result is None:
            raise CustomerChannelLinkRejected("customer identity disappeared after creation")
        return result

    def issue_customer_link(
        self,
        *,
        actor: TenantContext,
        customer_id: str,
        target_platform: CustomerPlatform | str | None = None,
        ttl_seconds: int = 900,
        now: datetime | None = None,
    ) -> IssuedCustomerLink:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_customer_records()
        normalized_customer_id = normalize_uuid(customer_id, field_name="customer_id")
        customer = self._conn.execute(
            "SELECT 1 FROM customers WHERE id=? AND business_id=? AND status='active'",
            (normalized_customer_id, current.business_id),
        ).fetchone()
        if customer is None:
            raise CustomerChannelLinkRejected("active customer was not found in the business")
        target = None if target_platform is None else normalize_customer_platform(target_platform)
        if target is not None and target not in {
            CustomerPlatform.TELEGRAM,
            CustomerPlatform.VK,
            CustomerPlatform.MAX,
        }:
            raise ValueError("customer channel link supports only Telegram, VK or MAX")
        ttl = min(max(int(ttl_seconds), 60), 3600)
        issued_at = now or _utc_now()
        expires_at = issued_at + timedelta(seconds=ttl)
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        self._conn.execute(
            """
            INSERT INTO customer_channel_link_tokens(
                id, business_id, customer_id, token_digest, target_platform,
                created_by_member_id, created_at, expires_at,
                consumed_at, consumed_platform, consumed_external_subject
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
            """,
            (
                str(uuid4()),
                current.business_id,
                normalized_customer_id,
                digest,
                None if target is None else target.value,
                current.membership_id,
                _iso(issued_at),
                _iso(expires_at),
            ),
        )
        return IssuedCustomerLink(
            token=token,
            business_id=current.business_id,
            customer_id=normalized_customer_id,
            target_platform=target,
            expires_at=_iso(expires_at),
        )

    def consume_customer_link(
        self,
        *,
        context: CustomerIngressContext,
        token: str,
        external_subject: str,
        username: str | None = None,
        display_name: str | None = None,
        now: datetime | None = None,
    ) -> CustomerIdentity:
        raw = normalize_customer_link_token(token)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        platform, subject = normalize_identity_subject(context.platform, external_subject)
        timestamp_dt = now or _utc_now()
        timestamp = _iso(timestamp_dt)
        row = self._conn.execute(
            """
            SELECT customer_id, target_platform, expires_at, consumed_at
            FROM customer_channel_link_tokens
            WHERE token_digest=? AND business_id=?
            LIMIT 1
            """,
            (digest, context.business_id),
        ).fetchone()
        if row is None:
            raise CustomerChannelLinkRejected("customer link token was not found for this business")
        consumed_at = _value(row, "consumed_at", 3)
        if consumed_at is not None:
            raise CustomerChannelLinkRejected("customer link token was already consumed")
        try:
            expires_at = datetime.fromisoformat(str(_value(row, "expires_at", 2)).replace("Z", "+00:00"))
        except ValueError as exc:
            raise CustomerChannelLinkRejected("customer link token expiry is invalid") from exc
        if expires_at <= timestamp_dt:
            raise CustomerChannelLinkRejected("customer link token expired")
        target_platform = _value(row, "target_platform", 1)
        if target_platform is not None and str(target_platform) != platform.value:
            raise CustomerChannelLinkRejected("customer link token belongs to another platform")
        customer_id = str(_value(row, "customer_id", 0))
        existing = self._find_identity(
            business_id=context.business_id,
            platform=platform,
            external_subject=subject,
        )
        if existing is not None and existing.customer_id != customer_id:
            raise CustomerChannelIdentityConflict("external identity belongs to another customer")
        cursor = self._conn.execute(
            """
            UPDATE customer_channel_link_tokens
            SET consumed_at=?, consumed_platform=?, consumed_external_subject=?
            WHERE token_digest=? AND business_id=? AND consumed_at IS NULL
            """,
            (timestamp, platform.value, subject, digest, context.business_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise CustomerChannelLinkRejected("customer link token lost a concurrent consume race")
        if existing is not None:
            return existing
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
                    context.business_id,
                    customer_id,
                    platform.value,
                    subject,
                    normalize_optional_handle(username),
                    normalize_optional_person_name(display_name, field_name="display_name"),
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            concurrent = self._find_identity(
                business_id=context.business_id,
                platform=platform,
                external_subject=subject,
            )
            if concurrent is None or concurrent.customer_id != customer_id:
                raise CustomerChannelIdentityConflict("external identity was claimed concurrently") from exc
            return concurrent
        result = self._find_identity(
            business_id=context.business_id,
            platform=platform,
            external_subject=subject,
        )
        if result is None:
            raise CustomerChannelLinkRejected("linked customer identity disappeared")
        return result

    def _find_identity(
        self,
        *,
        business_id: str,
        platform: CustomerPlatform,
        external_subject: str,
    ) -> CustomerIdentity | None:
        row = self._conn.execute(
            """
            SELECT id, business_id, customer_id, platform, external_subject,
                   username, display_name, status, created_at, updated_at, revoked_at
            FROM customer_identities
            WHERE business_id=? AND platform=? AND external_subject=? AND status='active'
            LIMIT 1
            """,
            (business_id, platform.value, external_subject),
        ).fetchone()
        return None if row is None else _identity_from_row(row)
