from __future__ import annotations

from typing import Any

from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


def _rowdict(row: Any) -> dict[str, Any]:
    if hasattr(row, "keys"):
        return {str(key): row[key] for key in row.keys()}
    return dict(row)


def _bounded_limit(value: int, *, maximum: int = 50) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("limit must be a positive integer")
    if value <= 0:
        raise ValueError("limit must be a positive integer")
    return min(value, maximum)


class SalesUiRepository:
    """Read-only, tenant-scoped projections for the owner-facing sales UI."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def _customer_context(self, actor: TenantContext) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_view_customer_records()
        return current

    def _analytics_context(self, actor: TenantContext) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_view_promotion_analytics()
        return current

    def list_open_work(
        self,
        *,
        actor: TenantContext,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        current = self._customer_context(actor)
        selected_limit = _bounded_limit(limit)
        rows = self._conn.execute(
            """
            SELECT
                l.id,
                l.customer_id,
                COALESCE(c.display_name, 'Клиент') AS customer_name,
                l.source_kind,
                l.stage,
                l.updated_at,
                (
                    SELECT p.action_kind
                    FROM clientplatform_sales_action_plans p
                    WHERE p.business_id=l.business_id
                      AND p.lead_id=l.id
                      AND p.status='planned'
                    ORDER BY p.created_at DESC, p.id DESC
                    LIMIT 1
                ) AS next_action_kind
            FROM clientplatform_sales_leads l
            JOIN customers c
              ON c.id=l.customer_id AND c.business_id=l.business_id
            WHERE l.business_id=?
              AND l.stage IN ('new','contacted','qualified','checkout')
            ORDER BY l.updated_at DESC, l.id DESC
            LIMIT ?
            """,
            (current.business_id, selected_limit),
        ).fetchall()
        return [_rowdict(row) for row in rows]

    def list_handoff_work(
        self,
        *,
        actor: TenantContext,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        current = self._customer_context(actor)
        selected_limit = _bounded_limit(limit)
        rows = self._conn.execute(
            """
            SELECT
                h.id,
                h.lead_id,
                COALESCE(c.display_name, 'Клиент') AS customer_name,
                h.reason,
                h.severity,
                h.status,
                h.claimed_by_member_id,
                h.created_at
            FROM clientplatform_sales_handoffs h
            JOIN clientplatform_sales_leads l
              ON l.id=h.lead_id AND l.business_id=h.business_id
            JOIN customers c
              ON c.id=l.customer_id AND c.business_id=l.business_id
            WHERE h.business_id=?
              AND h.status IN ('open','claimed')
            ORDER BY
                CASE h.severity WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,
                h.created_at,
                h.id
            LIMIT ?
            """,
            (current.business_id, selected_limit),
        ).fetchall()
        return [_rowdict(row) for row in rows]

    def list_ladders(self, *, actor: TenantContext) -> list[dict[str, Any]]:
        current = self._analytics_context(actor)
        rows = self._conn.execute(
            """
            SELECT
                l.id,
                l.name,
                l.created_at,
                COUNT(s.id) AS step_count
            FROM commercial_ladders l
            LEFT JOIN commercial_ladder_steps s
              ON s.ladder_id=l.id AND s.business_id=l.business_id
            WHERE l.business_id=? AND l.status='active'
            GROUP BY l.id, l.name, l.created_at
            ORDER BY l.created_at, l.id
            """,
            (current.business_id,),
        ).fetchall()
        return [_rowdict(row) for row in rows]

    def list_ladder_steps(
        self,
        *,
        actor: TenantContext,
        ladder_id: str,
    ) -> list[dict[str, Any]]:
        current = self._analytics_context(actor)
        normalized = normalize_uuid(ladder_id, field_name="ladder_id")
        ladder = self._conn.execute(
            """
            SELECT 1
            FROM commercial_ladders
            WHERE id=? AND business_id=? AND status='active'
            LIMIT 1
            """,
            (normalized, current.business_id),
        ).fetchone()
        if ladder is None:
            raise ValueError("commercial ladder was not found in the active business")
        rows = self._conn.execute(
            """
            SELECT
                id,
                position,
                kind,
                title,
                offering_id,
                min_evidence_score,
                requires_human_approval
            FROM commercial_ladder_steps
            WHERE ladder_id=? AND business_id=?
            ORDER BY position, id
            """,
            (normalized, current.business_id),
        ).fetchall()
        return [_rowdict(row) for row in rows]
