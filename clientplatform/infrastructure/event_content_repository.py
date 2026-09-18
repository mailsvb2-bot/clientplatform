from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from clientplatform.domain.event_content import (
    EventContentMode,
    EventContentPreference,
    EventContentStage,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def _from_row(row: Any) -> EventContentPreference:
    return EventContentPreference(
        business_id=str(_value(row, "business_id", 0)),
        event_id=str(_value(row, "event_id", 1)),
        stage=EventContentStage(str(_value(row, "stage", 2))),
        mode=EventContentMode(str(_value(row, "mode", 3))),
        updated_by_member_id=str(_value(row, "updated_by_member_id", 4)),
        created_at=str(_value(row, "created_at", 5)),
        updated_at=str(_value(row, "updated_at", 6)),
    )


_COLUMNS = (
    "business_id,event_id,stage,mode,updated_by_member_id,created_at,updated_at"
)


class EventContentPreferenceRepository:
    def __init__(self, conn: Any):
        self._conn = conn

    def set(
        self,
        *,
        actor: TenantContext,
        event_id: str,
        stage: EventContentStage,
        mode: EventContentMode,
        now: str | None = None,
    ) -> EventContentPreference:
        actor.assert_can_manage_business()
        normalized_event_id = normalize_uuid(event_id, field_name="event_id")
        event = self._conn.execute(
            """
            SELECT id FROM clientplatform_events
            WHERE id=? AND business_id=? LIMIT 1
            """,
            (normalized_event_id, actor.business_id),
        ).fetchone()
        if event is None:
            raise ValueError("event was not found in the active business")
        timestamp = str(now or _utc_now())
        self._conn.execute(
            """
            INSERT INTO clientplatform_event_content_preferences(
                business_id,event_id,stage,mode,updated_by_member_id,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(business_id,event_id,stage) DO UPDATE SET
                mode=excluded.mode,
                updated_by_member_id=excluded.updated_by_member_id,
                updated_at=excluded.updated_at
            """,
            (
                actor.business_id,
                normalized_event_id,
                stage.value,
                mode.value,
                actor.membership_id,
                timestamp,
                timestamp,
            ),
        )
        stored = self.get(
            actor=actor,
            event_id=normalized_event_id,
            stage=stage,
        )
        if stored is None:
            raise RuntimeError("event content preference was not persisted")
        return stored

    def get(
        self,
        *,
        actor: TenantContext,
        event_id: str,
        stage: EventContentStage,
    ) -> EventContentPreference | None:
        actor.assert_can_manage_business()
        normalized_event_id = normalize_uuid(event_id, field_name="event_id")
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM clientplatform_event_content_preferences "  # nosec B608 - static columns
            "WHERE business_id=? AND event_id=? AND stage=? LIMIT 1",
            (actor.business_id, normalized_event_id, stage.value),
        ).fetchone()
        return None if row is None else _from_row(row)

    def list_for_event(
        self,
        *,
        actor: TenantContext,
        event_id: str,
    ) -> tuple[EventContentPreference, ...]:
        actor.assert_can_manage_business()
        normalized_event_id = normalize_uuid(event_id, field_name="event_id")
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM clientplatform_event_content_preferences "  # nosec B608 - static columns
            "WHERE business_id=? AND event_id=? ORDER BY stage",
            (actor.business_id, normalized_event_id),
        ).fetchall()
        return tuple(_from_row(row) for row in rows)


__all__ = ["EventContentPreferenceRepository"]
