from __future__ import annotations

"""Cross-database activity repository fixes for boolean list filters.

SQLite accepts integers directly in boolean expressions. PostgreSQL does not:
`0 OR status='active'` raises a datatype mismatch. Comparing the compatibility
flag with `1` keeps the existing integer parameters valid on both databases.
"""

from clientplatform.domain.activity import BusinessCapability, BusinessOffering
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.activity_repository import (
    ActivityRepository as BaseActivityRepository,
    _capability_from_row,
    _offering_from_row,
)


class ActivityRepository(BaseActivityRepository):
    """Production-safe ActivityRepository for both SQLite and PostgreSQL."""

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
            WHERE business_id=? AND (? = 1 OR status='active')
            ORDER BY created_at, connector_key
            """,
            (current.business_id, 1 if include_disabled else 0),
        ).fetchall()
        return [_capability_from_row(row) for row in rows]

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
            WHERE business_id=? AND capability_id=?
              AND (? = 1 OR status='active')
            ORDER BY created_at, id
            """,
            (
                current.business_id,
                capability.id,
                1 if include_archived else 0,
            ),
        ).fetchall()
        return [_offering_from_row(row) for row in rows]


__all__ = ["ActivityRepository"]
