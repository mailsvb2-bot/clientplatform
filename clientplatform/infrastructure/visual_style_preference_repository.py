from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from clientplatform.domain.tenancy import TenantContext
from clientplatform.domain.visual_style_intent import (
    VisualStyleIntent,
    visual_style_from_json,
)
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


class VisualStylePreferenceRepository:
    """Business/member-scoped owner style defaults; never generation authority."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def _actor(self, actor: TenantContext) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_promotions()
        return current

    def get(self, *, actor: TenantContext) -> VisualStyleIntent:
        current = self._actor(actor)
        row = self._conn.execute(
            """
            SELECT style_json
            FROM visual_style_preferences
            WHERE business_id=? AND member_id=?
            LIMIT 1
            """,
            (current.business_id, current.membership_id),
        ).fetchone()
        if row is None:
            return VisualStyleIntent()
        return visual_style_from_json(str(_value(row, "style_json", 0)))

    def save(
        self,
        *,
        actor: TenantContext,
        style: VisualStyleIntent,
        now: str | None = None,
    ) -> VisualStyleIntent:
        current = self._actor(actor)
        value = style.normalized()
        timestamp = str(now or _utc_now())
        payload = value.to_json()
        self._conn.execute(
            """
            INSERT INTO visual_style_preferences(
                business_id,member_id,style_json,created_at,updated_at
            ) VALUES(?,?,?,?,?)
            ON CONFLICT(business_id,member_id) DO UPDATE SET
                style_json=excluded.style_json,
                updated_at=excluded.updated_at
            """,
            (
                current.business_id,
                current.membership_id,
                payload,
                timestamp,
                timestamp,
            ),
        )
        return value


__all__ = ["VisualStylePreferenceRepository"]
