from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from typing import Any

from clientplatform.domain.event_followup import EVENT_FOLLOWUP_CHANNELS, EVENT_FOLLOWUP_SEGMENTS

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
_SEGMENT_COLUMNS = {
    "no_show": "segment_no_show",
    "join_signal_unpaid": "segment_join_signal",
    "attended_unpaid": "segment_attended",
    "offer_clicked_unpaid": "segment_offer_clicked",
}
_CHANNEL_COLUMNS = {
    "email": "channel_email",
    "max": "channel_max",
    "vk": "channel_vk",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def event_followups_platform_enabled() -> bool:
    raw = os.getenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED")
    if raw is None or not raw.strip():
        return True
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return False


@dataclass(frozen=True, slots=True)
class EventFollowupSettings:
    business_id: str
    enabled: bool
    enabled_segments: tuple[str, ...]
    enabled_channels: tuple[str, ...]
    settings_epoch: int
    updated_by_member_id: str
    created_at: str
    updated_at: str

    def segment_enabled(self, segment: str) -> bool:
        return str(segment) in self.enabled_segments

    def channel_enabled(self, channel: str) -> bool:
        return str(channel) in self.enabled_channels


class EventFollowupSettingsRepository:
    """Durable tenant-owned switch plus event follow-up strategy."""

    def __init__(self, conn: Any):
        self._conn = conn

    def get(self, *, business_id: str) -> EventFollowupSettings | None:
        business = str(business_id or "").strip()
        if not business:
            raise ValueError("business_id is required")
        row = self._conn.execute(
            """
            SELECT business_id,enabled,
                   segment_no_show,segment_join_signal,segment_attended,segment_offer_clicked,
                   channel_email,channel_max,channel_vk,
                   settings_epoch,updated_by_member_id,created_at,updated_at
            FROM clientplatform_event_followup_settings
            WHERE business_id=? LIMIT 1
            """,
            (business,),
        ).fetchone()
        if row is None:
            return None
        enabled_segments = tuple(
            key for index, key in enumerate(EVENT_FOLLOWUP_SEGMENTS, start=2)
            if bool(_value(row, _SEGMENT_COLUMNS[key], index))
        )
        enabled_channels = tuple(
            key for index, key in enumerate(EVENT_FOLLOWUP_CHANNELS, start=6)
            if bool(_value(row, _CHANNEL_COLUMNS[key], index))
        )
        return EventFollowupSettings(
            business_id=str(_value(row, "business_id", 0)),
            enabled=bool(_value(row, "enabled", 1)),
            enabled_segments=enabled_segments,
            enabled_channels=enabled_channels,
            settings_epoch=int(_value(row, "settings_epoch", 9)),
            updated_by_member_id=str(_value(row, "updated_by_member_id", 10)),
            created_at=str(_value(row, "created_at", 11)),
            updated_at=str(_value(row, "updated_at", 12)),
        )

    def set_enabled(
        self,
        *,
        business_id: str,
        enabled: bool,
        updated_by_member_id: str,
        now: str | None = None,
    ) -> EventFollowupSettings:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        business = str(business_id or "").strip()
        member = str(updated_by_member_id or "").strip()
        if not business or not member:
            raise ValueError("business_id and member_id are required")
        timestamp = str(now or _utc_now())
        self._conn.execute(
            """
            INSERT INTO clientplatform_event_followup_settings(
                business_id,enabled,settings_epoch,updated_by_member_id,created_at,updated_at
            ) VALUES(?,?,1,?,?,?)
            ON CONFLICT(business_id) DO UPDATE SET
                enabled=excluded.enabled,
                settings_epoch=clientplatform_event_followup_settings.settings_epoch+1,
                updated_by_member_id=excluded.updated_by_member_id,
                updated_at=excluded.updated_at
            """,
            (business, 1 if enabled else 0, member, timestamp, timestamp),
        )
        settings = self.get(business_id=business)
        if settings is None:
            raise RuntimeError("event follow-up settings were not persisted")
        return settings

    def set_segment(
        self,
        *,
        business_id: str,
        segment: str,
        enabled: bool,
        updated_by_member_id: str,
        now: str | None = None,
    ) -> EventFollowupSettings:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        key = str(segment or "").strip().lower()
        column = _SEGMENT_COLUMNS.get(key)
        if column is None:
            raise ValueError("unsupported event follow-up segment")
        return self._set_strategy_flag(
            business_id=business_id,
            column=column,
            enabled=enabled,
            updated_by_member_id=updated_by_member_id,
            now=now,
        )

    def set_channel(
        self,
        *,
        business_id: str,
        channel: str,
        enabled: bool,
        updated_by_member_id: str,
        now: str | None = None,
    ) -> EventFollowupSettings:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        key = str(channel or "").strip().lower()
        column = _CHANNEL_COLUMNS.get(key)
        if column is None:
            raise ValueError("unsupported event follow-up channel")
        return self._set_strategy_flag(
            business_id=business_id,
            column=column,
            enabled=enabled,
            updated_by_member_id=updated_by_member_id,
            now=now,
        )

    def _set_strategy_flag(
        self,
        *,
        business_id: str,
        column: str,
        enabled: bool,
        updated_by_member_id: str,
        now: str | None,
    ) -> EventFollowupSettings:
        business = str(business_id or "").strip()
        member = str(updated_by_member_id or "").strip()
        if not business or not member:
            raise ValueError("business_id and member_id are required")
        timestamp = str(now or _utc_now())
        if column not in set(_SEGMENT_COLUMNS.values()) | set(_CHANNEL_COLUMNS.values()):
            raise ValueError("unsupported event follow-up strategy column")
        self._conn.execute(
            f"INSERT INTO clientplatform_event_followup_settings("  # nosec B608
            f"business_id,enabled,{column},settings_epoch,updated_by_member_id,created_at,updated_at) "
            f"VALUES(?,0,?,1,?,?,?) "
            f"ON CONFLICT(business_id) DO UPDATE SET {column}=excluded.{column}, "
            "settings_epoch=clientplatform_event_followup_settings.settings_epoch+1, "
            "updated_by_member_id=excluded.updated_by_member_id,updated_at=excluded.updated_at",
            (business, 1 if enabled else 0, member, timestamp, timestamp),
        )
        settings = self.get(business_id=business)
        if settings is None:
            raise RuntimeError("event follow-up settings were not persisted")
        return settings

    def enabled(self, *, business_id: str) -> bool:
        settings = self.get(business_id=business_id)
        return bool(settings and settings.enabled)


def event_followups_enabled_in_conn(conn: Any, *, business_id: str) -> bool:
    if not event_followups_platform_enabled():
        return False
    return EventFollowupSettingsRepository(conn).enabled(business_id=business_id)


__all__ = [
    "EVENT_FOLLOWUP_CHANNELS",
    "EVENT_FOLLOWUP_SEGMENTS",
    "EventFollowupSettings",
    "EventFollowupSettingsRepository",
    "event_followups_enabled_in_conn",
    "event_followups_platform_enabled",
]
