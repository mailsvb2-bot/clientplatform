from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clientplatform.application.automation_policy import set_owner_event_autosend_policy_in_conn
from clientplatform.domain.event_followup import EVENT_FOLLOWUP_CHANNELS, EVENT_FOLLOWUP_SEGMENTS
from clientplatform.domain.tenancy import PlatformRole, TenantContext, TenantPermissionDenied
from clientplatform.infrastructure.event_followup_settings_repository import (
    EventFollowupSettings,
    EventFollowupSettingsRepository,
    event_followups_enabled_in_conn,
    event_followups_platform_enabled,
)
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db import get_db, get_db_ro, tx


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return current.astimezone(timezone.utc)


def _cancel_pending_followups(
    conn: Any,
    *,
    business_id: str,
    timestamp: str,
) -> int:
    cursor = conn.execute(
        """
        UPDATE provider_dispatch_outbox
        SET status='cancelled',updated_at=?,locked_at=NULL,lock_token=NULL,
            last_error='event_commercial_business_disabled'
        WHERE business_id=? AND source_kind='event_message'
          AND status IN ('pending','retry')
          AND idempotency_key LIKE 'event:%:message:post:v4:stage:%'
        """,
        (timestamp, business_id),
    )
    return max(0, int(getattr(cursor, "rowcount", 0) or 0))


def _cancel_pending_for_channel(
    conn: Any,
    *,
    business_id: str,
    channel: str,
    timestamp: str,
) -> int:
    cursor = conn.execute(
        """
        UPDATE provider_dispatch_outbox
        SET status='cancelled',updated_at=?,locked_at=NULL,lock_token=NULL,
            last_error='event_commercial_strategy_disabled'
        WHERE business_id=? AND source_kind='event_message' AND platform=?
          AND status IN ('pending','retry')
          AND idempotency_key LIKE 'event:%:message:post:v4:stage:%'
        """,
        (timestamp, business_id, channel),
    )
    return max(0, int(getattr(cursor, "rowcount", 0) or 0))


def _cancel_pending_for_segment(
    conn: Any,
    *,
    business_id: str,
    segment: str,
    timestamp: str,
) -> int:
    segment_predicates = {
        "offer_clicked_unpaid": "r.offer_clicked_at IS NOT NULL",
        "attended_unpaid": (
            "r.offer_clicked_at IS NULL AND r.attendance_confirmed_at IS NOT NULL"
        ),
        "join_signal_unpaid": (
            "r.offer_clicked_at IS NULL AND r.attendance_confirmed_at IS NULL "
            "AND r.first_join_click_at IS NOT NULL"
        ),
        "no_show": (
            "r.offer_clicked_at IS NULL AND r.attendance_confirmed_at IS NULL "
            "AND r.first_join_click_at IS NULL"
        ),
    }
    predicate = segment_predicates.get(segment)
    if predicate is None:
        raise ValueError("Неизвестная группа получателей")
    cursor = conn.execute(
        f"""
        UPDATE provider_dispatch_outbox AS d
        SET status='cancelled',updated_at=?,locked_at=NULL,lock_token=NULL,
            last_error='event_commercial_strategy_disabled'
        WHERE d.business_id=? AND d.source_kind='event_message'
          AND d.status IN ('pending','retry')
          AND d.idempotency_key LIKE 'event:%:message:post:v4:stage:%'
          AND EXISTS (
              SELECT 1 FROM clientplatform_event_registrations r
              WHERE r.id=d.source_id AND r.business_id=d.business_id
                AND ({predicate})
          )
        """,  # nosec B608 -- predicate comes only from the static mapping above.
        (timestamp, business_id),
    )
    return max(0, int(getattr(cursor, "rowcount", 0) or 0))


def _audit(
    conn: Any,
    *,
    actor: TenantContext,
    enabled: bool,
    settings_epoch: int,
    timestamp: str,
) -> None:
    conn.execute(
        """
        INSERT INTO clientplatform_admin_audit_events(
            id,business_id,actor_user_id,action,subject_type,subject_id,detail,created_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            str(uuid4()),
            actor.business_id,
            actor.user_id,
            "event_commercial_followups_enabled" if enabled else "event_commercial_followups_disabled",
            "event_followup_settings",
            actor.business_id,
            f"enabled={int(enabled)};epoch={settings_epoch}",
            timestamp,
        ),
    )


def _audit_strategy(
    conn: Any,
    *,
    actor: TenantContext,
    kind: str,
    key: str,
    enabled: bool,
    settings_epoch: int,
    timestamp: str,
) -> None:
    conn.execute(
        """
        INSERT INTO clientplatform_admin_audit_events(
            id,business_id,actor_user_id,action,subject_type,subject_id,detail,created_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            str(uuid4()),
            actor.business_id,
            actor.user_id,
            f"event_commercial_followup_{kind}_changed",
            "event_followup_settings",
            actor.business_id,
            f"{kind}={key};enabled={int(enabled)};epoch={settings_epoch}",
            timestamp,
        ),
    )


def get_business_event_followup_settings(*, actor: TenantContext) -> EventFollowupSettings | None:
    with get_db_ro() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        return EventFollowupSettingsRepository(conn).get(business_id=current.business_id)


def get_business_event_followups_enabled(*, actor: TenantContext) -> bool:
    with get_db_ro() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        return event_followups_enabled_in_conn(conn, business_id=current.business_id)


