from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from clientplatform.application.event_commercial_consent import (
    active_event_commercial_channels,
)
from clientplatform.application.event_delivery_targets import (
    EventDeliveryTarget,
    resolve_event_email_target,
    resolve_event_messenger_target,
)
from clientplatform.application.event_sessions import (
    validate_warmup_days,
    warmup_window_for_sessions,
)
from clientplatform.domain.email_outbound import EmailPayload
from clientplatform.domain.event_content import EventContentStage
from clientplatform.domain.events import Event, EventRegistration, normalize_utc
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.event_content_repository import (
    EventContentMessageRepository,
)
from clientplatform.infrastructure.event_dispatch_safety import (
    event_commercial_policy_authorized,
)
from clientplatform.infrastructure.event_followup_settings_repository import (
    EventFollowupSettings,
    EventFollowupSettingsRepository,
    event_followups_platform_enabled,
)
from clientplatform.infrastructure.event_repository import EventRepository
from clientplatform.infrastructure.event_session_repository import EventSessionRepository
from services.db import get_db, get_db_ro


_DEFAULT_PRIORITY = ("max", "vk", "telegram", "email")
_ALLOWED_PRIORITY = frozenset({"max", "vk", "telegram", "email"})
_WARMUP_GRACE = timedelta(hours=18)


@dataclass(frozen=True, slots=True)
class EventWarmupDraft:
    position: int
    slot_key: str
    publish_date: date
    scheduled_at: datetime
    days_before_event: int
    text: str
    source: str = "template"
    revision: int = 0


@dataclass(frozen=True, slots=True)
class EventWarmupPlan:
    event_id: str
    timezone_name: str
    days_until_event: int
    requested_days: int
    drafts: tuple[EventWarmupDraft, ...]


@dataclass(frozen=True, slots=True)
class EventWarmupBatchResult:
    scanned: int
    queued: int
    expired: int
    no_consent: int
    no_route: int
    policy_blocked: int
    settings_disabled: int


def _clean(value: object, *, limit: int) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _program_topics(description: str) -> tuple[str, ...]:
    """Read canonical day topics from the event description without a second store."""

    lines = [line.strip() for line in str(description or "").splitlines() if line.strip()]
    if not lines or lines[0].casefold() != "программа по дням:":
        return ()
    topics: list[str] = []
    for line in lines[1:]:
        _prefix, separator, topic = line.partition(":")
        if not separator:
            continue
        clean = _clean(topic, limit=180)
        if clean:
            topics.append(clean)
    return tuple(topics)


def _safe_warmup_text(
    *,
    title: str,
    description: str,
    days_before_event: int,
    position: int,
    total: int,
) -> str:
    clean_title = _clean(title, limit=240)
    clean_description = _clean(description, limit=700)
    topics = _program_topics(description)
    if days_before_event == 1:
        opener = "Уже завтра"
    elif days_before_event == 2:
        opener = "До вебинара осталось 2 дня"
    else:
        opener = f"До вебинара осталось {days_before_event} дн."

    focus = ""
    if topics:
        # Spread a long pre-event series across the actual day themes.  A
        # two-month campaign therefore changes focus instead of repeating one
        # generic paragraph for 60 days.
        topic_index = min(
            len(topics) - 1,
            ((max(position, 1) - 1) * len(topics)) // max(total, 1),
        )
        focus = topics[topic_index]

    if position == 1 and total > 1:
        angle = (
            f"Начинаем с темы «{focus}»: посмотрите, где она уже проявляется в Вашей ситуации."
            if focus
            else "Начинаем знакомство с темой и отмечаем главное, с чем будем работать на эфире."
        )
    elif position == total:
        angle = (
            f"Перед эфиром вернитесь к теме «{focus}» и запишите один вопрос, который хотите разобрать."
            if focus
            else "Перед стартом выберите один главный вопрос, который хотите разобрать на эфире."
        )
    elif focus:
        phase = (position - 1) % 3
        angle = (
            f"Сегодня в центре внимания тема «{focus}». Отметьте один пример из своей практики или жизни."
            if phase == 0
            else (
                f"Продолжаем тему «{focus}». Подумайте, что в ней сейчас вызывает больше всего вопросов."
                if phase == 1
                else f"Тема дня — «{focus}». Сформулируйте один результат, который хотите получить на вебинаре."
            )
        )
    else:
        angle = "Возвращаемся к теме и постепенно готовимся применить материал на вебинаре."

    parts = [f"🔥 {opener}: «{clean_title}».", "", angle]
    if clean_description and not topics:
        parts.extend(["", clean_description])
    parts.extend(
        [
            "",
            "Ваша регистрация уже подтверждена. Персональная ссылка на эфир — ниже.",
        ]
    )
    return "\n".join(parts)


