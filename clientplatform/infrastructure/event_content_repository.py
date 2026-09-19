from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from clientplatform.domain.event_content import (
    EventContentAsset,
    EventContentMessage,
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


_MESSAGE_COLUMNS = (
    "business_id,event_id,stage,slot_key,position,revision,scheduled_at,text,source,"
    "updated_by_member_id,created_at,updated_at"
)


def _message_from_row(row: Any) -> EventContentMessage:
    return EventContentMessage(
        business_id=str(_value(row, "business_id", 0)),
        event_id=str(_value(row, "event_id", 1)),
        stage=EventContentStage(str(_value(row, "stage", 2))),
        slot_key=str(_value(row, "slot_key", 3)),
        position=int(_value(row, "position", 4)),
        revision=int(_value(row, "revision", 5)),
        scheduled_at=(
            None
            if _value(row, "scheduled_at", 6) is None
            else str(_value(row, "scheduled_at", 6))
        ),
        text=str(_value(row, "text", 7)),
        source=str(_value(row, "source", 8)),
        updated_by_member_id=str(_value(row, "updated_by_member_id", 9)),
        created_at=str(_value(row, "created_at", 10)),
        updated_at=str(_value(row, "updated_at", 11)),
    )


def _slot_key(value: object) -> str:
    key = str(value or "").strip()
    if not key or len(key) > 80:
        raise ValueError("event content slot key is invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in key):
        raise ValueError("event content slot key is invalid")
    return key


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




class EventContentMessageRepository:
    def __init__(self, conn: Any):
        self._conn = conn

    def _event_exists(self, *, actor: TenantContext, event_id: str) -> str:
        actor.assert_can_manage_business()
        normalized = normalize_uuid(event_id, field_name="event_id")
        row = self._conn.execute(
            "SELECT id FROM clientplatform_events WHERE id=? AND business_id=? LIMIT 1",
            (normalized, actor.business_id),
        ).fetchone()
        if row is None:
            raise ValueError("event was not found in the active business")
        return normalized

    def upsert(
        self,
        *,
        actor: TenantContext,
        event_id: str,
        stage: EventContentStage,
        slot_key: str,
        position: int,
        text: str,
        source: str,
        scheduled_at: str | None,
        now: str | None = None,
    ) -> EventContentMessage:
        normalized = self._event_exists(actor=actor, event_id=event_id)
        key = _slot_key(slot_key)
        if isinstance(position, bool) or not isinstance(position, int) or position < 1:
            raise ValueError("event content position is invalid")
        body = str(text or "").strip()
        if not 1 <= len(body) <= 4000:
            raise ValueError("event content text length is invalid")
        source_value = str(source or "").strip().lower()
        if source_value not in {"template", "ai", "owner"}:
            raise ValueError("event content source is invalid")
        timestamp = str(now or _utc_now())
        schedule = None if scheduled_at is None else str(scheduled_at)
        self._conn.execute(
            """
            INSERT INTO clientplatform_event_content_messages(
                business_id,event_id,stage,slot_key,position,revision,scheduled_at,text,source,
                updated_by_member_id,created_at,updated_at
            ) VALUES(?,?,?,?,?,1,?,?,?,?,?,?)
            ON CONFLICT(business_id,event_id,stage,slot_key) DO UPDATE SET
                position=excluded.position,
                revision=clientplatform_event_content_messages.revision + 1,
                scheduled_at=excluded.scheduled_at,
                text=excluded.text,
                source=excluded.source,
                updated_by_member_id=excluded.updated_by_member_id,
                updated_at=excluded.updated_at
            """,
            (
                actor.business_id,
                normalized,
                stage.value,
                key,
                position,
                schedule,
                body,
                source_value,
                actor.membership_id,
                timestamp,
                timestamp,
            ),
        )
        stored = self.get(
            actor=actor,
            event_id=normalized,
            stage=stage,
            slot_key=key,
        )
        if stored is None:
            raise RuntimeError("event content message was not persisted")
        return stored

    def get(
        self,
        *,
        actor: TenantContext,
        event_id: str,
        stage: EventContentStage,
        slot_key: str,
    ) -> EventContentMessage | None:
        normalized = self._event_exists(actor=actor, event_id=event_id)
        key = _slot_key(slot_key)
        row = self._conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM clientplatform_event_content_messages "  # nosec B608 - static columns
            "WHERE business_id=? AND event_id=? AND stage=? AND slot_key=? LIMIT 1",
            (actor.business_id, normalized, stage.value, key),
        ).fetchone()
        return None if row is None else _message_from_row(row)

    def list_for_stage(
        self,
        *,
        actor: TenantContext,
        event_id: str,
        stage: EventContentStage,
    ) -> tuple[EventContentMessage, ...]:
        normalized = self._event_exists(actor=actor, event_id=event_id)
        rows = self._conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM clientplatform_event_content_messages "  # nosec B608 - static columns
            "WHERE business_id=? AND event_id=? AND stage=? "
            "ORDER BY position,slot_key",
            (actor.business_id, normalized, stage.value),
        ).fetchall()
        return tuple(_message_from_row(row) for row in rows)

    def delete_missing_slots(
        self,
        *,
        actor: TenantContext,
        event_id: str,
        stage: EventContentStage,
        keep_slot_keys: tuple[str, ...],
    ) -> int:
        normalized = self._event_exists(actor=actor, event_id=event_id)
        keys = tuple(_slot_key(value) for value in keep_slot_keys)
        if not keys:
            cursor = self._conn.execute(
                """
                DELETE FROM clientplatform_event_content_messages
                WHERE business_id=? AND event_id=? AND stage=?
                """,
                (actor.business_id, normalized, stage.value),
            )
        else:
            placeholders = ",".join("?" for _ in keys)
            cursor = self._conn.execute(
                f"DELETE FROM clientplatform_event_content_messages "  # nosec B608
                f"WHERE business_id=? AND event_id=? AND stage=? "
                f"AND slot_key NOT IN ({placeholders})",
                (actor.business_id, normalized, stage.value, *keys),
            )
        return max(0, int(getattr(cursor, "rowcount", 0) or 0))



