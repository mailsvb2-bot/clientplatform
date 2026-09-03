from __future__ import annotations

from typing import Any

from clientplatform.domain.tenancy import (
    BusinessMember,
    PlatformRole,
    TenantContext,
    TenantInvariantViolation,
)
from clientplatform.infrastructure.tenancy_repository import TenancyRepository as _BaseTenancyRepository


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


class TenancyRepository(_BaseTenancyRepository):
    """Canonical clientplatform tenancy repository with transactional invariant guards."""

    def _lock_business_membership_boundary(self, business_id: str) -> None:
        # Updating the shared business row obtains a transaction-scoped row lock
        # in PostgreSQL. Concurrent owner demotion/revocation operations for the
        # same business therefore serialize before counting active owners.
        # SQLite already serializes writes and accepts the same statement.
        self._conn.execute(
            """
            UPDATE businesses
            SET updated_at=updated_at
            WHERE id=? AND status='active'
            """,
            (business_id,),
        )

    def grant_member(
        self,
        *,
        actor: TenantContext,
        user_id: int,
        role: PlatformRole | str,
        now: str | None = None,
    ) -> BusinessMember:
        current_actor = self.resolve_context(user_id=actor.user_id, business_id=actor.business_id)
        target_user_id = self._canonical_user_id(user_id)
        target_role = current_actor.assert_can_manage_members(role)
        self._lock_business_membership_boundary(current_actor.business_id)
        existing = self._conn.execute(
            """
            SELECT role, status
            FROM business_members
            WHERE business_id=? AND user_id=?
            LIMIT 1
            """,
            (current_actor.business_id, target_user_id),
        ).fetchone()
        if existing is not None:
            existing_role = PlatformRole(str(_value(existing, "role", 0)))
            existing_status = str(_value(existing, "status", 1))
            if (
                existing_status == "active"
                and existing_role == PlatformRole.OWNER
                and target_role != PlatformRole.OWNER
            ):
                owner_count_row = self._conn.execute(
                    """
                    SELECT COUNT(*) AS c
                    FROM business_members
                    WHERE business_id=? AND role='owner' AND status='active'
                    """,
                    (current_actor.business_id,),
                ).fetchone()
                owner_count = int(_value(owner_count_row, "c", 0))
                if owner_count <= 1:
                    raise TenantInvariantViolation(
                        "a business must retain at least one active owner"
                    )
        return super().grant_member(
            actor=current_actor,
            user_id=target_user_id,
            role=target_role,
            now=now,
        )

    def revoke_member(
        self,
        *,
        actor: TenantContext,
        user_id: int,
        now: str | None = None,
    ) -> BusinessMember:
        current_actor = self.resolve_context(user_id=actor.user_id, business_id=actor.business_id)
        target_user_id = self._canonical_user_id(user_id)
        self._lock_business_membership_boundary(current_actor.business_id)
        revoked = super().revoke_member(
            actor=current_actor,
            user_id=target_user_id,
            now=now,
        )
        self._conn.execute(
            "DELETE FROM clientplatform_owner_input_sessions WHERE business_id=? AND user_id=?",
            (current_actor.business_id, target_user_id),
        )
        self._conn.execute(
            "DELETE FROM clientplatform_owner_control_workspaces WHERE business_id=? AND user_id=?",
            (current_actor.business_id, target_user_id),
        )
        self._conn.execute(
            "DELETE FROM clientplatform_owner_onboarding_sessions WHERE business_id=? AND user_id=?",
            (current_actor.business_id, target_user_id),
        )
        return revoked