def _scheduled_at(*, publish_date: date, timezone_name: str) -> datetime:
    zone = ZoneInfo(str(timezone_name))
    return datetime.combine(publish_date, time(hour=12), tzinfo=zone).astimezone(
        timezone.utc
    )


def _slot_key(days_before_event: int) -> str:
    return f"before:{int(days_before_event)}"


def build_event_warmup_plan(
    *,
    event_id: str,
    title: str,
    description: str,
    timezone_name: str,
    first_session_starts_at: datetime,
    requested_days: int,
    now: datetime | None = None,
) -> EventWarmupPlan:
    current = normalize_utc(now or datetime.now(timezone.utc), field_name="now")
    zone = ZoneInfo(str(timezone_name))
    first_local_date = first_session_starts_at.astimezone(zone).date()
    current_local_date = current.astimezone(zone).date()
    days_until_event = max((first_local_date - current_local_date).days, 0)
    selected = validate_warmup_days(
        requested_days,
        max_warmup_days=days_until_event,
    )
    if selected == 0:
        return EventWarmupPlan(
            event_id=event_id,
            timezone_name=str(timezone_name),
            days_until_event=days_until_event,
            requested_days=0,
            drafts=(),
        )

    first_publish_date = first_local_date - timedelta(days=selected)
    drafts = tuple(
        EventWarmupDraft(
            position=index + 1,
            slot_key=_slot_key(selected - index),
            publish_date=first_publish_date + timedelta(days=index),
            scheduled_at=_scheduled_at(
                publish_date=first_publish_date + timedelta(days=index),
                timezone_name=timezone_name,
            ),
            days_before_event=selected - index,
            text=_safe_warmup_text(
                title=title,
                description=description,
                days_before_event=selected - index,
                position=index + 1,
                total=selected,
            ),
        )
        for index in range(selected)
    )
    return EventWarmupPlan(
        event_id=event_id,
        timezone_name=str(timezone_name),
        days_until_event=days_until_event,
        requested_days=selected,
        drafts=drafts,
    )


def _base_plan(
    *,
    actor: TenantContext,
    event_id: str,
    requested_days: int,
    now: datetime | None,
) -> EventWarmupPlan:
    with get_db_ro() as conn:
        event = EventRepository(conn).get(actor=actor, event_id=event_id)
        actor.assert_can_manage_business()
        sessions = EventSessionRepository(conn).list_for_event(
            actor=actor,
            event_id=event.id,
        )
    window = warmup_window_for_sessions(
        sessions,
        timezone_name=event.timezone_name,
        now=now,
    )
    validate_warmup_days(
        requested_days,
        max_warmup_days=window.max_warmup_days,
    )
    return build_event_warmup_plan(
        event_id=event.id,
        title=event.title,
        description=event.description,
        timezone_name=event.timezone_name,
        first_session_starts_at=sessions[0].starts_at,
        requested_days=requested_days,
        now=now,
    )


def get_event_warmup_plan(
    *,
    actor: TenantContext,
    event_id: str,
    requested_days: int,
    now: datetime | None = None,
) -> EventWarmupPlan:
    base = _base_plan(
        actor=actor,
        event_id=event_id,
        requested_days=requested_days,
        now=now,
    )
    if not base.drafts:
        return base
    with get_db_ro() as conn:
        stored = EventContentMessageRepository(conn).list_for_stage(
            actor=actor,
            event_id=base.event_id,
            stage=EventContentStage.WARMUP,
        )
    by_key = {item.slot_key: item for item in stored}
    drafts: list[EventWarmupDraft] = []
    for draft in base.drafts:
        item = by_key.get(draft.slot_key)
        if item is None:
            drafts.append(draft)
            continue
        scheduled = (
            draft.scheduled_at
            if item.scheduled_at is None
            else normalize_utc(item.scheduled_at, field_name="scheduled_at")
        )
        drafts.append(
            replace(
                draft,
                scheduled_at=scheduled,
                text=item.text,
                source=item.source,
                revision=item.revision,
            )
        )
    return replace(base, drafts=tuple(drafts))