_ASSET_COLUMNS = (
    "business_id,event_id,stage,slot_key,kind,media_reference,source,source_ref,revision,"
    "updated_by_member_id,created_at,updated_at"
)


def _asset_from_row(row: Any) -> EventContentAsset:
    return EventContentAsset(
        business_id=str(_value(row, "business_id", 0)),
        event_id=str(_value(row, "event_id", 1)),
        stage=EventContentStage(str(_value(row, "stage", 2))),
        slot_key=str(_value(row, "slot_key", 3)),
        kind=str(_value(row, "kind", 4)),
        media_reference=str(_value(row, "media_reference", 5)),
        source=str(_value(row, "source", 6)),
        source_ref=str(_value(row, "source_ref", 7)),
        revision=int(_value(row, "revision", 8)),
        updated_by_member_id=str(_value(row, "updated_by_member_id", 9)),
        created_at=str(_value(row, "created_at", 10)),
        updated_at=str(_value(row, "updated_at", 11)),
    )


class EventContentAssetRepository:
    def __init__(self, conn: Any):
        self._conn = conn

    def _event_exists(self, *, actor: TenantContext, event_id: str) -> str:
        actor.assert_can_manage_business()
        normalized = normalize_uuid(event_id, field_name="event_id")
        row = self._conn.execute(
            "SELECT id FROM clientplatform_events WHERE id=? AND business_id=? LIMIT 1",
            (normalized, actor.business_id),
        ).fetchone()
        if row is None:
            raise ValueError("event was not found in the active business")
        return normalized

    def get(
        self,
        *,
        actor: TenantContext,
        event_id: str,
        stage: EventContentStage,
        slot_key: str,
    ) -> EventContentAsset | None:
        normalized = self._event_exists(actor=actor, event_id=event_id)
        key = _slot_key(slot_key)
        row = self._conn.execute(
            f"SELECT {_ASSET_COLUMNS} FROM clientplatform_event_content_assets "
            "WHERE business_id=? AND event_id=? AND stage=? AND slot_key=? LIMIT 1",
            (actor.business_id, normalized, stage.value, key),
        ).fetchone()
        return None if row is None else _asset_from_row(row)

    def upsert(
        self,
        *,
        actor: TenantContext,
        event_id: str,
        stage: EventContentStage,
        slot_key: str,
        kind: str,
        media_reference: str,
        source: str,
        source_ref: str = "",
        now: str | None = None,
    ) -> EventContentAsset:
        normalized = self._event_exists(actor=actor, event_id=event_id)
        key = _slot_key(slot_key)
        normalized_kind = str(kind or "").strip().lower()
        if normalized_kind not in {"image", "video"}:
            raise ValueError("event content asset kind is invalid")
        reference = str(media_reference or "").strip()
        if not 1 <= len(reference) <= 2048 or any(ord(char) < 32 for char in reference):
            raise ValueError("event content asset reference is invalid")
        normalized_source = str(source or "").strip().lower()
        if normalized_source not in {"owner", "generated"}:
            raise ValueError("event content asset source is invalid")
        normalized_source_ref = str(source_ref or "").strip()
        if len(normalized_source_ref) > 200:
            raise ValueError("event content asset source reference is invalid")
        timestamp = str(now or _utc_now())
        self._conn.execute(
            """
            INSERT INTO clientplatform_event_content_assets(
                business_id,event_id,stage,slot_key,kind,media_reference,source,source_ref,
                revision,updated_by_member_id,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,1,?,?,?)
            ON CONFLICT(business_id,event_id,stage,slot_key) DO UPDATE SET
                kind=excluded.kind,
                media_reference=excluded.media_reference,
                source=excluded.source,
                source_ref=excluded.source_ref,
                revision=clientplatform_event_content_assets.revision + 1,
                updated_by_member_id=excluded.updated_by_member_id,
                updated_at=excluded.updated_at
            """,
            (
                actor.business_id,
                normalized,
                stage.value,
                key,
                normalized_kind,
                reference,
                normalized_source,
                normalized_source_ref,
                actor.membership_id,
                timestamp,
                timestamp,
            ),
        )
        stored = self.get(
            actor=actor,
            event_id=normalized,
            stage=stage,
            slot_key=key,
        )
        if stored is None:
            raise RuntimeError("event content asset was not persisted")
        return stored

__all__ = [
    "EventContentAssetRepository",
    "EventContentMessageRepository",
    "EventContentPreferenceRepository",
]
