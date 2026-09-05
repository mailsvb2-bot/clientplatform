from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from clientplatform.domain.activity import (
    ActivityInvariantViolation,
    ActivityNotFound,
    BusinessCapability,
    BusinessOffering,
    BusinessProfile,
    BusinessProfileStatus,
    CapabilityKind,
    CapabilityStatus,
    CustomerInvite,
    InviteClaim,
    InviteStatus,
    IssuedCustomerInvite,
    OfferingStatus,
    invite_token_hash,
    new_invite_token,
    normalize_activity_description,
    normalize_capability_title,
    normalize_known_timezone,
    normalize_offering_description,
    normalize_offering_title,
    resolve_activity_connector,
)
from clientplatform.domain.customers import (
    CustomerNotFound,
    CustomerPlatform,
    normalize_identity_subject,
)
from clientplatform.domain.tenancy import TenantContext, normalize_user_id, normalize_uuid
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _profile_from_row(row: Any) -> BusinessProfile:
    return BusinessProfile(
        business_id=str(_value(row, "business_id", 0)),
        activity_description=str(_value(row, "activity_description", 1)),
        timezone=str(_value(row, "timezone", 2)),
        status=BusinessProfileStatus(str(_value(row, "status", 3))),
        created_by_member_id=str(_value(row, "created_by_member_id", 4)),
        created_at=str(_value(row, "created_at", 5)),
        updated_at=str(_value(row, "updated_at", 6)),
    )


def _capability_from_row(row: Any) -> BusinessCapability:
    return BusinessCapability(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        connector_key=str(_value(row, "connector_key", 2)),
        kind=CapabilityKind(str(_value(row, "kind", 3))),
        title=str(_value(row, "title", 4)),
        status=CapabilityStatus(str(_value(row, "status", 5))),
        created_by_member_id=str(_value(row, "created_by_member_id", 6)),
        created_at=str(_value(row, "created_at", 7)),
        updated_at=str(_value(row, "updated_at", 8)),
    )


def _offering_from_row(row: Any) -> BusinessOffering:
    return BusinessOffering(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        capability_id=str(_value(row, "capability_id", 2)),
        title=str(_value(row, "title", 3)),
        description=str(_value(row, "description", 4)),
        status=OfferingStatus(str(_value(row, "status", 5))),
        created_by_member_id=str(_value(row, "created_by_member_id", 6)),
        created_at=str(_value(row, "created_at", 7)),
        updated_at=str(_value(row, "updated_at", 8)),
    )


def _invite_from_row(row: Any) -> CustomerInvite:
    claimed_customer_id = _value(row, "claimed_customer_id", 6)
    claimed_at = _value(row, "claimed_at", 8)
    revoked_at = _value(row, "revoked_at", 9)
    return CustomerInvite(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        status=InviteStatus(str(_value(row, "status", 2))),
        expires_at=str(_value(row, "expires_at", 3)),
        created_by_member_id=str(_value(row, "created_by_member_id", 4)),
        created_at=str(_value(row, "created_at", 5)),
        claimed_customer_id=None if claimed_customer_id is None else str(claimed_customer_id),
        claimed_at=None if claimed_at is None else str(claimed_at),
        revoked_at=None if revoked_at is None else str(revoked_at),
    )


