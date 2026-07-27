from __future__ import annotations

from typing import Any

from a1.domain.tenancy import (
    BusinessMember,
    PlatformRole,
    TenantContext,
    TenantInvariantViolation,
    normalize_user_id,
)
from a1.infrastructure.tenancy_repository import TenancyRepository as _BaseTenancyRepository


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


class TenancyRepository(_BaseTenancyRepository):
    """Canonical A1 tenancy repository with invariant guards.

    The imported additive repository remains the storage implementation. This
    canonical façade closes invariants that require comparing the current and
    requested roles before delegating the mutation.
    """

    def grant_member(
        self,
        *,
        actor: TenantContext,
        user_id: int,
        role: PlatformRole | str,
        now: str | None = None,
    ) -> BusinessMember:
        current_actor = self.resolve_context(user_id=actor.user_id, business_id=actor.business_id)
        target_user_id = normalize_user_id(user_id)
        target_role = current_actor.assert_can_manage_members(role)
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
