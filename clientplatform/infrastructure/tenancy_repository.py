from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.tenancy import (
    Business,
    BusinessAccess,
    BusinessMember,
    BusinessStatus,
    MembershipStatus,
    PlatformRole,
    TenantAccessDenied,
    TenantContext,
    TenantInvariantViolation,
    TenantPermissionDenied,
    normalize_business_name,
    normalize_user_id,
    normalize_uuid,
    parse_business_member_role,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _business_from_row(row: Any) -> Business:
    return Business(
        id=str(_value(row, "id", 0)),
        name=str(_value(row, "name", 1)),
        status=BusinessStatus(str(_value(row, "status", 2))),
        created_by_user_id=int(_value(row, "created_by_user_id", 3)),
        created_at=str(_value(row, "created_at", 4)),
        updated_at=str(_value(row, "updated_at", 5)),
    )


def _member_from_row(row: Any) -> BusinessMember:
    revoked_at = _value(row, "revoked_at", 7)
    return BusinessMember(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        user_id=int(_value(row, "user_id", 2)),
        role=PlatformRole(str(_value(row, "role", 3))),
        status=MembershipStatus(str(_value(row, "status", 4))),
        created_at=str(_value(row, "created_at", 5)),
        updated_at=str(_value(row, "updated_at", 6)),
        revoked_at=None if revoked_at is None else str(revoked_at),
    )


class TenancyRepository:
    """Explicit tenant repository bound to one caller-owned DB connection."""

    def __init__(self, conn: Any):
        self._conn = conn

    def create_business(
        self,
        *,
        owner_user_id: int,
        name: str,
        business_id: str | None = None,
        now: str | None = None,
    ) -> BusinessAccess:
        owner_id = normalize_user_id(owner_user_id)
        normalized_name = normalize_business_name(name)
        new_business_id = normalize_uuid(
            business_id or str(uuid4()),
            field_name="business_id",
        )
        membership_id = str(uuid4())
        timestamp = str(now or _utc_now())

        self._conn.execute(
            """
            INSERT INTO businesses(
                id, name, status, created_by_user_id, created_at, updated_at
            ) VALUES(?, ?, 'active', ?, ?, ?)
            """,
            (new_business_id, normalized_name, owner_id, timestamp, timestamp),
        )
        self._conn.execute(
            """
            INSERT INTO business_members(
                id, business_id, user_id, role, status,
                created_at, updated_at, revoked_at
            ) VALUES(?, ?, ?, 'owner', 'active', ?, ?, NULL)
            """,
            (membership_id, new_business_id, owner_id, timestamp, timestamp),
        )
        return self.get_access(user_id=owner_id, business_id=new_business_id)

    def resolve_context(self, *, user_id: int, business_id: str) -> TenantContext:
        principal_id = normalize_user_id(user_id)
        normalized_business_id = normalize_uuid(
            business_id,
            field_name="business_id",
        )
        row = self._conn.execute(
            """
            SELECT bm.id, bm.business_id, bm.user_id, bm.role
            FROM business_members bm
            JOIN businesses b ON b.id = bm.business_id
            WHERE bm.business_id=?
              AND bm.user_id=?
              AND bm.status='active'
              AND b.status='active'
            LIMIT 1
            """,
            (normalized_business_id, principal_id),
        ).fetchone()
        if row is None:
            raise TenantAccessDenied("active business membership was not found")
        return TenantContext(
            membership_id=str(_value(row, "id", 0)),
            business_id=str(_value(row, "business_id", 1)),
            user_id=int(_value(row, "user_id", 2)),
            role=PlatformRole(str(_value(row, "role", 3))),
        )

    def set_owner_control_workspace(
        self,
        *,
        user_id: int,
        platform: str,
        business_id: str,
        now: str | None = None,
    ) -> str:
        principal_id = normalize_user_id(user_id)
        normalized_platform = str(platform or "").strip().casefold()
        if normalized_platform not in {"telegram", "vk", "max"}:
            raise ValueError("owner control platform is invalid")
        current = self.resolve_context(user_id=principal_id, business_id=business_id)
        timestamp = str(now or _utc_now())
        self._conn.execute(
            """
            INSERT INTO clientplatform_owner_control_workspaces(
                user_id, platform, business_id, updated_at
            ) VALUES(?, ?, ?, ?)
            ON CONFLICT(user_id, platform) DO UPDATE SET
                business_id=excluded.business_id,
                updated_at=excluded.updated_at
            """,
            (principal_id, normalized_platform, current.business_id, timestamp),
        )
        return current.business_id

    def get_owner_control_workspace(self, *, user_id: int, platform: str) -> str | None:
        principal_id = normalize_user_id(user_id)
        normalized_platform = str(platform or "").strip().casefold()
        if normalized_platform not in {"telegram", "vk", "max"}:
            raise ValueError("owner control platform is invalid")
        row = self._conn.execute(
            """
            SELECT w.business_id
            FROM clientplatform_owner_control_workspaces w
            JOIN business_members bm
              ON bm.business_id=w.business_id AND bm.user_id=w.user_id
            JOIN businesses b ON b.id=w.business_id
            WHERE w.user_id=? AND w.platform=?
              AND bm.status='active' AND b.status='active'
            LIMIT 1
            """,
            (principal_id, normalized_platform),
        ).fetchone()
        if row is None:
            return None
        return str(_value(row, "business_id", 0))

    def get_access(self, *, user_id: int, business_id: str) -> BusinessAccess:
        context = self.resolve_context(user_id=user_id, business_id=business_id)
        business_row = self._conn.execute(
            """
            SELECT id, name, status, created_by_user_id, created_at, updated_at
            FROM businesses
            WHERE id=? AND status='active'
            LIMIT 1
            """,
            (context.business_id,),
        ).fetchone()
        member_row = self._conn.execute(
            """
            SELECT id, business_id, user_id, role, status,
                   created_at, updated_at, revoked_at
            FROM business_members
            WHERE id=? AND business_id=? AND status='active'
            LIMIT 1
            """,
            (context.membership_id, context.business_id),
        ).fetchone()
        if business_row is None or member_row is None:
            raise TenantAccessDenied("tenant access became inactive")
        return BusinessAccess(
            business=_business_from_row(business_row),
            membership=_member_from_row(member_row),
        )

    def list_accessible_businesses(self, *, user_id: int) -> list[BusinessAccess]:
        principal_id = normalize_user_id(user_id)
        rows = self._conn.execute(
            """
            SELECT
                b.id AS business_id,
                b.name AS business_name,
                b.status AS business_status,
                b.created_by_user_id,
                b.created_at AS business_created_at,
                b.updated_at AS business_updated_at,
                bm.id AS membership_id,
                bm.user_id,
                bm.role,
                bm.status AS membership_status,
                bm.created_at AS membership_created_at,
                bm.updated_at AS membership_updated_at,
                bm.revoked_at
            FROM business_members bm
            JOIN businesses b ON b.id = bm.business_id
            WHERE bm.user_id=? AND bm.status='active' AND b.status='active'
            ORDER BY b.created_at, b.id
            """,
            (principal_id,),
        ).fetchall()
        accesses: list[BusinessAccess] = []
        for row in rows:
            business = Business(
                id=str(_value(row, "business_id", 0)),
                name=str(_value(row, "business_name", 1)),
                status=BusinessStatus(str(_value(row, "business_status", 2))),
                created_by_user_id=int(_value(row, "created_by_user_id", 3)),
                created_at=str(_value(row, "business_created_at", 4)),
                updated_at=str(_value(row, "business_updated_at", 5)),
            )
            revoked_at = _value(row, "revoked_at", 12)
            membership = BusinessMember(
                id=str(_value(row, "membership_id", 6)),
                business_id=business.id,
                user_id=int(_value(row, "user_id", 7)),
                role=PlatformRole(str(_value(row, "role", 8))),
                status=MembershipStatus(str(_value(row, "membership_status", 9))),
                created_at=str(_value(row, "membership_created_at", 10)),
                updated_at=str(_value(row, "membership_updated_at", 11)),
                revoked_at=None if revoked_at is None else str(revoked_at),
            )
            accesses.append(
                BusinessAccess(business=business, membership=membership)
            )
        return accesses

    def rename_business(
        self,
        *,
        actor: TenantContext,
        name: str,
        now: str | None = None,
    ) -> Business:
        current_actor = self.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current_actor.assert_can_manage_business()
        normalized_name = normalize_business_name(name)
        timestamp = str(now or _utc_now())
        self._conn.execute(
            "UPDATE businesses SET name=?, updated_at=? "
            "WHERE id=? AND status='active'",
            (normalized_name, timestamp, current_actor.business_id),
        )
        return self.get_access(
            user_id=current_actor.user_id,
            business_id=current_actor.business_id,
        ).business

    def grant_member(
        self,
        *,
        actor: TenantContext,
        user_id: int,
        role: PlatformRole | str,
        now: str | None = None,
    ) -> BusinessMember:
        current_actor = self.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        target_user_id = normalize_user_id(user_id)
        target_role = current_actor.assert_can_manage_members(role)
        timestamp = str(now or _utc_now())

        existing = self._conn.execute(
            """
            SELECT id, business_id, user_id, role, status,
                   created_at, updated_at, revoked_at
            FROM business_members
            WHERE business_id=? AND user_id=?
            LIMIT 1
            """,
            (current_actor.business_id, target_user_id),
        ).fetchone()
        if existing is not None:
            existing_role = PlatformRole(str(_value(existing, "role", 3)))
            existing_status = MembershipStatus(str(_value(existing, "status", 4)))
            if current_actor.role == PlatformRole.ADMINISTRATOR and existing_role not in {
                PlatformRole.MANAGER,
                PlatformRole.CONTENT_MANAGER,
                PlatformRole.MARKETER,
                PlatformRole.ANALYST,
                PlatformRole.SUPPORT,
            }:
                raise TenantPermissionDenied(
                    "administrator cannot modify owner or administrator membership"
                )
            if (
                existing_role == PlatformRole.OWNER
                and existing_status == MembershipStatus.ACTIVE
                and target_role != PlatformRole.OWNER
            ):
                self._serialize_owner_membership_change(current_actor.business_id)
                self._assert_another_active_owner(current_actor.business_id)
            membership_id = str(_value(existing, "id", 0))
            self._conn.execute(
                """
                UPDATE business_members
                SET role=?, status='active', updated_at=?, revoked_at=NULL
                WHERE id=? AND business_id=?
                """,
                (
                    target_role.value,
                    timestamp,
                    membership_id,
                    current_actor.business_id,
                ),
            )
        else:
            membership_id = str(uuid4())
            self._conn.execute(
                """
                INSERT INTO business_members(
                    id, business_id, user_id, role, status,
                    created_at, updated_at, revoked_at
                ) VALUES(?, ?, ?, ?, 'active', ?, ?, NULL)
                """,
                (
                    membership_id,
                    current_actor.business_id,
                    target_user_id,
                    target_role.value,
                    timestamp,
                    timestamp,
                ),
            )
        return self._get_member(
            membership_id=membership_id,
            business_id=current_actor.business_id,
        )

    def revoke_member(
        self,
        *,
        actor: TenantContext,
        user_id: int,
        now: str | None = None,
    ) -> BusinessMember:
        current_actor = self.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        target_user_id = normalize_user_id(user_id)
        row = self._conn.execute(
            """
            SELECT id, business_id, user_id, role, status,
                   created_at, updated_at, revoked_at
            FROM business_members
            WHERE business_id=? AND user_id=? AND status='active'
            LIMIT 1
            """,
            (current_actor.business_id, target_user_id),
        ).fetchone()
        if row is None:
            raise TenantAccessDenied("active target membership was not found")
        target_role = PlatformRole(str(_value(row, "role", 3)))
        current_actor.assert_can_manage_members(target_role)
        if target_role == PlatformRole.OWNER:
            self._serialize_owner_membership_change(current_actor.business_id)
            self._assert_another_active_owner(current_actor.business_id)

        membership_id = str(_value(row, "id", 0))
        timestamp = str(now or _utc_now())
        self._conn.execute(
            """
            UPDATE business_members
            SET status='revoked', updated_at=?, revoked_at=?
            WHERE id=? AND business_id=? AND status='active'
            """,
            (
                timestamp,
                timestamp,
                membership_id,
                current_actor.business_id,
            ),
        )
        return self._get_member(
            membership_id=membership_id,
            business_id=current_actor.business_id,
        )

    def _serialize_owner_membership_change(self, business_id: str) -> None:
        """Serialize owner removal/demotion on one tenant across DB dialects.

        PostgreSQL obtains a row-level write lock on ``businesses``; SQLite
        obtains its normal transaction write lock. The assignment is a no-op so
        business-visible timestamps do not change merely because permissions are
        being validated.
        """

        cursor = self._conn.execute(
            """
            UPDATE businesses
            SET updated_at=updated_at
            WHERE id=? AND status='active'
            """,
            (business_id,),
        )
        if int(getattr(cursor, "rowcount", 1) or 0) != 1:
            raise TenantAccessDenied("active business was not found")

    def _assert_another_active_owner(self, business_id: str) -> None:
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM business_members
            WHERE business_id=? AND role='owner' AND status='active'
            """,
            (business_id,),
        ).fetchone()
        owner_count = int(_value(row, "c", 0))
        if owner_count <= 1:
            raise TenantInvariantViolation(
                "a business must retain at least one active owner"
            )

    def _get_member(
        self,
        *,
        membership_id: str,
        business_id: str,
    ) -> BusinessMember:
        normalized_membership_id = normalize_uuid(
            membership_id,
            field_name="membership_id",
        )
        normalized_business_id = normalize_uuid(
            business_id,
            field_name="business_id",
        )
        row = self._conn.execute(
            """
            SELECT id, business_id, user_id, role, status,
                   created_at, updated_at, revoked_at
            FROM business_members
            WHERE id=? AND business_id=?
            LIMIT 1
            """,
            (normalized_membership_id, normalized_business_id),
        ).fetchone()
        if row is None:
            raise TenantAccessDenied(
                "membership was not found in the active business scope"
            )
        return _member_from_row(row)