def set_business_event_followups_enabled(
    *,
    actor: TenantContext,
    enabled: bool,
    now: datetime | None = None,
) -> bool:
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be boolean")
    timestamp_dt = _now(now)
    timestamp = timestamp_dt.isoformat(timespec="seconds")
    with get_db() as conn:
        with tx(conn):
            current = TenancyRepository(conn).resolve_context(
                user_id=actor.user_id,
                business_id=actor.business_id,
            )
            current.assert_can_manage_business()
            repository = EventFollowupSettingsRepository(conn)
            existing = repository.get(business_id=current.business_id)
            if enabled:
                if current.role != PlatformRole.OWNER:
                    raise TenantPermissionDenied(
                        "включить автоматические сообщения участникам вебинара может только владелец"
                    )
                if not event_followups_platform_enabled():
                    raise ValueError("Автосерия временно отключена на уровне платформы")
                if existing is not None and not existing.enabled_segments:
                    raise ValueError("Выберите хотя бы одну группу участников вебинара")
                if existing is not None and not existing.enabled_channels:
                    raise ValueError("Выберите хотя бы один канал для сообщений")
                selected_channels = (
                    existing.enabled_channels
                    if existing is not None
                    else EVENT_FOLLOWUP_CHANNELS
                )
                set_owner_event_autosend_policy_in_conn(
                    conn,
                    actor=current,
                    allowed_channels=selected_channels,
                    now=timestamp_dt,
                )

            settings = repository.set_enabled(
                business_id=current.business_id,
                enabled=enabled,
                updated_by_member_id=current.membership_id,
                now=timestamp,
            )
            if not enabled:
                _cancel_pending_followups(
                    conn,
                    business_id=current.business_id,
                    timestamp=timestamp,
                )
            _audit(
                conn,
                actor=current,
                enabled=enabled,
                settings_epoch=settings.settings_epoch,
                timestamp=timestamp,
            )
    return enabled


def set_business_event_followup_segment_enabled(
    *,
    actor: TenantContext,
    segment: str,
    enabled: bool,
    now: datetime | None = None,
) -> EventFollowupSettings:
    key = str(segment or "").strip().lower()
    if key not in EVENT_FOLLOWUP_SEGMENTS:
        raise ValueError("Неизвестная группа получателей")
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be boolean")
    timestamp = _now(now).isoformat(timespec="seconds")
    with get_db() as conn:
        with tx(conn):
            current = TenancyRepository(conn).resolve_context(
                user_id=actor.user_id,
                business_id=actor.business_id,
            )
            current.assert_can_manage_business()
            if enabled and current.role != PlatformRole.OWNER:
                raise TenantPermissionDenied("расширить группы автосообщений может только владелец")
            repository = EventFollowupSettingsRepository(conn)
            existing = repository.get(business_id=current.business_id)
            if (
                not enabled
                and existing is not None
                and existing.enabled
                and key in existing.enabled_segments
                and len(existing.enabled_segments) == 1
            ):
                raise ValueError("Для включённых автосообщений нужна хотя бы одна группа получателей")
            settings = repository.set_segment(
                business_id=current.business_id,
                segment=key,
                enabled=enabled,
                updated_by_member_id=current.membership_id,
                now=timestamp,
            )
            if not enabled:
                _cancel_pending_for_segment(
                    conn,
                    business_id=current.business_id,
                    segment=key,
                    timestamp=timestamp,
                )
            _audit_strategy(
                conn,
                actor=current,
                kind="segment",
                key=key,
                enabled=enabled,
                settings_epoch=settings.settings_epoch,
                timestamp=timestamp,
            )
            return settings


def set_business_event_followup_channel_enabled(
    *,
    actor: TenantContext,
    channel: str,
    enabled: bool,
    now: datetime | None = None,
) -> EventFollowupSettings:
    key = str(channel or "").strip().lower()
    if key not in EVENT_FOLLOWUP_CHANNELS:
        raise ValueError("Неизвестный канал автосообщений")
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be boolean")
    timestamp_dt = _now(now)
    timestamp = timestamp_dt.isoformat(timespec="seconds")
    with get_db() as conn:
        with tx(conn):
            current = TenancyRepository(conn).resolve_context(
                user_id=actor.user_id,
                business_id=actor.business_id,
            )
            current.assert_can_manage_business()
            if enabled and current.role != PlatformRole.OWNER:
                raise TenantPermissionDenied("расширить каналы автосообщений может только владелец")
            repository = EventFollowupSettingsRepository(conn)
            existing = repository.get(business_id=current.business_id)
            if (
                not enabled
                and existing is not None
                and existing.enabled
                and key in existing.enabled_channels
                and len(existing.enabled_channels) == 1
            ):
                raise ValueError("Для включённых автосообщений нужен хотя бы один канал")
            settings = repository.set_channel(
                business_id=current.business_id,
                channel=key,
                enabled=enabled,
                updated_by_member_id=current.membership_id,
                now=timestamp,
            )
            if not enabled:
                _cancel_pending_for_channel(
                    conn,
                    business_id=current.business_id,
                    channel=key,
                    timestamp=timestamp,
                )
            if settings.enabled and current.role == PlatformRole.OWNER:
                set_owner_event_autosend_policy_in_conn(
                    conn,
                    actor=current,
                    allowed_channels=settings.enabled_channels,
                    now=timestamp_dt,
                )
            _audit_strategy(
                conn,
                actor=current,
                kind="channel",
                key=key,
                enabled=enabled,
                settings_epoch=settings.settings_epoch,
                timestamp=timestamp,
            )
            return settings


def toggle_business_event_followups(*, actor: TenantContext) -> bool:
    current = get_business_event_followup_settings(actor=actor)
    return set_business_event_followups_enabled(
        actor=actor,
        enabled=not bool(current and current.enabled),
    )


__all__ = [
    "get_business_event_followup_settings",
    "get_business_event_followups_enabled",
    "set_business_event_followup_channel_enabled",
    "set_business_event_followup_segment_enabled",
    "set_business_event_followups_enabled",
    "toggle_business_event_followups",
]
