from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from clientplatform.domain.tenancy import TenantContext
from clientplatform.domain.visual_style_intent import VisualStyleIntent
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class VisualStylePreferenceRepository:
    """Per-owner, per-business reusable visual-style preference."""

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
            WHERE business_id=? AND created_by_member_id=?
            LIMIT 1
            """,
            (current.business_id, current.membership_id),
        ).fetchone()
        if row is None:
            return VisualStyleIntent()
        raw = row["style_json"] if hasattr(row, "keys") else row[0]
        return VisualStyleIntent.from_json(str(raw or ""))

    def save(
        self,
        *,
        actor: TenantContext,
        style: VisualStyleIntent,
        now: str | None = None,
    ) -> VisualStyleIntent:
        current = self._actor(actor)
        normalized = style.normalized()
        raw = normalized.to_json()
        timestamp = str(now or _now())
        self._conn.execute(
            """
            INSERT INTO visual_style_preferences(
                business_id,created_by_member_id,style_json,created_at,updated_at
            ) VALUES(?,?,?,?,?)
            ON CONFLICT(business_id, created_by_member_id) DO UPDATE SET
                style_json=excluded.style_json,
                updated_at=excluded.updated_at
            """,
            (
                current.business_id,
                current.membership_id,
                raw,
                timestamp,
                timestamp,
            ),
        )
        return normalized

    def clear(self, *, actor: TenantContext) -> bool:
        current = self._actor(actor)
        cursor = self._conn.execute(
            """
            DELETE FROM visual_style_preferences
            WHERE business_id=? AND created_by_member_id=?
            """,
            (current.business_id, current.membership_id),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0


__all__ = ["VisualStylePreferenceRepository"]