def save_event_warmup_plan(
    *,
    actor: TenantContext,
    event_id: str,
    requested_days: int,
    now: datetime | None = None,
) -> EventWarmupPlan:
    actor.assert_can_manage_business()
    base = _base_plan(
        actor=actor,
        event_id=event_id,
        requested_days=requested_days,
        now=now,
    )
    with get_db() as conn:
        repository = EventContentMessageRepository(conn)
        existing = {
            item.slot_key: item
            for item in repository.list_for_stage(
                actor=actor,
                event_id=base.event_id,
                stage=EventContentStage.WARMUP,
            )
        }
        keep: list[str] = []
        for draft in base.drafts:
            key = draft.slot_key
            keep.append(key)
            current = existing.get(key)
            body = draft.text
            source = "template"
            if current is not None and current.source == "owner":
                body = current.text
                source = "owner"
            repository.upsert(
                actor=actor,
                event_id=base.event_id,
                stage=EventContentStage.WARMUP,
                slot_key=key,
                position=draft.position,
                text=body,
                source=source,
                scheduled_at=draft.scheduled_at.isoformat(),
            )
        repository.delete_missing_slots(
            actor=actor,
            event_id=base.event_id,
            stage=EventContentStage.WARMUP,
            keep_slot_keys=tuple(keep),
        )
    return get_event_warmup_plan(
        actor=actor,
        event_id=base.event_id,
        requested_days=requested_days,
        now=now,
    )


def get_saved_event_warmup_plan(
    *,
    actor: TenantContext,
    event_id: str,
    now: datetime | None = None,
) -> EventWarmupPlan:
    """Read the persisted schedule without revalidating it against today's remaining days."""

    current = normalize_utc(now or datetime.now(timezone.utc), field_name="now")
    with get_db_ro() as conn:
        event = EventRepository(conn).get(actor=actor, event_id=event_id)
        actor.assert_can_manage_business()
        sessions = EventSessionRepository(conn).list_for_event(
            actor=actor,
            event_id=event.id,
        )
        messages = EventContentMessageRepository(conn).list_for_stage(
            actor=actor,
            event_id=event.id,
            stage=EventContentStage.WARMUP,
        )
    window = warmup_window_for_sessions(
        sessions,
        timezone_name=event.timezone_name,
        now=current,
    )
    zone = ZoneInfo(event.timezone_name)
    first_local_date = sessions[0].starts_at.astimezone(zone).date()
    drafts: list[EventWarmupDraft] = []
    for item in messages:
        if item.scheduled_at is None:
            continue
        scheduled = normalize_utc(item.scheduled_at, field_name="scheduled_at")
        publish_date = scheduled.astimezone(zone).date()
        drafts.append(
            EventWarmupDraft(
                position=item.position,
                slot_key=item.slot_key,
                publish_date=publish_date,
                scheduled_at=scheduled,
                days_before_event=max((first_local_date - publish_date).days, 0),
                text=item.text,
                source=item.source,
                revision=item.revision,
            )
        )
    return EventWarmupPlan(
        event_id=event.id,
        timezone_name=event.timezone_name,
        days_until_event=window.days_until_event,
        requested_days=len(drafts),
        drafts=tuple(drafts),
    )


def set_event_warmup_text(
    *,
    actor: TenantContext,
    event_id: str,
    position: int,
    text: str,
) -> EventWarmupDraft:
    actor.assert_can_manage_business()
    with get_db() as conn:
        repository = EventContentMessageRepository(conn)
        messages = repository.list_for_stage(
            actor=actor,
            event_id=event_id,
            stage=EventContentStage.WARMUP,
        )
        current = next((item for item in messages if item.position == position), None)
        if current is None:
            raise ValueError("сначала выберите длительность серии сообщений")
        repository.upsert(
            actor=actor,
            event_id=event_id,
            stage=EventContentStage.WARMUP,
            slot_key=current.slot_key,
            position=current.position,
            text=text,
            source="owner",
            scheduled_at=current.scheduled_at,
        )
    plan = get_saved_event_warmup_plan(
        actor=actor,
        event_id=event_id,
    )
    for draft in plan.drafts:
        if draft.position == position:
            return draft
    raise ValueError("сообщение для этой позиции не найдено")


