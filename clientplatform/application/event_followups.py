from __future__ import annotations

"""Consent-aware, replay-safe, multi-channel post-event sales follow-up.

Commercial follow-ups are a separate purpose from organizational event messages.
They are queued only when a registration has active channel-scoped commercial
consent and are revalidated immediately before provider I/O. Payment is terminal:
paid registrations are proactively cancelled and also blocked at the provider
boundary.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from typing import Any
from uuid import uuid4

from clientplatform.application.event_commercial_consent import (
    active_event_commercial_channels,
)
from clientplatform.domain.email_outbound import EmailPayload
from clientplatform.domain.events import normalize_utc
from clientplatform.infrastructure.event_dispatch_safety import (
    event_commercial_policy_authorized,
    quarantine_stale_event_commercial_boundaries,
)
from services.db import get_db


_POST_FOLLOWUP_VERSION = "v4"
_DEFAULT_EVENT_DURATION = timedelta(hours=2)
_STAGE_GRACE = timedelta(hours=36)
_DEFAULT_PRIORITY = ("max", "vk", "email")
_ALLOWED_PRIORITY = frozenset({"max", "vk", "email", "telegram"})
_COMMERCIAL_PROVIDER_BOUNDARY_MARKER = "event_commercial_provider_call_started_non_idempotent"
_SCAN_SCOPE = "commercial-event-followups:v1"


@dataclass(frozen=True, slots=True)
class EventFollowupCandidate:
    business_id: str
    event_id: str
    event_title: str
    registration_id: str
    customer_id: str | None
    name: str
    email: str
    token: str
    email_connection_id: str | None
    segment: str
    event_end_at: datetime


@dataclass(frozen=True, slots=True)
class EventFollowupTarget:
    platform: str
    connection_id: str
    recipient_kind: str
    customer_identity_id: str | None
    external_subject: str


@dataclass(frozen=True, slots=True)
class EventFollowupBatchResult:
    scanned: int
    queued: int
    not_due: int
    expired: int
    no_consent: int
    no_route: int
    legacy_after_cancelled: int
    authority_cancelled: int
    policy_blocked: int = 0


def commercial_event_followups_enabled() -> bool:
    """Kill switch. Consent alone never silently enables a new send surface."""

    return str(
        os.getenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED") or ""
    ).strip().lower() in {"1", "true", "yes", "on"}


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


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def classify_event_followup_segment(
    *,
    first_join_click_at: object | None,
    attendance_confirmed_at: object | None,
    offer_clicked_at: object | None,
) -> str:
    """Classify only observed facts; a join redirect is not attendance."""

    if offer_clicked_at:
        return "offer_clicked_unpaid"
    if attendance_confirmed_at:
        return "attended_unpaid"
    if first_join_click_at:
        return "join_signal_unpaid"
    return "no_show"


def _stage_offsets(segment: str) -> tuple[tuple[int, timedelta], ...]:
    first = (1, timedelta(minutes=60))
    second = (2, timedelta(hours=24))
    third = (3, timedelta(hours=48))
    if segment in {"attended_unpaid", "offer_clicked_unpaid"}:
        return (first, second, third)
    return (first, second)


def _public_base_url() -> str:
    from config.settings import settings

    value = str(getattr(settings, "MESSENGER_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if not value.startswith("https://"):
        raise RuntimeError("MESSENGER_PUBLIC_BASE_URL must be configured as HTTPS")
    return value


def _offer_url(token: str) -> str:
    return f"{_public_base_url()}/e/offer/{token}"


def _unsubscribe_url(token: str) -> str:
    return f"{_public_base_url()}/e/marketing/unsubscribe/{token}"


def _render(candidate: EventFollowupCandidate, *, stage: int) -> tuple[str, str]:
    offer = _offer_url(candidate.token)
    unsubscribe = _unsubscribe_url(candidate.token)
    name = candidate.name
    title = candidate.event_title
    footer = f"\n\nОтказаться от рекламных сообщений: {unsubscribe}"

    if candidate.segment == "offer_clicked_unpaid":
        if stage == 1:
            body = (
                f"{name}, вы открывали предложение после «{title}».\n\n"
                f"Если хотите вернуться к нему: {offer}"
            )
        elif stage == 2:
            body = (
                f"{name}, если после «{title}» остался вопрос по формату или оплате, "
                f"предложение по-прежнему доступно здесь: {offer}"
            )
        else:
            body = (
                f"{name}, завершаю серию сообщений по «{title}». "
                f"Если тема остаётся актуальной, вернуться к предложению можно здесь: {offer}"
            )
        return (f"После «{title}»", body + footer)

    if candidate.segment == "attended_unpaid":
        if stage == 1:
            body = (
                f"{name}, спасибо, что были на «{title}».\n\n"
                f"Продолжить и посмотреть предложение: {offer}"
            )
        elif stage == 2:
            body = (
                f"{name}, напомню о продолжении темы «{title}» без искусственной срочности. "
                f"Подробности здесь: {offer}"
            )
        else:
            body = (
                f"{name}, это последнее сообщение в серии после «{title}». "
                f"Если захотите продолжить позже: {offer}"
            )
        return (f"Продолжение после «{title}»", body + footer)

    if candidate.segment == "join_signal_unpaid":
        body = (
            f"{name}, вы переходили к эфиру «{title}». "
            f"Если тема для вас актуальна, предложение здесь: {offer}"
            if stage == 1
            else f"{name}, оставлю ещё одну ссылку по теме «{title}»: {offer}"
        )
        return (f"После мероприятия «{title}»", body + footer)

    if candidate.segment == "no_show":
        body = (
            f"{name}, вы регистрировались на «{title}», но мы не видим подтверждённого участия. "
            f"Если тема остаётся актуальной, посмотреть предложение можно здесь: {offer}"
            if stage == 1
            else f"{name}, это последнее напоминание по регистрации на «{title}»: {offer}"
        )
        return (f"Вы регистрировались: {title}", body + footer)

    raise ValueError("unsupported event follow-up segment")


def _candidate_from_row(row: Any) -> EventFollowupCandidate:
    starts_at = normalize_utc(str(_value(row, "starts_at", 9)), field_name="starts_at")
    ends_raw = _value(row, "ends_at", 10)
    ends_at = (
        normalize_utc(str(ends_raw), field_name="ends_at")
        if ends_raw is not None
        else starts_at + _DEFAULT_EVENT_DURATION
    )
    return EventFollowupCandidate(
        business_id=str(_value(row, "business_id", 0)),
        event_id=str(_value(row, "event_id", 1)),
        event_title=str(_value(row, "event_title", 2)),
        registration_id=str(_value(row, "registration_id", 3)),
        customer_id=(
            None
            if _value(row, "customer_id", 4) is None
            else str(_value(row, "customer_id", 4))
        ),
        name=str(_value(row, "name", 5)),
        email=str(_value(row, "email", 6)),
        token=str(_value(row, "token", 7)),
        email_connection_id=(
            None
            if _value(row, "email_connection_id", 8) is None
            else str(_value(row, "email_connection_id", 8))
        ),
        segment=classify_event_followup_segment(
            first_join_click_at=_value(row, "first_join_click_at", 11),
            attendance_confirmed_at=_value(row, "attendance_confirmed_at", 12),
            offer_clicked_at=_value(row, "offer_clicked_at", 13),
        ),
        event_end_at=ends_at,
    )


def _resolve_email_target(conn: Any, candidate: EventFollowupCandidate) -> EventFollowupTarget | None:
    if not candidate.email_connection_id:
        return None
    row = conn.execute(
        """
        SELECT id
        FROM connections
        WHERE id=? AND business_id=? AND platform='email'
          AND connection_type='email_smtp' AND status='active'
        LIMIT 1
        """,
        (candidate.email_connection_id, candidate.business_id),
    ).fetchone()
    if row is None:
        return None
    return EventFollowupTarget(
        platform="email",
        connection_id=candidate.email_connection_id,
        recipient_kind="external_subject",
        customer_identity_id=None,
        external_subject=candidate.email,
    )


def _resolve_messenger_target(
    conn: Any,
    candidate: EventFollowupCandidate,
    *,
    platform: str,
) -> EventFollowupTarget | None:
    if not candidate.customer_id:
        return None
    # First prefer a previously successful route for this customer's active
    # identity. This binds both the exact identity and the exact VK/MAX bot or
    # community and avoids guessing when the tenant has several connections.
    history = conn.execute(
        """
        SELECT ci.id AS identity_id,ci.external_subject,d.connection_id
        FROM provider_dispatch_outbox d
        JOIN customer_identities ci
          ON ci.id=d.customer_identity_id AND ci.business_id=d.business_id
         AND ci.platform=d.platform AND ci.status='active'
        JOIN connections c
          ON c.id=d.connection_id AND c.business_id=d.business_id
         AND c.platform=d.platform AND c.status='active'
        WHERE d.business_id=? AND d.platform=? AND ci.customer_id=?
          AND d.status='sent'
        ORDER BY COALESCE(d.sent_at,d.updated_at) DESC,d.id DESC
        LIMIT 1
        """,
        (candidate.business_id, platform, candidate.customer_id),
    ).fetchone()
    if history is not None:
        identity_id = str(
            history["identity_id"] if hasattr(history, "keys") else history[0]
        )
        external_subject = str(
            history["external_subject"] if hasattr(history, "keys") else history[1]
        ).strip()
        connection_id = str(
            history["connection_id"] if hasattr(history, "keys") else history[2]
        )
        if not external_subject:
            return None
    else:
        identities = conn.execute(
            """
            SELECT id,external_subject
            FROM customer_identities
            WHERE business_id=? AND customer_id=? AND platform=? AND status='active'
            ORDER BY COALESCE(last_contact_at,updated_at,created_at) DESC,id DESC
            LIMIT 2
            """,
            (candidate.business_id, candidate.customer_id, platform),
        ).fetchall()
        if len(identities) != 1:
            return None
        identity = identities[0]
        identity_id = str(identity["id"] if hasattr(identity, "keys") else identity[0])
        external_subject = str(
            identity["external_subject"] if hasattr(identity, "keys") else identity[1]
        ).strip()
        if not external_subject:
            return None
        connections = conn.execute(
            """
            SELECT id
            FROM connections
            WHERE business_id=? AND platform=? AND status='active'
            ORDER BY created_at,id
            LIMIT 2
            """,
            (candidate.business_id, platform),
        ).fetchall()
        if len(connections) != 1:
            return None
        connection_id = str(
            connections[0]["id"] if hasattr(connections[0], "keys") else connections[0][0]
        )

    return EventFollowupTarget(
        platform=platform,
        connection_id=connection_id,
        recipient_kind="customer_identity",
        customer_identity_id=identity_id,
        external_subject=external_subject,
    )


def _resolve_target(
    conn: Any,
    candidate: EventFollowupCandidate,
    *,
    consent_channels: tuple[str, ...],
) -> EventFollowupTarget | None:
    allowed = set(consent_channels)
    for platform in _channel_priority():
        if platform not in allowed:
            continue
        if platform == "email":
            target = _resolve_email_target(conn, candidate)
        else:
            target = _resolve_messenger_target(conn, candidate, platform=platform)
        if target is not None:
            return target
    return None


def _dispatch_key(candidate: EventFollowupCandidate, *, stage: int) -> str:
    return (
        f"event:{candidate.event_id}:registration:{candidate.registration_id}:"
        f"message:post:{_POST_FOLLOWUP_VERSION}:stage:{stage}"
    )


def _stage_dispatch_state(
    conn: Any, candidate: EventFollowupCandidate, *, stage: int
) -> tuple[str, str | None] | None:
    row = conn.execute(
        """
        SELECT status,sent_at FROM provider_dispatch_outbox
        WHERE business_id=? AND idempotency_key=?
        LIMIT 1
        """,
        (candidate.business_id, _dispatch_key(candidate, stage=stage)),
    ).fetchone()
    if row is None:
        return None
    sent_at = _value(row, "sent_at", 1)
    return (
        str(_value(row, "status", 0)),
        None if sent_at is None else str(sent_at),
    )


def _next_due_stage(
    conn: Any,
    candidate: EventFollowupCandidate,
    *,
    current: datetime,
) -> tuple[int | None, str]:
    """Return at most one stage, chained to the previous successful delivery."""

    previous_sent_at: datetime | None = None
    previous_offset = timedelta(0)
    for stage, offset in _stage_offsets(candidate.segment):
        state = _stage_dispatch_state(conn, candidate, stage=stage)
        if state is not None:
            status, sent_at = state
            if status != "sent" or sent_at is None:
                return None, "waiting"
            previous_sent_at = normalize_utc(sent_at, field_name="sent_at")
            previous_offset = offset
            continue

        if stage == 1:
            due_at = candidate.event_end_at + offset
        else:
            if previous_sent_at is None:
                return None, "waiting"
            due_at = previous_sent_at + (offset - previous_offset)

        if current < due_at:
            return None, "future"
        if current > due_at + _STAGE_GRACE:
            return None, "expired"
        return stage, "due"

    return None, "complete"


def _delivery_payload(
    candidate: EventFollowupCandidate,
    target: EventFollowupTarget,
    *,
    stage: int,
) -> tuple[str, str]:
    subject, body = _render(candidate, stage=stage)
    if target.platform == "email":
        return "mixed", EmailPayload(subject=subject, body=body).to_json()
    return "text", body


def _automation_followup_authorized(
    conn: Any,
    *,
    candidate: EventFollowupCandidate,
    target: EventFollowupTarget,
    stage: int,
    scheduled_at: datetime | str,
) -> bool:
    payload_kind, payload_ref = _delivery_payload(candidate, target, stage=stage)
    del payload_kind
    return event_commercial_policy_authorized(
        conn,
        business_id=candidate.business_id,
        registration_id=candidate.registration_id,
        platform=target.platform,
        payload_ref=payload_ref,
        scheduled_at=scheduled_at,
        now=scheduled_at,
    )

def _materialize(
    conn: Any,
    *,
    candidate: EventFollowupCandidate,
    target: EventFollowupTarget,
    stage: int,
    now_iso: str,
) -> bool:
    payload_kind, payload_ref = _delivery_payload(candidate, target, stage=stage)
    key = _dispatch_key(candidate, stage=stage)
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
            candidate.business_id,
            target.platform,
            candidate.registration_id,
            target.connection_id,
            target.recipient_kind,
            target.customer_identity_id,
            target.external_subject,
            payload_kind,
            payload_ref,
            key,
            now_iso,
            now_iso,
            now_iso,
        ),
    )
    return int(getattr(cursor, "rowcount", 0) or 0) == 1


def _cancel_legacy_after_messages(conn: Any, *, now_iso: str) -> int:
    """Legacy v2 after-mail contains an offer and is therefore commercial work."""

    cursor = conn.execute(
        """
        UPDATE provider_dispatch_outbox
        SET status='cancelled',updated_at=?,locked_at=NULL,lock_token=NULL,
            last_error='replaced_by_consent_aware_event_followup'
        WHERE source_kind='event_message' AND platform='email'
          AND status IN ('pending','retry')
          AND idempotency_key LIKE 'event:%:message:after:v2'
        """,
        (now_iso,),
    )
    return max(0, int(getattr(cursor, "rowcount", 0) or 0))


def _cancel_invalid_commercial_messages(conn: Any, *, now_iso: str) -> int:
    """Proactively enforce PAID/REVOKED before claim; provider boundary checks again."""

    cursor = conn.execute(
        """
        UPDATE provider_dispatch_outbox AS d
        SET status='cancelled',updated_at=?,locked_at=NULL,lock_token=NULL,
            last_error='event_commercial_authority_revoked_or_paid'
        WHERE d.source_kind='event_message'
          AND d.status IN ('pending','retry')
          AND d.idempotency_key LIKE 'event:%:message:post:v4:stage:%'
          AND (
              EXISTS (
                  SELECT 1
                  FROM clientplatform_event_registrations r
                  JOIN clientplatform_event_conversion_links p
                    ON p.business_id=r.business_id AND p.event_id=r.event_id
                   AND p.registration_id=r.id
                  WHERE r.id=d.source_id AND r.business_id=d.business_id
              )
              OR NOT EXISTS (
                  SELECT 1
                  FROM clientplatform_event_registrations r
                  JOIN clientplatform_event_commercial_channel_state cs
                    ON cs.business_id=r.business_id AND cs.event_id=r.event_id
                   AND cs.registration_id=r.id AND cs.platform=d.platform
                   AND cs.status='active'
                  WHERE r.id=d.source_id AND r.business_id=d.business_id
                    AND r.status='registered'
              )
          )
        """,
        (now_iso,),
    )
    return max(0, int(getattr(cursor, "rowcount", 0) or 0))


def cancel_commercial_followups_for_registration_in_transaction(
    conn: Any,
    *,
    business_id: str,
    registration_id: str,
    reason: str,
    now: datetime | str | None = None,
) -> int:
    stamp = normalize_utc(
        now or datetime.now(timezone.utc), field_name="now"
    ).replace(microsecond=0).isoformat()
    cursor = conn.execute(
        """
        UPDATE provider_dispatch_outbox
        SET status='cancelled',updated_at=?,locked_at=NULL,lock_token=NULL,last_error=?
        WHERE business_id=? AND source_kind='event_message' AND source_id=?
          AND status IN ('pending','retry')
          AND idempotency_key LIKE 'event:%:message:post:v4:stage:%'
        """,
        (
            stamp,
            str(reason or "event_commercial_stopped")[:240],
            business_id,
            registration_id,
        ),
    )
    return max(0, int(getattr(cursor, "rowcount", 0) or 0))


def _load_scan_cursor(conn: Any) -> tuple[str | None, str | None, str | None]:
    row = conn.execute(
        """
        SELECT cursor_starts_at,cursor_event_id,cursor_registration_id
        FROM clientplatform_event_followup_scan_state
        WHERE scope=?
        LIMIT 1
        """,
        (_SCAN_SCOPE,),
    ).fetchone()
    if row is None:
        return None, None, None
    return (
        None if _value(row, "cursor_starts_at", 0) is None else str(_value(row, "cursor_starts_at", 0)),
        None if _value(row, "cursor_event_id", 1) is None else str(_value(row, "cursor_event_id", 1)),
        None if _value(row, "cursor_registration_id", 2) is None else str(_value(row, "cursor_registration_id", 2)),
    )


def _store_scan_cursor(
    conn: Any,
    *,
    cursor_start: str | None,
    cursor_event: str | None,
    cursor_registration: str | None,
    now_iso: str,
) -> None:
    conn.execute(
        """
        INSERT INTO clientplatform_event_followup_scan_state(
            scope,cursor_starts_at,cursor_event_id,cursor_registration_id,updated_at
        ) VALUES(?,?,?,?,?)
        ON CONFLICT(scope) DO UPDATE SET
            cursor_starts_at=excluded.cursor_starts_at,
            cursor_event_id=excluded.cursor_event_id,
            cursor_registration_id=excluded.cursor_registration_id,
            updated_at=excluded.updated_at
        """,
        (_SCAN_SCOPE, cursor_start, cursor_event, cursor_registration, now_iso),
    )


def _scan_candidate_rows(
    conn: Any,
    *,
    current: datetime,
    now_iso: str,
    cursor_start: str | None,
    cursor_event: str | None,
    cursor_registration: str | None,
    limit: int,
) -> list[Any]:
    return list(
        conn.execute(
            """
            SELECT e.business_id,
                   e.id AS event_id,
                   e.title AS event_title,
                   r.id AS registration_id,
                   r.customer_id,
                   r.name,
                   r.email,
                   r.token,
                   e.notification_connection_id AS email_connection_id,
                   e.starts_at,
                   e.ends_at,
                   r.first_join_click_at,
                   r.attendance_confirmed_at,
                   r.offer_clicked_at
            FROM clientplatform_events e
            JOIN clientplatform_event_registrations r
              ON r.event_id=e.id AND r.business_id=e.business_id
             AND r.status='registered'
            WHERE e.status IN ('published','completed')
              AND e.offer_url IS NOT NULL
              AND e.starts_at<=?
              AND e.starts_at>=?
              AND NOT EXISTS (
                  SELECT 1 FROM clientplatform_event_conversion_links p
                  WHERE p.business_id=r.business_id
                    AND p.event_id=r.event_id
                    AND p.registration_id=r.id
              )
              AND (
                  ? IS NULL
                  OR e.starts_at>?
                  OR (e.starts_at=? AND e.id>?)
                  OR (e.starts_at=? AND e.id=? AND r.id>?)
              )
            ORDER BY e.starts_at,e.id,r.id
            LIMIT ?
            """,
            (
                now_iso,
                (current - timedelta(days=7)).isoformat(),
                cursor_start,
                cursor_start,
                cursor_start,
                cursor_event,
                cursor_start,
                cursor_event,
                cursor_registration,
                int(limit),
            ),
        ).fetchall()
    )


def materialize_due_event_followups_in_transaction(
    conn: Any,
    *,
    limit: int = 100,
    now: datetime | str | None = None,
) -> EventFollowupBatchResult:
    bounded_limit = max(1, min(int(limit), 1000))
    current = normalize_utc(now or datetime.now(timezone.utc), field_name="now")
    now_iso = current.replace(microsecond=0).isoformat()

    # The old v2 `after` message contains an offer, so it is commercial work.
    # Cancel it even while the new engine is disabled; the kill switch must never
    # revive legacy advertising that had no channel-scoped marketing consent.
    legacy_cancelled = _cancel_legacy_after_messages(conn, now_iso=now_iso)
    if not commercial_event_followups_enabled():
        return EventFollowupBatchResult(
            scanned=0, queued=0, not_due=0, expired=0, no_consent=0, no_route=0,
            legacy_after_cancelled=legacy_cancelled, authority_cancelled=0, policy_blocked=0,
        )

    authority_cancelled = _cancel_invalid_commercial_messages(conn, now_iso=now_iso)
    cursor_start, cursor_event, cursor_registration = _load_scan_cursor(conn)
    rows = _scan_candidate_rows(
        conn,
        current=current,
        now_iso=now_iso,
        cursor_start=cursor_start,
        cursor_event=cursor_event,
        cursor_registration=cursor_registration,
        limit=bounded_limit,
    )
    if not rows and cursor_start is not None:
        # One bounded wrap per tick. The durable cursor prevents an ineligible
        # prefix from being rescanned forever while keeping inspection bounded.
        cursor_start = cursor_event = cursor_registration = None
        _store_scan_cursor(
            conn,
            cursor_start=None,
            cursor_event=None,
            cursor_registration=None,
            now_iso=now_iso,
        )
        rows = _scan_candidate_rows(
            conn,
            current=current,
            now_iso=now_iso,
            cursor_start=None,
            cursor_event=None,
            cursor_registration=None,
            limit=bounded_limit,
        )

    scanned = 0
    queued = 0
    not_due = 0
    expired = 0
    no_consent = 0
    no_route = 0
    policy_blocked = 0

    for row in rows:
        scanned += 1
        cursor_start = str(_value(row, "starts_at", 9))
        cursor_event = str(_value(row, "event_id", 1))
        cursor_registration = str(_value(row, "registration_id", 3))
        candidate = _candidate_from_row(row)
        consent_channels = active_event_commercial_channels(
            conn,
            business_id=candidate.business_id,
            event_id=candidate.event_id,
            registration_id=candidate.registration_id,
        )
        if not consent_channels:
            no_consent += 1
            continue

        stage, stage_state = _next_due_stage(conn, candidate, current=current)
        if stage is None:
            if stage_state == "future":
                not_due += 1
            elif stage_state == "expired":
                expired += 1
            continue

        target = _resolve_target(
            conn,
            candidate,
            consent_channels=consent_channels,
        )
        if target is None:
            no_route += 1
            continue

        if not _automation_followup_authorized(
            conn,
            candidate=candidate,
            target=target,
            stage=stage,
            scheduled_at=now_iso,
        ):
            policy_blocked += 1
            continue

        if _materialize(
            conn,
            candidate=candidate,
            target=target,
            stage=stage,
            now_iso=now_iso,
        ):
            queued += 1

    if rows:
        _store_scan_cursor(
            conn,
            cursor_start=cursor_start,
            cursor_event=cursor_event,
            cursor_registration=cursor_registration,
            now_iso=now_iso,
        )

    return EventFollowupBatchResult(
        scanned=scanned,
        queued=queued,
        not_due=not_due,
        expired=expired,
        no_consent=no_consent,
        no_route=no_route,
        legacy_after_cancelled=legacy_cancelled,
        authority_cancelled=authority_cancelled,
        policy_blocked=policy_blocked,
    )

def materialize_due_event_followups(
    *,
    limit: int = 100,
    lock_ttl_seconds: int = 900,
    now: datetime | str | None = None,
) -> EventFollowupBatchResult:
    with get_db() as conn:
        quarantine_now = (
            None
            if now is None
            else normalize_utc(now, field_name="now")
        )
        quarantine_stale_event_commercial_boundaries(
            conn,
            lock_ttl_seconds=lock_ttl_seconds,
            now=quarantine_now,
        )
        return materialize_due_event_followups_in_transaction(conn, limit=limit, now=now)


__all__ = [
    "EventFollowupBatchResult",
    "EventFollowupCandidate",
    "EventFollowupTarget",
    "cancel_commercial_followups_for_registration_in_transaction",
    "classify_event_followup_segment",
    "commercial_event_followups_enabled",
    "materialize_due_event_followups",
    "materialize_due_event_followups_in_transaction",
]