class ActivityRepository:
    """Tenant-scoped business profile and extensible activity connector repository."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def _current_actor(self, actor: TenantContext) -> TenantContext:
        return self._tenancy.resolve_context(user_id=actor.user_id, business_id=actor.business_id)

    def upsert_profile(
        self,
        *,
        actor: TenantContext,
        activity_description: str,
        timezone_name: str,
        now: str | None = None,
    ) -> BusinessProfile:
        current = self._current_actor(actor)
        current.assert_can_manage_business()
        description = normalize_activity_description(activity_description)
        timezone_value = normalize_known_timezone(timezone_name)
        timestamp = str(now or _utc_now())
        existing = self._conn.execute(
            "SELECT business_id FROM business_profiles WHERE business_id=?",
            (current.business_id,),
        ).fetchone()
        if existing is None:
            self._conn.execute(
                """
                INSERT INTO business_profiles(
                    business_id, activity_description, timezone, status,
                    created_by_member_id, created_at, updated_at
                ) VALUES(?, ?, ?, 'draft', ?, ?, ?)
                """,
                (
                    current.business_id,
                    description,
                    timezone_value,
                    current.membership_id,
                    timestamp,
                    timestamp,
                ),
            )
        else:
            self._conn.execute(
                """
                UPDATE business_profiles
                SET activity_description=?, timezone=?, updated_at=?
                WHERE business_id=?
                """,
                (description, timezone_value, timestamp, current.business_id),
            )
        return self.get_profile(actor=current)

    def get_profile(self, *, actor: TenantContext) -> BusinessProfile:
        current = self._current_actor(actor)
        row = self._conn.execute(
            """
            SELECT business_id, activity_description, timezone, status,
                   created_by_member_id, created_at, updated_at
            FROM business_profiles
            WHERE business_id=?
            LIMIT 1
            """,
            (current.business_id,),
        ).fetchone()
        if row is None:
            raise ActivityNotFound("business activity profile was not found")
        return _profile_from_row(row)

    def enable_capability(
        self,
        *,
        actor: TenantContext,
        connector_key: str,
        title: str | None = None,
        now: str | None = None,
    ) -> BusinessCapability:
        current = self._current_actor(actor)
        current.assert_can_manage_business()
        connector = resolve_activity_connector(connector_key)
        selected_title = normalize_capability_title(title or connector.title)
        timestamp = str(now or _utc_now())
        existing = self._conn.execute(
            """
            SELECT id FROM business_capabilities
            WHERE business_id=? AND connector_key=?
            LIMIT 1
            """,
            (current.business_id, connector.key),
        ).fetchone()
        if existing is None:
            capability_id = str(uuid4())
            self._conn.execute(
                """
                INSERT INTO business_capabilities(
                    id, business_id, connector_key, kind, title, status,
                    created_by_member_id, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    capability_id,
                    current.business_id,
                    connector.key,
                    connector.kind.value,
                    selected_title,
                    current.membership_id,
                    timestamp,
                    timestamp,
                ),
            )
        else:
            capability_id = str(_value(existing, "id", 0))
            self._conn.execute(
                """
                UPDATE business_capabilities
                SET kind=?, title=?, status='active', updated_at=?
                WHERE id=? AND business_id=?
                """,
                (
                    connector.kind.value,
                    selected_title,
                    timestamp,
                    capability_id,
                    current.business_id,
                ),
            )
        return self.get_capability(actor=current, capability_id=capability_id)

    def disable_capability(
        self,
        *,
        actor: TenantContext,
        connector_key: str,
        now: str | None = None,
    ) -> BusinessCapability:
        current = self._current_actor(actor)
        current.assert_can_manage_business()
        connector = resolve_activity_connector(connector_key)
        timestamp = str(now or _utc_now())
        row = self._conn.execute(
            """
            SELECT id FROM business_capabilities
            WHERE business_id=? AND connector_key=?
            LIMIT 1
            """,
            (current.business_id, connector.key),
        ).fetchone()
        if row is None:
            raise ActivityNotFound("business capability was not found")
        capability_id = str(_value(row, "id", 0))
        self._conn.execute(
            """
            UPDATE business_capabilities
            SET status='disabled', updated_at=?
            WHERE id=? AND business_id=?
            """,
            (timestamp, capability_id, current.business_id),
        )
        return self.get_capability(actor=current, capability_id=capability_id)

    def get_capability(self, *, actor: TenantContext, capability_id: str) -> BusinessCapability:
        current = self._current_actor(actor)
        normalized_id = normalize_uuid(capability_id, field_name="capability_id")
        row = self._conn.execute(
            """
            SELECT id, business_id, connector_key, kind, title, status,
                   created_by_member_id, created_at, updated_at
            FROM business_capabilities
            WHERE id=? AND business_id=?
            LIMIT 1
            """,
            (normalized_id, current.business_id),
        ).fetchone()
        if row is None:
            raise ActivityNotFound("business capability was not found")
        return _capability_from_row(row)

    def list_capabilities(
        self,
        *,
        actor: TenantContext,
        include_disabled: bool = False,
    ) -> list[BusinessCapability]:
        current = self._current_actor(actor)
        rows = self._conn.execute(
            """
            SELECT id, business_id, connector_key, kind, title, status,
                   created_by_member_id, created_at, updated_at
            FROM business_capabilities
            WHERE business_id=? AND (? OR status='active')
            ORDER BY created_at, connector_key
            """,
            (current.business_id, 1 if include_disabled else 0),
        ).fetchall()
        return [_capability_from_row(row) for row in rows]

    def complete_profile(self, *, actor: TenantContext, now: str | None = None) -> BusinessProfile:
        current = self._current_actor(actor)
        current.assert_can_manage_business()
        profile = self.get_profile(actor=current)
        if not self.list_capabilities(actor=current):
            raise ActivityInvariantViolation("at least one activity capability is required")
        timestamp = str(now or _utc_now())
        self._conn.execute(
            "UPDATE business_profiles SET status='ready', updated_at=? WHERE business_id=?",
            (timestamp, profile.business_id),
        )
        return self.get_profile(actor=current)

    def create_offering(
        self,
        *,
        actor: TenantContext,
        capability_id: str,
        title: str,
        description: str,
        idempotency_key: str | None = None,
        now: str | None = None,
    ) -> BusinessOffering:
        current = self._current_actor(actor)
        current.assert_can_manage_programs()
        capability = self.get_capability(actor=current, capability_id=capability_id)
        if capability.status != CapabilityStatus.ACTIVE:
            raise ActivityInvariantViolation("disabled capability cannot receive offerings")
        connector = resolve_activity_connector(capability.connector_key)
        if not connector.supports_offerings:
            raise ActivityInvariantViolation("this connector uses its own specialized content model")
        normalized_title = normalize_offering_title(title)
        normalized_description = normalize_offering_description(description)
        if idempotency_key is None:
            offering_id = str(uuid4())
        else:
            normalized_key = str(idempotency_key).strip()
            if not normalized_key or len(normalized_key) > 500:
                raise ValueError("idempotency_key must be 1..500 characters")
            if any(ord(char) < 32 or ord(char) == 127 for char in normalized_key):
                raise ValueError("idempotency_key contains control characters")
            offering_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"clientplatform:offering:{current.business_id}:{normalized_key}",
                )
            )
            existing = self._conn.execute(
                """
                SELECT id, business_id, capability_id, title, description, status,
                       created_by_member_id, created_at, updated_at
                FROM business_offerings
                WHERE id=? AND business_id=?
                LIMIT 1
                """,
                (offering_id, current.business_id),
            ).fetchone()
            if existing is not None:
                offering = _offering_from_row(existing)
                if (
                    offering.capability_id != capability.id
                    or offering.title != normalized_title
                    or offering.description != normalized_description
                ):
                    raise ActivityInvariantViolation(
                        "offering idempotency key belongs to different work"
                    )
                return offering
        timestamp = str(now or _utc_now())
        self._conn.execute(
            """
            INSERT INTO business_offerings(
                id, business_id, capability_id, title, description, status,
                created_by_member_id, created_at, updated_at, archived_at
            ) VALUES(?, ?, ?, ?, ?, 'active', ?, ?, ?, NULL)
            """,
            (
                offering_id,
                current.business_id,
                capability.id,
                normalized_title,
                normalized_description,
                current.membership_id,
                timestamp,
                timestamp,
            ),
        )
        return self.get_offering(actor=current, offering_id=offering_id)

    def get_offering(self, *, actor: TenantContext, offering_id: str) -> BusinessOffering:
        current = self._current_actor(actor)
        normalized_id = normalize_uuid(offering_id, field_name="offering_id")
        row = self._conn.execute(
            """
            SELECT id, business_id, capability_id, title, description, status,
                   created_by_member_id, created_at, updated_at
            FROM business_offerings
            WHERE id=? AND business_id=?
            LIMIT 1
            """,
            (normalized_id, current.business_id),
        ).fetchone()
        if row is None:
            raise ActivityNotFound("business offering was not found")
        return _offering_from_row(row)

    def archive_offering(
        self,
        *,
        actor: TenantContext,
        offering_id: str,
        now: str | None = None,
    ) -> BusinessOffering:
        current = self._current_actor(actor)
        current.assert_can_manage_programs()
        normalized_id = normalize_uuid(offering_id, field_name="offering_id")
        offering = self.get_offering(actor=current, offering_id=normalized_id)
        if offering.status == OfferingStatus.ARCHIVED:
            return offering
        timestamp = str(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE business_offerings
            SET status='archived', archived_at=?, updated_at=?
            WHERE id=? AND business_id=? AND status='active'
            """,
            (timestamp, timestamp, normalized_id, current.business_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            latest = self.get_offering(actor=current, offering_id=normalized_id)
            if latest.status == OfferingStatus.ARCHIVED:
                return latest
            raise ActivityInvariantViolation("offering changed concurrently; refresh and retry")
        return self.get_offering(actor=current, offering_id=normalized_id)

    def list_offerings(
        self,
        *,
        actor: TenantContext,
        capability_id: str,
        include_archived: bool = False,
    ) -> list[BusinessOffering]:
        current = self._current_actor(actor)
        capability = self.get_capability(actor=current, capability_id=capability_id)
        rows = self._conn.execute(
            """
            SELECT id, business_id, capability_id, title, description, status,
                   created_by_member_id, created_at, updated_at
            FROM business_offerings
            WHERE business_id=? AND capability_id=? AND (? OR status='active')
            ORDER BY created_at, id
            """,
            (current.business_id, capability.id, 1 if include_archived else 0),
        ).fetchall()
        return [_offering_from_row(row) for row in rows]

    def issue_customer_invite(
        self,
        *,
        actor: TenantContext,
        ttl_days: int = 7,
        now: str | None = None,
    ) -> IssuedCustomerInvite:
        current = self._current_actor(actor)
        current.assert_can_manage_deliveries()
        if isinstance(ttl_days, bool) or int(ttl_days) < 1 or int(ttl_days) > 30:
            raise ValueError("invite ttl_days must be between 1 and 30")
        timestamp = str(now or _utc_now())
        parsed_now = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed_now.tzinfo is None:
            parsed_now = parsed_now.replace(tzinfo=timezone.utc)
        expires_at = (parsed_now + timedelta(days=int(ttl_days))).isoformat(timespec="seconds")
        token = new_invite_token()
        invite_id = str(uuid4())
        self._conn.execute(
            """
            INSERT INTO customer_invites(
                id, business_id, token_hash, status, expires_at,
                created_by_member_id, claimed_customer_id, created_at,
                claimed_at, revoked_at
            ) VALUES(?, ?, ?, 'active', ?, ?, NULL, ?, NULL, NULL)
            """,
            (
                invite_id,
                current.business_id,
                invite_token_hash(token),
                expires_at,
                current.membership_id,
                timestamp,
            ),
        )
        row = self._conn.execute(
            """
            SELECT id, business_id, status, expires_at, created_by_member_id,
                   created_at, claimed_customer_id, token_hash, claimed_at, revoked_at
            FROM customer_invites WHERE id=? AND business_id=?
            """,
            (invite_id, current.business_id),
        ).fetchone()
        if row is None:
            raise ActivityNotFound("issued customer invite was not found")
        return IssuedCustomerInvite(invite=_invite_from_row(row), token=token)

    def claim_customer_invite(
        self,
        *,
        token: str,
        telegram_user_id: int,
        username: str | None,
        display_name: str | None,
        now: str | None = None,
    ) -> InviteClaim:
        principal_id = normalize_user_id(telegram_user_id)
        return self.claim_customer_invite_identity(
            token=token,
            platform=CustomerPlatform.TELEGRAM,
            external_subject=str(principal_id),
            username=username,
            display_name=display_name,
            now=now,
        )

    def claim_customer_invite_identity(
        self,
        *,
        token: str,
        platform: CustomerPlatform | str,
        external_subject: str,
        username: str | None,
        display_name: str | None,
        expected_business_id: str | None = None,
        now: str | None = None,
    ) -> InviteClaim:
        normalized_platform, normalized_subject = normalize_identity_subject(
            platform, external_subject
        )
        timestamp = str(now or _utc_now())
        row = self._conn.execute(
            """
            SELECT ci.id, ci.business_id, ci.status, ci.expires_at,
                   ci.created_by_member_id, ci.claimed_customer_id,
                   b.name AS business_name, bm.user_id AS creator_user_id
            FROM customer_invites ci
            JOIN businesses b ON b.id=ci.business_id AND b.status='active'
            JOIN business_members bm
              ON bm.id=ci.created_by_member_id
             AND bm.business_id=ci.business_id
             AND bm.status='active'
            WHERE ci.token_hash=?
            LIMIT 1
            """,
            (invite_token_hash(token),),
        ).fetchone()
        if row is None:
            raise ActivityNotFound("customer invite was not found")
        business_id = str(_value(row, "business_id", 1))
        if expected_business_id is not None:
            expected = normalize_uuid(
                expected_business_id, field_name="expected_business_id"
            )
            if business_id != expected:
                raise ActivityInvariantViolation(
                    "customer invite belongs to another business"
                )
        status = InviteStatus(str(_value(row, "status", 2)))
        expires_at = datetime.fromisoformat(str(_value(row, "expires_at", 3)).replace("Z", "+00:00"))
        current_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        claimed_customer_id = _value(row, "claimed_customer_id", 5)
        if status == InviteStatus.CLAIMED:
            if claimed_customer_id is None:
                raise ActivityInvariantViolation("claimed invite lost its customer reference")
            identity_row = self._conn.execute(
                """
                SELECT 1 FROM customer_identities
                WHERE business_id=? AND customer_id=? AND platform=?
                  AND external_subject=? AND status='active'
                LIMIT 1
                """,
                (
                    str(_value(row, "business_id", 1)),
                    str(claimed_customer_id),
                    normalized_platform.value,
                    normalized_subject,
                ),
            ).fetchone()
            if identity_row is None:
                raise ActivityInvariantViolation("customer invite has already been used")
            return InviteClaim(
                business_id=str(_value(row, "business_id", 1)),
                business_name=str(_value(row, "business_name", 6)),
                customer_id=str(claimed_customer_id),
                already_connected=True,
            )
        if status != InviteStatus.ACTIVE:
            raise ActivityInvariantViolation("customer invite is not active")
        if current_time >= expires_at:
            self._conn.execute(
                "UPDATE customer_invites SET status='expired' WHERE id=? AND status='active'",
                (str(_value(row, "id", 0)),),
            )
            raise ActivityInvariantViolation("customer invite has expired")

        creator_user_id = int(_value(row, "creator_user_id", 7))
        actor = self._tenancy.resolve_context(user_id=creator_user_id, business_id=business_id)
        customers = CustomerRepository(self._conn)
        try:
            record = customers.find_by_identity(
                actor=actor,
                platform=normalized_platform,
                external_subject=normalized_subject,
            )
            customer_id = record.customer.id
            already_connected = True
        except CustomerNotFound:
            customer = customers.create_customer(actor=actor, display_name=display_name)
            customers.attach_identity(
                actor=actor,
                customer_id=customer.id,
                platform=normalized_platform,
                external_subject=normalized_subject,
                username=username,
                display_name=display_name,
            )
            customer_id = customer.id
            already_connected = False

        cursor = self._conn.execute(
            """
            UPDATE customer_invites
            SET status='claimed', claimed_customer_id=?, claimed_at=?
            WHERE id=? AND status='active'
            """,
            (customer_id, timestamp, str(_value(row, "id", 0))),
        )
        if getattr(cursor, "rowcount", 1) == 0:
            raise ActivityInvariantViolation("customer invite was claimed concurrently")
        return InviteClaim(
            business_id=business_id,
            business_name=str(_value(row, "business_name", 6)),
            customer_id=customer_id,
            already_connected=already_connected,
        )