def reset_event_warmup_text(
    *,
    actor: TenantContext,
    event_id: str,
    position: int,
) -> EventWarmupDraft:
    actor.assert_can_manage_business()
    with get_db_ro() as conn:
        event = EventRepository(conn).get(actor=actor, event_id=event_id)
        sessions = EventSessionRepository(conn).list_for_event(
            actor=actor,
            event_id=event.id,
        )
        messages = EventContentMessageRepository(conn).list_for_stage(
            actor=actor,
            event_id=event.id,
            stage=EventContentStage.WARMUP,
        )
    current = next((item for item in messages if item.position == position), None)
    if current is None or current.scheduled_at is None:
        raise ValueError("сообщение для этой позиции не найдено")
    scheduled = normalize_utc(current.scheduled_at, field_name="scheduled_at")
    zone = ZoneInfo(event.timezone_name)
    publish_date = scheduled.astimezone(zone).date()
    first_local_date = sessions[0].starts_at.astimezone(zone).date()
    days_before_event = max((first_local_date - publish_date).days, 0)
    body = _safe_warmup_text(
        title=event.title,
        description=event.description,
        days_before_event=days_before_event,
        position=position,
        total=len(messages),
    )
    with get_db() as conn:
        updated = EventContentMessageRepository(conn).upsert(
            actor=actor,
            event_id=event.id,
            stage=EventContentStage.WARMUP,
            slot_key=current.slot_key,
            position=position,
            text=body,
            source="template",
            scheduled_at=scheduled.isoformat(),
        )
    return EventWarmupDraft(
        position=position,
        slot_key=current.slot_key,
        publish_date=publish_date,
        scheduled_at=scheduled,
        days_before_event=days_before_event,
        text=body,
        source="template",
        revision=updated.revision,
    )


def _public_base_url() -> str:
    from config.settings import settings

    value = str(getattr(settings, "MESSENGER_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if not value.startswith("https://"):
        raise RuntimeError("MESSENGER_PUBLIC_BASE_URL must be configured as HTTPS")
    return value


def _channel_priority() -> tuple[str, ...]:
    raw = str(os.getenv("CLIENTPLATFORM_EVENT_FOLLOWUP_CHANNEL_PRIORITY") or "").strip()
    if not raw:
        return _DEFAULT_PRIORITY
    result: list[str] = []
    for part in raw.split(","):
        value = part.strip().lower()
        if value in _ALLOWED_PRIORITY and value not in result:
            result.append(value)
    return tuple(result) if result else _DEFAULT_PRIORITY


def _resolve_target(
    conn: Any,
    *,
    event: Event,
    registration: EventRegistration,
    channels: tuple[str, ...],
) -> EventDeliveryTarget | None:
    allowed = set(channels)
    for platform in _channel_priority():
        if platform not in allowed:
            continue
        if platform == "email":
            target = resolve_event_email_target(
                conn,
                event=event,
                registration=registration,
            )
        else:
            target = resolve_event_messenger_target(
                conn,
                event=event,
                registration=registration,
                platform=platform,
            )
        if target is not None:
            return target
    return None


def _join_url(registration: EventRegistration) -> str:
    return f"{_public_base_url()}/e/join/{registration.token}"


def _render_for_registration(
    *,
    text: str,
    event: Event,
    registration: EventRegistration,
) -> tuple[str, str]:
    join = _join_url(registration)
    body = str(text)
    body = body.replace("{name}", registration.name)
    body = body.replace("{title}", event.title)
    body = body.replace("{join_url}", join)
    if join not in body:
        body = f"{body}\n\nВойти в эфир: {join}"
    subject = f"До вебинара «{event.title}»"
    return subject, body


def _dispatch_key(
    *,
    event_id: str,
    registration_id: str,
    slot_key: str,
    revision: int,
) -> str:
    normalized_slot = str(slot_key).replace(":", "-")
    return (
        f"event:{event_id}:registration:{registration_id}:"
        f"message:warmup:v2:slot:{normalized_slot}:revision:{int(revision)}"
    )


def _active_visual_asset(
    conn: Any,
    *,
    business_id: str,
    event_id: str,
    slot_key: str,
) -> tuple[str, str, int] | None:
    try:
        row = conn.execute(
            """
            SELECT a.kind,a.media_reference,a.revision,p.mode
            FROM clientplatform_event_content_assets a
            JOIN clientplatform_event_content_preferences p
              ON p.business_id=a.business_id AND p.event_id=a.event_id AND p.stage=a.stage
            WHERE a.business_id=? AND a.event_id=? AND a.stage='warmup' AND a.slot_key=?
            LIMIT 1
            """,
            (business_id, event_id, slot_key),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    kind = str(row["kind"] if hasattr(row, "keys") else row[0])
    reference = str(row["media_reference"] if hasattr(row, "keys") else row[1])
    revision = int(row["revision"] if hasattr(row, "keys") else row[2])
    mode = str(row["mode"] if hasattr(row, "keys") else row[3])
    valid = (
        (mode == "text_with_video" and kind == "video")
        or (mode in {"text_with_image", "text_in_image"} and kind == "image")
    )
    return (kind, reference, revision) if valid else None


def _media_dispatch_key(
    *,
    event_id: str,
    registration_id: str,
    slot_key: str,
    message_revision: int,
    asset_revision: int,
) -> str:
    normalized_slot = str(slot_key).replace(":", "-")
    return (
        f"event:{event_id}:registration:{registration_id}:"
        f"message:warmup-media:v1:slot:{normalized_slot}:"
        f"message-revision:{int(message_revision)}:asset-revision:{int(asset_revision)}"
    )


def _materialize_media(
    conn: Any,
    *,
    event: Event,
    registration: EventRegistration,
    target: EventDeliveryTarget,
    slot_key: str,
    message_revision: int,
    asset_kind: str,
    asset_reference: str,
    asset_revision: int,
    now_iso: str,
) -> bool:
    key = _media_dispatch_key(
        event_id=event.id,
        registration_id=registration.id,
        slot_key=slot_key,
        message_revision=message_revision,
        asset_revision=asset_revision,
    )
    cursor = conn.execute(
        """
        INSERT INTO provider_dispatch_outbox(
            id,business_id,platform,source_kind,source_id,
            logical_delivery_id,partner_campaign_id,partner_candidate_id,
            sales_followup_id,connection_id,recipient_kind,customer_identity_id,
            external_subject,payload_kind,payload_ref,idempotency_key,status,
            attempts,available_at,locked_at,lock_token,provider_message_id,
            last_error,created_at,updated_at,sent_at,dead_at
        ) VALUES(
            ?,?,?,'event_message',?,NULL,NULL,NULL,NULL,?,?,?,?,?,?,?,'pending',0,?,
            NULL,NULL,NULL,NULL,?,?,NULL,NULL
        )
        ON CONFLICT(business_id,idempotency_key) DO NOTHING
        """,
        (
            str(uuid4()),
            event.business_id,
            target.platform,
            registration.id,
            target.connection_id,
            target.recipient_kind,
            target.customer_identity_id,
            target.external_subject,
            asset_kind,
            asset_reference,
            key,
            now_iso,
            now_iso,
            now_iso,
        ),
    )
    return int(getattr(cursor, "rowcount", 0) or 0) == 1


def _materialize(
    conn: Any,
    *,
    event: Event,
    registration: EventRegistration,
    target: EventDeliveryTarget,
    slot_key: str,
    revision: int,
    text: str,
    now_iso: str,
    available_at: str | None = None,
) -> bool:
    subject, body = _render_for_registration(
        text=text,
        event=event,
        registration=registration,
    )
    if target.platform == "email":
        payload_kind = "mixed"
        payload_ref = EmailPayload(subject=subject, body=body).to_json()
    else:
        payload_kind = "text"
        payload_ref = body
    key = _dispatch_key(
        event_id=event.id,
        registration_id=registration.id,
        slot_key=slot_key,
        revision=revision,
    )
    cursor = conn.execute(
        """
        INSERT INTO provider_dispatch_outbox(
            id,business_id,platform,source_kind,source_id,
            logical_delivery_id,partner_campaign_id,partner_candidate_id,
            sales_followup_id,connection_id,recipient_kind,customer_identity_id,
            external_subject,payload_kind,payload_ref,idempotency_key,status,
            attempts,available_at,locked_at,lock_token,provider_message_id,
            last_error,created_at,updated_at,sent_at,dead_at
        ) VALUES(
            ?,?,?,'event_message',?,NULL,NULL,NULL,NULL,?,?,?,?,?,?,?,'pending',0,?,
            NULL,NULL,NULL,NULL,?,?,NULL,NULL
        )
        ON CONFLICT(business_id,idempotency_key) DO NOTHING
        """,
        (
            str(uuid4()),
            event.business_id,
            target.platform,
            registration.id,
            target.connection_id,
            target.recipient_kind,
            target.customer_identity_id,
            target.external_subject,
            payload_kind,
            payload_ref,
            key,
            available_at or now_iso,
            now_iso,
            now_iso,
        ),
    )
    return int(getattr(cursor, "rowcount", 0) or 0) == 1


def materialize_due_event_warmups_in_transaction(
    conn: Any,
    *,
    limit: int = 100,
    now: datetime | str | None = None,
) -> EventWarmupBatchResult:
    bounded = max(1, min(int(limit), 1000))
    current = normalize_utc(now or datetime.now(timezone.utc), field_name="now")
    now_iso = current.replace(microsecond=0).isoformat()
    if not event_followups_platform_enabled():
        return EventWarmupBatchResult(0, 0, 0, 0, 0, 0, 0)

    rows = conn.execute(
        """
        SELECT m.business_id,m.event_id,m.position,m.slot_key,m.revision,m.scheduled_at,m.text,
               e.public_slug,e.timezone_name,r.id AS registration_id,r.token
        FROM clientplatform_event_content_messages m
        JOIN clientplatform_events e
          ON e.id=m.event_id AND e.business_id=m.business_id
        JOIN clientplatform_event_registrations r
          ON r.event_id=e.id AND r.business_id=e.business_id
         AND r.status='registered'
        JOIN clientplatform_event_followup_settings s
          ON s.business_id=e.business_id AND s.enabled=1
        WHERE m.stage='warmup' AND m.scheduled_at IS NOT NULL
          AND m.scheduled_at<=? AND m.scheduled_at>=?
          AND e.status='published' AND e.starts_at>?
          AND NOT EXISTS (
              SELECT 1 FROM clientplatform_event_conversion_links p
              WHERE p.business_id=r.business_id
                AND p.event_id=r.event_id
                AND p.registration_id=r.id
          )
          AND EXISTS (
              SELECT 1 FROM clientplatform_event_commercial_channel_state cs
              WHERE cs.business_id=r.business_id
                AND cs.event_id=r.event_id
                AND cs.registration_id=r.id
                AND cs.status='active'
          )
          AND NOT EXISTS (
              SELECT 1 FROM provider_dispatch_outbox d
              WHERE d.business_id=r.business_id
                AND d.idempotency_key=(
                    'event:' || e.id || ':registration:' || r.id ||
                    ':message:warmup:v2:slot:' || REPLACE(m.slot_key, ':', '-') ||
                    ':revision:' || CAST(m.revision AS TEXT)
                )
          )
        ORDER BY m.scheduled_at,m.event_id,m.position,r.id
        LIMIT ?
        """,
        (
            now_iso,
            (current - _WARMUP_GRACE).isoformat(),
            now_iso,
            bounded,
        ),
    ).fetchall()

    scanned = queued = expired = no_consent = no_route = policy_blocked = settings_disabled = 0
    event_repository = EventRepository(conn)
    settings_repository = EventFollowupSettingsRepository(conn)
    settings_cache: dict[str, EventFollowupSettings | None] = {}
    event_cache: dict[str, Event] = {}

    for row in rows:
        scanned += 1
        business_id = str(row["business_id"] if hasattr(row, "keys") else row[0])
        event_id = str(row["event_id"] if hasattr(row, "keys") else row[1])
        position = int(row["position"] if hasattr(row, "keys") else row[2])
        slot_key = str(row["slot_key"] if hasattr(row, "keys") else row[3])
        revision = int(row["revision"] if hasattr(row, "keys") else row[4])
        scheduled_at = normalize_utc(
            str(row["scheduled_at"] if hasattr(row, "keys") else row[5]),
            field_name="scheduled_at",
        )
        text = str(row["text"] if hasattr(row, "keys") else row[6])
        public_slug = str(row["public_slug"] if hasattr(row, "keys") else row[7])
        timezone_name = str(row["timezone_name"] if hasattr(row, "keys") else row[8])
        token = str(row["token"] if hasattr(row, "keys") else row[10])

        zone = ZoneInfo(timezone_name)
        if current.astimezone(zone).date() != scheduled_at.astimezone(zone).date():
            expired += 1
            continue

        key = _dispatch_key(
            event_id=event_id,
            registration_id=str(
                row["registration_id"] if hasattr(row, "keys") else row[9]
            ),
            slot_key=slot_key,
            revision=revision,
        )
        existing = conn.execute(
            """
            SELECT 1 FROM provider_dispatch_outbox
            WHERE business_id=? AND idempotency_key=? LIMIT 1
            """,
            (business_id, key),
        ).fetchone()
        if existing is not None:
            continue

        if business_id not in settings_cache:
            settings_cache[business_id] = settings_repository.get(
                business_id=business_id
            )
        settings = settings_cache[business_id]
        if settings is None or not settings.enabled:
            settings_disabled += 1
            continue

        event = event_cache.get(event_id)
        if event is None:
            event = event_repository.get_public_owner_event(public_slug=public_slug)
            event_cache[event_id] = event
        registration = event_repository.get_registration_by_token(token=token)
        consent_channels = active_event_commercial_channels(
            conn,
            business_id=business_id,
            event_id=event_id,
            registration_id=registration.id,
        )
        if not consent_channels:
            no_consent += 1
            continue
        selected_channels = tuple(
            channel
            for channel in consent_channels
            if channel in settings.enabled_channels
        )
        if not selected_channels:
            no_consent += 1
            continue

        target = _resolve_target(
            conn,
            event=event,
            registration=registration,
            channels=selected_channels,
        )
        if target is None:
            no_route += 1
            continue

        _subject, body = _render_for_registration(
            text=text,
            event=event,
            registration=registration,
        )
        payload_ref = (
            EmailPayload(subject=_subject, body=body).to_json()
            if target.platform == "email"
            else body
        )
        if not event_commercial_policy_authorized(
            conn,
            business_id=business_id,
            registration_id=registration.id,
            platform=target.platform,
            payload_ref=payload_ref,
            scheduled_at=current,
            now=current,
        ):
            policy_blocked += 1
            continue

        media_queued = False
        if target.platform != "email":
            asset = _active_visual_asset(
                conn,
                business_id=business_id,
                event_id=event_id,
                slot_key=slot_key,
            )
            if asset is not None:
                asset_kind, asset_reference, asset_revision = asset
                if event_commercial_policy_authorized(
                    conn,
                    business_id=business_id,
                    registration_id=registration.id,
                    platform=target.platform,
                    payload_ref=asset_reference,
                    scheduled_at=current,
                    now=current,
                ):
                    media_queued = _materialize_media(
                        conn,
                        event=event,
                        registration=registration,
                        target=target,
                        slot_key=slot_key,
                        message_revision=revision,
                        asset_kind=asset_kind,
                        asset_reference=asset_reference,
                        asset_revision=asset_revision,
                        now_iso=now_iso,
                    )

        if _materialize(
            conn,
            event=event,
            registration=registration,
            target=target,
            slot_key=slot_key,
            revision=revision,
            text=text,
            now_iso=now_iso,
            available_at=(
                (current + timedelta(seconds=2)).replace(microsecond=0).isoformat()
                if media_queued
                else now_iso
            ),
        ):
            queued += 1

    return EventWarmupBatchResult(
        scanned=scanned,
        queued=queued,
        expired=expired,
        no_consent=no_consent,
        no_route=no_route,
        policy_blocked=policy_blocked,
        settings_disabled=settings_disabled,
    )


def materialize_due_event_warmups(
    *,
    limit: int = 100,
    now: datetime | str | None = None,
) -> EventWarmupBatchResult:
    with get_db() as conn:
        return materialize_due_event_warmups_in_transaction(
            conn,
            limit=limit,
            now=now,
        )


__all__ = [
    "EventWarmupBatchResult",
    "EventWarmupDraft",
    "EventWarmupPlan",
    "build_event_warmup_plan",
    "get_event_warmup_plan",
    "get_saved_event_warmup_plan",
    "materialize_due_event_warmups",
    "materialize_due_event_warmups_in_transaction",
    "reset_event_warmup_text",
    "save_event_warmup_plan",
    "set_event_warmup_text",
]
