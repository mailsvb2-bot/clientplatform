from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clientplatform.application.automation_policy import (
    approve_automation_action,
    get_automation_action_approval,
    is_owner_autopilot_enabled,
    list_current_automation_action_approvals,
    reject_automation_action,
    revoke_automation_action_approval,
    set_owner_autopilot_enabled,
    toggle_owner_autopilot,
)
from clientplatform.domain.automation_policy import AutomationActionApproval
from clientplatform.domain.money import normalize_settlement_currency, settlement_currency_minor_unit_exponent
from clientplatform.domain.outcomes import (
    BusinessOutcomeEvent,
    OutcomeMoney,
    OutcomeSource,
    OutcomeType,
)
from clientplatform.domain.tenancy import (
    BUSINESS_MEMBER_ROLES,
    PlatformRole,
    TenantContext,
    TenantPermissionDenied,
    normalize_uuid,
)
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.automation_policy_repository import AutomationPolicyRepository
from clientplatform.infrastructure.outcome_repository import OutcomeRepository
from clientplatform.infrastructure.revenue_attribution_repository import (
    RevenueAttributionRepository,
)
from services.db import get_db, get_db_ro


_CONTENT_ROLES = frozenset(
    {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.CONTENT_MANAGER,
        PlatformRole.MARKETER,
    }
)
_FINANCE_READ_ROLES = frozenset(
    {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.MARKETER,
        PlatformRole.ANALYST,
    }
)
_FINANCE_WRITE_ROLES = frozenset(
    {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.MARKETER,
    }
)
_OBSERVABILITY_ROLES = frozenset(BUSINESS_MEMBER_ROLES)
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9._-]{0,39}$")
_PUBLICATION_SCHEDULE_VERSION_RE = re.compile(r"^[0-9a-f]{1,16}$")


class PaymentIdempotencyConflict(ValueError):
    """A business payment key was reused for a different external fact."""


class PaymentStateConflict(ValueError):
    """A payment mutation conflicts with the durable payment state."""


class PaymentEvidenceInvariantViolation(RuntimeError):
    """Durable payment state and canonical outcome evidence disagree."""


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    id: str
    business_id: str
    channel: str
    title: str
    body: str
    status: str
    created_at: str
    updated_at: str
    scheduled_at: str | None
    published_at: str | None
    failed_at: str | None
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class PublicationCalendarProjection:
    entries: tuple[PublicationRecord, ...]
    actionable_drafts: tuple[PublicationRecord, ...]
    draft_count: int
    scheduled_count: int
    published_count: int
    failed_count: int
    cancelled_count: int


_PUBLICATION_STATUS_LABELS = {
    "draft": ("📝", "Черновик"),
    "scheduled": ("🗓", "Запланировано"),
    "published": ("✅", "Опубликовано"),
    "failed": ("⚠️", "Ошибка"),
    "cancelled": ("⛔", "Отменено"),
}
_AUTOMATION_ACTION_LABELS = {
    "growth.read_only_analysis": "Проанализировать рост",
    "ads.adjust_budget": "Изменить рекламный бюджет",
    "sales.followup": "Отправить клиенту follow-up",
    "payments.refund": "Оформить возврат",
}
_AUTOMATION_CHANNEL_LABELS = {
    "internal": "внутри ClientPlatform",
    "email": "email",
    "yandex_direct": "Яндекс Директ",
    "telegram": "Telegram",
    "vk": "ВКонтакте",
    "max": "MAX",
}
_AUTOMATION_APPROVAL_REASON_LABELS = {
    "cautious_mode_external_write": "включён осторожный режим",
    "action_requires_approval": "это действие требует подтверждения",
    "channel_requires_approval": "канал требует подтверждения",
    "action_threshold_requires_approval": "для действия установлен порог подтверждения",
    "money_threshold_requires_approval": "сумма достигла порога подтверждения",
}


_PUBLICATION_CHANNEL_LABELS = {
    "telegram": "Telegram",
    "vk": "ВКонтакте",
    "max": "MAX",
    "other": "Другой канал",
}


def _publication_effective_timestamp(item: PublicationRecord) -> str:
    if item.status == "scheduled" and item.scheduled_at:
        return item.scheduled_at
    if item.status == "published" and item.published_at:
        return item.published_at
    if item.status == "failed" and item.failed_at:
        return item.failed_at
    return item.updated_at


def _format_publication_timestamp(value: object, *, timezone_name: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "время не указано"
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return "время не указано"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        zone: tzinfo = ZoneInfo(str(timezone_name or "UTC").strip() or "UTC")
    except (ValueError, ZoneInfoNotFoundError):
        zone = timezone.utc
    return parsed.astimezone(zone).strftime("%d.%m.%Y %H:%M")


def _publication_business_zone(conn: Any, business_id: str) -> ZoneInfo:
    row = conn.execute(
        "SELECT timezone FROM business_profiles WHERE business_id=? LIMIT 1",
        (business_id,),
    ).fetchone()
    if row is None:
        raise ValueError("business timezone is required before scheduling publication")
    timezone_name = str(_value(row, "timezone", 0) or "").strip()
    if not timezone_name:
        raise ValueError("business timezone is required before scheduling publication")
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("business timezone is invalid") from exc


def _publication_schedule_utc(
    value: object,
    *,
    zone: ZoneInfo,
    now: datetime | None = None,
) -> str:
    raw = " ".join(str(value or "").split())
    try:
        local = datetime.strptime(raw, "%d.%m.%Y %H:%M")
    except ValueError as exc:
        raise ValueError(
            "publication time must look like 28.08.2026 19:30"
        ) from exc

    occurrences: list[datetime] = []
    for fold in (0, 1):
        candidate = local.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(timezone.utc).astimezone(zone)
        if round_trip.replace(tzinfo=None) != local or round_trip.fold != fold:
            continue
        occurrences.append(candidate)
    if not occurrences:
        raise ValueError(
            "publication time does not exist locally because of a timezone transition"
        )
    if len(occurrences) > 1:
        raise ValueError(
            "publication time is ambiguous because of a timezone transition"
        )

    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    scheduled = occurrences[0].astimezone(timezone.utc).replace(microsecond=0)
    if scheduled <= reference.astimezone(timezone.utc):
        raise ValueError("publication time must be in the future")
    return scheduled.isoformat(timespec="seconds")


def encode_publication_schedule_version(scheduled_at: str) -> str:
    """Encode a canonical scheduled timestamp into a compact stale-action token."""

    raw = str(scheduled_at).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("scheduled_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("scheduled_at must be timezone-aware")
    utc_value = parsed.astimezone(timezone.utc).replace(microsecond=0)
    return format(int(utc_value.timestamp()), "x")


def decode_publication_schedule_version(version: str) -> str:
    """Decode a compact schedule token back to the canonical UTC timestamp."""

    raw = str(version).strip().casefold()
    if not _PUBLICATION_SCHEDULE_VERSION_RE.fullmatch(raw):
        raise ValueError("publication schedule version is invalid")
    try:
        parsed = datetime.fromtimestamp(int(raw, 16), tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("publication schedule version is invalid") from exc
    return parsed.replace(microsecond=0).isoformat(timespec="seconds")


def format_publication_calendar_lines(
    publications: list[PublicationRecord] | tuple[PublicationRecord, ...],
    *,
    timezone_name: str,
    max_entries: int = 8,
) -> tuple[str, ...]:
    """Render canonical publication facts for any owner messenger surface."""

    if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
        raise ValueError("max_entries must be a positive integer")
    selected = publications[:max_entries]
    if not selected:
        return ("• Публикаций пока нет.",)
    lines: list[str] = []
    for item in selected:
        icon, status_label = _PUBLICATION_STATUS_LABELS.get(
            item.status, ("•", "Статус неизвестен")
        )
        channel_label = _PUBLICATION_CHANNEL_LABELS.get(
            item.channel, "Другой канал"
        )
        title = " ".join(str(item.title or "").split())
        if len(title) > 48:
            title = title[:47].rstrip() + "…"
        timestamp = _format_publication_timestamp(
            _publication_effective_timestamp(item),
            timezone_name=timezone_name,
        )
        lines.append(
            f"• {icon} {timestamp} · {channel_label} · {status_label} · {title}"
        )
    return tuple(lines)


@dataclass(frozen=True, slots=True)
class PaymentRecord:
    id: str
    business_id: str
    customer_id: str | None
    amount_minor: int
    currency: str
    status: str
    provider: str
    external_reference: str | None
    note: str
    created_at: str
    paid_at: str | None
    refunded_at: str | None
    idempotency_key: str | None
    outcome_event_id: str | None
    offering_id: str | None
    revenue_attribution_id: str | None
    refund_idempotency_key: str | None
    refund_outcome_event_id: str | None
    refund_revenue_attribution_id: str | None


@dataclass(frozen=True, slots=True)
class _PaymentEvidence:
    business_id: str
    payment_id: str
    operation: str
    idempotency_key: str
    request_fingerprint: str
    outcome_event_id: str
    offering_id: str | None
    provider: str
    external_reference: str | None


@dataclass(frozen=True, slots=True)
class OfferingPrice:
    id: str
    business_id: str
    offering_id: str
    offering_title: str
    amount_minor: int
    currency: str
    status: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class SubscriptionState:
    business_id: str
    plan_key: str
    status: str
    included_staff: int
    included_customers: int
    started_at: str
    renews_at: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class InteractionMetricInput:
    business_id: str
    actor_user_id: int
    callback_action: str
    success: bool
    ack_ms: int
    lock_wait_ms: int
    app_ms: int
    telegram_ms: int
    total_ms: int
    transport_role: str
    transport_route: str
    transport_generation: int | None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class InteractionSnapshot:
    count: int
    successes: int
    failures: int
    p50_ms: int
    p95_ms: int
    max_ms: int
    ack_p95_ms: int
    lock_p95_ms: int
    telegram_p95_ms: int
    window_minutes: int


@dataclass(frozen=True, slots=True)
class AdminAlert:
    id: str
    kind: str
    severity: str
    message: str
    occurrences: int
    first_seen_at: str
    last_seen_at: str


@dataclass(frozen=True, slots=True)
class PaymentCurrencySummary:
    currency: str
    amount_minor: int
    paid_payments: int


@dataclass(frozen=True, slots=True)
class PaymentSummary:
    paid_payments: int
    paid_customers: int
    by_currency: tuple[PaymentCurrencySummary, ...]


@dataclass(frozen=True, slots=True)
class AdminInsightSnapshot:
    active_customers: int
    active_offerings: int
    active_invites: int
    claimed_invites: int
    enrollments: int
    completed_enrollments: int
    publication_drafts: int
    publications_published: int
    paid_payments: int
    paid_amount_minor: int
    payment_currency: str
    priced_offerings: int
    active_staff: int


@dataclass(frozen=True, slots=True)
class AuditEvent:
    action: str
    subject_type: str
    subject_id: str | None
    detail: str
    created_at: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _text(value: object, *, field: str, maximum: int) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").replace("\x00", " ")).strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters")
    return normalized


def _body(value: object, *, maximum: int = 4000) -> str:
    normalized = str(value or "").replace("\x00", " ").strip()
    if not normalized:
        raise ValueError("body must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"body must be at most {maximum} characters")
    return normalized


def _currency(value: object) -> str:
    return normalize_settlement_currency(value)


def _amount_minor(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("amount_minor must be positive")
    normalized = value
    if normalized <= 0:
        raise ValueError("amount_minor must be positive")
    if normalized > 1_000_000_000_00:
        raise ValueError("amount_minor is too large")
    return normalized


def _idempotency_key(value: object) -> str:
    normalized = str(value or "").strip()
    if not _IDEMPOTENCY_KEY_RE.fullmatch(normalized):
        raise ValueError(
            "idempotency_key must be 1-200 opaque ASCII characters"
        )
    return normalized


def _provider(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if not _PROVIDER_RE.fullmatch(normalized):
        raise ValueError("provider must be a lowercase stable identifier")
    return normalized


def _external_reference(value: object, *, provider: str) -> str | None:
    normalized = (
        None
        if value in (None, "")
        else _text(value, field="external_reference", maximum=180)
    )
    if provider != "manual" and normalized is None:
        raise ValueError("external_reference is required for provider confirmation")
    return normalized


def _payment_stamp(value: datetime | None) -> datetime:
    stamp = value or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        raise ValueError("payment timestamp must be timezone-aware")
    return stamp.astimezone(timezone.utc)


def _request_fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _begin_payment_write(conn: Any) -> None:
    if isinstance(conn, sqlite3.Connection) and not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def _serialize_payment_write(conn: Any, *, business_id: str) -> None:
    """Serialize payment invariants after tenant authorization has succeeded."""

    cursor = conn.execute(
        """
        UPDATE businesses
        SET updated_at=updated_at
        WHERE id=? AND status='active'
        """,
        (business_id,),
    )
    if int(getattr(cursor, "rowcount", 1) or 0) != 1:
        raise PaymentStateConflict("active business was not found")


def _payment_evidence_from_row(row: Any) -> _PaymentEvidence:
    offering_id = _value(row, "offering_id", 6)
    external_reference = _value(row, "external_reference", 8)
    return _PaymentEvidence(
        business_id=str(_value(row, "business_id", 0)),
        payment_id=str(_value(row, "payment_id", 1)),
        operation=str(_value(row, "operation", 2)),
        idempotency_key=str(_value(row, "idempotency_key", 3)),
        request_fingerprint=str(_value(row, "request_fingerprint", 4)),
        outcome_event_id=str(_value(row, "outcome_event_id", 5)),
        offering_id=None if offering_id is None else str(offering_id),
        provider=str(_value(row, "provider", 7)),
        external_reference=(
            None if external_reference is None else str(external_reference)
        ),
    )


_PAYMENT_EVIDENCE_SELECT = """
    SELECT business_id, payment_id, operation, idempotency_key,
           request_fingerprint, outcome_event_id, offering_id,
           provider, external_reference
    FROM business_payment_outcome_evidence
"""


def _evidence_by_key(
    conn: Any,
    *,
    business_id: str,
    idempotency_key: str,
) -> _PaymentEvidence | None:
    row = conn.execute(
        _PAYMENT_EVIDENCE_SELECT
        + " WHERE business_id=? AND idempotency_key=? LIMIT 1",
        (business_id, idempotency_key),
    ).fetchone()
    return None if row is None else _payment_evidence_from_row(row)


def _evidence_for_payment(
    conn: Any,
    *,
    business_id: str,
    payment_id: str,
    operation: str,
) -> _PaymentEvidence | None:
    row = conn.execute(
        _PAYMENT_EVIDENCE_SELECT
        + " WHERE business_id=? AND payment_id=? AND operation=? LIMIT 1",
        (business_id, payment_id, operation),
    ).fetchone()
    return None if row is None else _payment_evidence_from_row(row)


def _provider_evidence(
    conn: Any,
    *,
    business_id: str,
    operation: str,
    provider: str,
    external_reference: str | None,
) -> _PaymentEvidence | None:
    if external_reference is None:
        return None
    row = conn.execute(
        _PAYMENT_EVIDENCE_SELECT
        + " WHERE business_id=? AND operation=? AND provider=? "
        "AND external_reference=? LIMIT 1",
        (business_id, operation, provider, external_reference),
    ).fetchone()
    return None if row is None else _payment_evidence_from_row(row)


def _insert_payment_evidence(
    conn: Any,
    *,
    evidence: _PaymentEvidence,
    recorded_by_member_id: str,
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO business_payment_outcome_evidence(
            business_id, payment_id, operation, idempotency_key,
            request_fingerprint, outcome_event_id, offering_id,
            provider, external_reference, recorded_by_member_id, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            evidence.business_id,
            evidence.payment_id,
            evidence.operation,
            evidence.idempotency_key,
            evidence.request_fingerprint,
            evidence.outcome_event_id,
            evidence.offering_id,
            evidence.provider,
            evidence.external_reference,
            recorded_by_member_id,
            created_at,
        ),
    )


def _resolve(
    conn: Any,
    actor: TenantContext,
    *,
    allowed_roles: frozenset[PlatformRole],
) -> TenantContext:
    current = TenancyRepository(conn).resolve_context(
        user_id=actor.user_id,
        business_id=actor.business_id,
    )
    if current.role not in allowed_roles:
        raise TenantPermissionDenied("operation is not allowed for this business role")
    return current


def _audit(
    conn: Any,
    *,
    actor: TenantContext,
    action: str,
    subject_type: str,
    subject_id: str | None = None,
    detail: str = "",
    now: str | None = None,
) -> None:
    timestamp = str(now or _utc_now())
    conn.execute(
        """
        INSERT INTO clientplatform_admin_audit_events(
            id, business_id, actor_user_id, action, subject_type,
            subject_id, detail, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid4()),
            actor.business_id,
            actor.user_id,
            _text(action, field="action", maximum=120),
            _text(subject_type, field="subject_type", maximum=80),
            subject_id,
            str(detail or "")[:1000],
            timestamp,
        ),
    )


def _native_idempotent_entity_id(
    *,
    business_id: str,
    kind: str,
    idempotency_key: str | None,
) -> str:
    if idempotency_key is None:
        return str(uuid4())
    normalized = _idempotency_key(idempotency_key)
    return str(
        uuid5(
            NAMESPACE_URL,
            f"clientplatform:{kind}:{business_id}:{normalized}",
        )
    )


def create_publication_draft(
    *,
    actor: TenantContext,
    title: str,
    body: str,
    channel: str = "telegram",
    idempotency_key: str | None = None,
) -> PublicationRecord:
    normalized_channel = str(channel or "telegram").strip().lower()
    if normalized_channel not in {"telegram", "vk", "max", "other"}:
        raise ValueError("unsupported publication channel")
    normalized_title = _text(title, field="title", maximum=180)
    normalized_body = _body(body)
    timestamp = _utc_now()

    with get_db() as conn:
        current = _resolve(conn, actor, allowed_roles=_CONTENT_ROLES)
        publication_id = _native_idempotent_entity_id(
            business_id=current.business_id,
            kind="publication",
            idempotency_key=idempotency_key,
        )
        if idempotency_key is not None:
            existing = conn.execute(
                """
                SELECT id, business_id, channel, title, body, status,
                       created_at, updated_at, scheduled_at, published_at,
                       failed_at, failure_reason
                FROM business_publications
                WHERE id=? AND business_id=?
                LIMIT 1
                """,
                (publication_id, current.business_id),
            ).fetchone()
            if existing is not None:
                record = _publication_from_row(existing)
                if (
                    record.channel != normalized_channel
                    or record.title != normalized_title
                    or record.body != normalized_body
                ):
                    raise ValueError(
                        "publication idempotency key belongs to different work"
                    )
                return record
        conn.execute(
            """
            INSERT INTO business_publications(
                id, business_id, channel, title, body, status,
                created_by_member_id, created_at, updated_at,
                scheduled_at, published_at, failed_at, failure_reason
            ) VALUES(?, ?, ?, ?, ?, 'draft', ?, ?, ?, NULL, NULL, NULL, NULL)
            """,
            (
                publication_id,
                current.business_id,
                normalized_channel,
                normalized_title,
                normalized_body,
                current.membership_id,
                timestamp,
                timestamp,
            ),
        )
        _audit(
            conn,
            actor=current,
            action="publication_draft_created",
            subject_type="publication",
            subject_id=publication_id,
            detail=normalized_title,
            now=timestamp,
        )
        row = conn.execute(
            """
            SELECT id, business_id, channel, title, body, status,
                   created_at, updated_at, scheduled_at, published_at,
                   failed_at, failure_reason
            FROM business_publications
            WHERE id=? AND business_id=?
            """,
            (publication_id, current.business_id),
        ).fetchone()
    if row is None:
        raise RuntimeError("created publication was not found")
    return _publication_from_row(row)


def _publication_schedule_mutation_id(
    *, business_id: str, idempotency_key: str
) -> str:
    digest = hashlib.sha256(
        f"{business_id}\x1f{idempotency_key}".encode("utf-8")
    ).hexdigest()
    return f"publication-schedule-mutation:{digest}"


def _publication_schedule_request_hash(
    *, publication_id: str, local_time: str
) -> str:
    return hashlib.sha256(
        f"{publication_id}\x1f{str(local_time).strip()}".encode("utf-8")
    ).hexdigest()


def _record_publication_schedule_mutation(
    conn: Any,
    *,
    actor: TenantContext,
    mutation_id: str,
    publication_id: str,
    request_hash: str,
    scheduled_at: str,
    updated_at: str,
    now: str,
) -> None:
    detail = json.dumps(
        {
            "request_hash": request_hash,
            "scheduled_at": scheduled_at,
            "updated_at": updated_at,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    conn.execute(
        """
        INSERT INTO clientplatform_admin_audit_events(
            id, business_id, actor_user_id, action, subject_type,
            subject_id, detail, created_at
        ) VALUES(?, ?, ?, 'publication_schedule_mutation', 'publication', ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (
            mutation_id,
            actor.business_id,
            actor.user_id,
            publication_id,
            detail,
            now,
        ),
    )
    receipt = conn.execute(
        """
        SELECT actor_user_id, subject_id, detail
        FROM clientplatform_admin_audit_events
        WHERE id=? AND business_id=? AND action='publication_schedule_mutation'
        LIMIT 1
        """,
        (mutation_id, actor.business_id),
    ).fetchone()
    if receipt is None:
        raise RuntimeError("publication scheduling receipt was not persisted")
    if int(_value(receipt, "actor_user_id", 0)) != actor.user_id:
        raise ValueError("publication scheduling idempotency key is not reusable")
    if str(_value(receipt, "subject_id", 1) or "") != publication_id:
        raise ValueError("publication scheduling idempotency key is not reusable")
    if str(_value(receipt, "detail", 2) or "") != detail:
        raise ValueError("publication scheduling idempotency key is not reusable")


def _publication_schedule_replay(
    conn: Any,
    *,
    actor: TenantContext,
    mutation_id: str,
    publication_id: str,
    request_hash: str,
) -> PublicationRecord | None:
    receipt = conn.execute(
        """
        SELECT actor_user_id, subject_id, detail
        FROM clientplatform_admin_audit_events
        WHERE id=? AND business_id=? AND action='publication_schedule_mutation'
        LIMIT 1
        """,
        (mutation_id, actor.business_id),
    ).fetchone()
    if receipt is None:
        return None
    if int(_value(receipt, "actor_user_id", 0)) != actor.user_id:
        raise ValueError("publication scheduling idempotency key is not reusable")
    if str(_value(receipt, "subject_id", 1) or "") != publication_id:
        raise ValueError("publication scheduling idempotency key is not reusable")
    try:
        detail = json.loads(str(_value(receipt, "detail", 2) or ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("publication scheduling receipt is invalid") from exc
    if not isinstance(detail, dict) or detail.get("request_hash") != request_hash:
        raise ValueError("publication scheduling idempotency key is not reusable")
    scheduled_at = detail.get("scheduled_at")
    updated_at = detail.get("updated_at")
    if not isinstance(scheduled_at, str) or not isinstance(updated_at, str):
        raise RuntimeError("publication scheduling receipt is invalid")
    row = conn.execute(
        """
        SELECT id, business_id, channel, title, body, status,
               created_at, updated_at, scheduled_at, published_at,
               failed_at, failure_reason
        FROM business_publications
        WHERE id=? AND business_id=?
        """,
        (publication_id, actor.business_id),
    ).fetchone()
    if row is None:
        raise ValueError("publication is not available for scheduling")
    current = _publication_from_row(row)
    return replace(
        current,
        status="scheduled",
        updated_at=updated_at,
        scheduled_at=scheduled_at,
        published_at=None,
        failed_at=None,
        failure_reason=None,
    )


def schedule_publication(
    *,
    actor: TenantContext,
    publication_id: str,
    local_time: str,
    now: datetime | None = None,
    idempotency_key: str | None = None,
) -> PublicationRecord:
    """Schedule or reschedule one canonical publication in business local time."""

    normalized_id = normalize_uuid(publication_id, field_name="publication_id")
    timestamp = _utc_now()
    normalized_idempotency_key = (
        None
        if idempotency_key is None
        else _text(idempotency_key, field="idempotency_key", maximum=500)
    )
    request_hash = _publication_schedule_request_hash(
        publication_id=normalized_id,
        local_time=local_time,
    )
    with get_db() as conn:
        current = _resolve(conn, actor, allowed_roles=_CONTENT_ROLES)
        mutation_id = (
            None
            if normalized_idempotency_key is None
            else _publication_schedule_mutation_id(
                business_id=current.business_id,
                idempotency_key=normalized_idempotency_key,
            )
        )
        if mutation_id is not None:
            replay = _publication_schedule_replay(
                conn,
                actor=current,
                mutation_id=mutation_id,
                publication_id=normalized_id,
                request_hash=request_hash,
            )
            if replay is not None:
                return replay
        zone = _publication_business_zone(conn, current.business_id)
        scheduled_at = _publication_schedule_utc(local_time, zone=zone, now=now)
        row = conn.execute(
            """
            SELECT status, scheduled_at
            FROM business_publications
            WHERE id=? AND business_id=?
            LIMIT 1
            """,
            (normalized_id, current.business_id),
        ).fetchone()
        if row is None:
            raise ValueError("publication is not available for scheduling")
        status = str(_value(row, "status", 0))
        previous_schedule = _value(row, "scheduled_at", 1)
        previous_schedule_text = (
            None if previous_schedule is None else str(previous_schedule)
        )
        if status == "scheduled" and previous_schedule_text == scheduled_at:
            unchanged = conn.execute(
                """
                SELECT id, business_id, channel, title, body, status,
                       created_at, updated_at, scheduled_at, published_at,
                       failed_at, failure_reason
                FROM business_publications
                WHERE id=? AND business_id=?
                """,
                (normalized_id, current.business_id),
            ).fetchone()
            if unchanged is None:
                raise RuntimeError("scheduled publication was not found")
            unchanged_record = _publication_from_row(unchanged)
            if mutation_id is not None:
                _record_publication_schedule_mutation(
                    conn,
                    actor=current,
                    mutation_id=mutation_id,
                    publication_id=normalized_id,
                    request_hash=request_hash,
                    scheduled_at=scheduled_at,
                    updated_at=unchanged_record.updated_at,
                    now=timestamp,
                )
            return unchanged_record
        if status not in {"draft", "scheduled"}:
            raise ValueError("publication is not available for scheduling")
        if status == "scheduled" and previous_schedule_text is None:
            raise ValueError("scheduled publication has no canonical schedule")

        if status == "draft":
            cursor = conn.execute(
                """
                UPDATE business_publications
                SET status='scheduled', scheduled_at=?, updated_at=?
                WHERE id=? AND business_id=? AND status='draft'
                """,
                (scheduled_at, timestamp, normalized_id, current.business_id),
            )
            action = "publication_scheduled"
        else:
            cursor = conn.execute(
                """
                UPDATE business_publications
                SET scheduled_at=?, updated_at=?
                WHERE id=? AND business_id=?
                  AND status='scheduled' AND scheduled_at=?
                """,
                (
                    scheduled_at,
                    timestamp,
                    normalized_id,
                    current.business_id,
                    previous_schedule_text,
                ),
            )
            action = "publication_rescheduled"
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            concurrent = conn.execute(
                "SELECT status, scheduled_at FROM business_publications WHERE id=? AND business_id=?",
                (normalized_id, current.business_id),
            ).fetchone()
            if concurrent is not None:
                concurrent_status = str(_value(concurrent, "status", 0))
                concurrent_schedule = _value(concurrent, "scheduled_at", 1)
                if (
                    concurrent_status == "scheduled"
                    and str(concurrent_schedule or "") == scheduled_at
                ):
                    result = conn.execute(
                        """
                        SELECT id, business_id, channel, title, body, status,
                               created_at, updated_at, scheduled_at, published_at,
                               failed_at, failure_reason
                        FROM business_publications
                        WHERE id=? AND business_id=?
                        """,
                        (normalized_id, current.business_id),
                    ).fetchone()
                    if result is None:
                        raise RuntimeError("scheduled publication was not found")
                    concurrent_record = _publication_from_row(result)
                    if mutation_id is not None:
                        _record_publication_schedule_mutation(
                            conn,
                            actor=current,
                            mutation_id=mutation_id,
                            publication_id=normalized_id,
                            request_hash=request_hash,
                            scheduled_at=scheduled_at,
                            updated_at=concurrent_record.updated_at,
                            now=timestamp,
                        )
                    return concurrent_record
            raise ValueError("publication changed concurrently; refresh and retry")
        if mutation_id is not None:
            _record_publication_schedule_mutation(
                conn,
                actor=current,
                mutation_id=mutation_id,
                publication_id=normalized_id,
                request_hash=request_hash,
                scheduled_at=scheduled_at,
                updated_at=timestamp,
                now=timestamp,
            )
        _audit(
            conn,
            actor=current,
            action=action,
            subject_type="publication",
            subject_id=normalized_id,
            detail=scheduled_at,
            now=timestamp,
        )
        result = conn.execute(
            """
            SELECT id, business_id, channel, title, body, status,
                   created_at, updated_at, scheduled_at, published_at,
                   failed_at, failure_reason
            FROM business_publications
            WHERE id=? AND business_id=?
            """,
            (normalized_id, current.business_id),
        ).fetchone()
    if result is None:
        raise RuntimeError("scheduled publication was not found")
    return _publication_from_row(result)


def cancel_publication_schedule(
    *,
    actor: TenantContext,
    publication_id: str,
    expected_scheduled_at: str,
) -> PublicationRecord:
    """Cancel a scheduled publication without starting any delivery worker."""

    normalized_id = normalize_uuid(publication_id, field_name="publication_id")
    timestamp = _utc_now()
    with get_db() as conn:
        current = _resolve(conn, actor, allowed_roles=_CONTENT_ROLES)
        row = conn.execute(
            """
            SELECT status, scheduled_at
            FROM business_publications
            WHERE id=? AND business_id=?
            LIMIT 1
            """,
            (normalized_id, current.business_id),
        ).fetchone()
        if row is None:
            raise ValueError("publication is not available for cancellation")
        status = str(_value(row, "status", 0))
        previous_schedule = _value(row, "scheduled_at", 1)
        previous_schedule_text = (
            None if previous_schedule is None else str(previous_schedule)
        )
        if status == "cancelled":
            if previous_schedule_text is None or (
                encode_publication_schedule_version(previous_schedule_text)
                != encode_publication_schedule_version(expected_scheduled_at)
            ):
                raise ValueError("publication schedule changed; refresh and retry")
            unchanged = conn.execute(
                """
                SELECT id, business_id, channel, title, body, status,
                       created_at, updated_at, scheduled_at, published_at,
                       failed_at, failure_reason
                FROM business_publications
                WHERE id=? AND business_id=?
                """,
                (normalized_id, current.business_id),
            ).fetchone()
            if unchanged is None:
                raise RuntimeError("cancelled publication was not found")
            return _publication_from_row(unchanged)
        if status != "scheduled" or previous_schedule_text is None:
            raise ValueError("only a scheduled publication can be cancelled")
        if (
            encode_publication_schedule_version(previous_schedule_text)
            != encode_publication_schedule_version(expected_scheduled_at)
        ):
            raise ValueError("publication schedule changed; refresh and retry")
        cursor = conn.execute(
            """
            UPDATE business_publications
            SET status='cancelled', updated_at=?
            WHERE id=? AND business_id=?
              AND status='scheduled' AND scheduled_at=?
            """,
            (timestamp, normalized_id, current.business_id, previous_schedule_text),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            concurrent = conn.execute(
                "SELECT status, scheduled_at FROM business_publications WHERE id=? AND business_id=?",
                (normalized_id, current.business_id),
            ).fetchone()
            concurrent_status = (
                None if concurrent is None else str(_value(concurrent, "status", 0))
            )
            concurrent_schedule = (
                None if concurrent is None else _value(concurrent, "scheduled_at", 1)
            )
            concurrent_schedule_text = (
                None if concurrent_schedule is None else str(concurrent_schedule)
            )
            if (
                concurrent_status == "cancelled"
                and concurrent_schedule_text is not None
                and encode_publication_schedule_version(concurrent_schedule_text)
                == encode_publication_schedule_version(expected_scheduled_at)
            ):
                result = conn.execute(
                    """
                    SELECT id, business_id, channel, title, body, status,
                           created_at, updated_at, scheduled_at, published_at,
                           failed_at, failure_reason
                    FROM business_publications
                    WHERE id=? AND business_id=?
                    """,
                    (normalized_id, current.business_id),
                ).fetchone()
                if result is None:
                    raise RuntimeError("cancelled publication was not found")
                return _publication_from_row(result)
            raise ValueError("publication changed concurrently; refresh and retry")
        _audit(
            conn,
            actor=current,
            action="publication_schedule_cancelled",
            subject_type="publication",
            subject_id=normalized_id,
            detail=previous_schedule_text,
            now=timestamp,
        )
        result = conn.execute(
            """
            SELECT id, business_id, channel, title, body, status,
                   created_at, updated_at, scheduled_at, published_at,
                   failed_at, failure_reason
            FROM business_publications
            WHERE id=? AND business_id=?
            """,
            (normalized_id, current.business_id),
        ).fetchone()
    if result is None:
        raise RuntimeError("cancelled publication was not found")
    return _publication_from_row(result)


def publish_publication(
    *,
    actor: TenantContext,
    publication_id: str,
) -> PublicationRecord:
    normalized_id = normalize_uuid(publication_id, field_name="publication_id")
    timestamp = _utc_now()
    with get_db() as conn:
        current = _resolve(conn, actor, allowed_roles=_CONTENT_ROLES)
        existing = conn.execute(
            """
            SELECT status FROM business_publications
            WHERE id=? AND business_id=?
            LIMIT 1
            """,
            (normalized_id, current.business_id),
        ).fetchone()
        if existing is None or str(_value(existing, "status", 0)) not in {
            "draft",
            "scheduled",
            "failed",
        }:
            raise ValueError("publication is not available for publishing")
        conn.execute(
            """
            UPDATE business_publications
            SET status='published', published_at=?, updated_at=?,
                failed_at=NULL, failure_reason=NULL
            WHERE id=? AND business_id=?
            """,
            (timestamp, timestamp, normalized_id, current.business_id),
        )
        _audit(
            conn,
            actor=current,
            action="publication_published",
            subject_type="publication",
            subject_id=normalized_id,
            now=timestamp,
        )
        row = conn.execute(
            """
            SELECT id, business_id, channel, title, body, status,
                   created_at, updated_at, scheduled_at, published_at,
                   failed_at, failure_reason
            FROM business_publications
            WHERE id=? AND business_id=?
            """,
            (normalized_id, current.business_id),
        ).fetchone()
    if row is None:
        raise RuntimeError("published publication was not found")
    return _publication_from_row(row)


def list_publications(
    *,
    actor: TenantContext,
    limit: int = 20,
) -> list[PublicationRecord]:
    normalized_limit = max(1, min(int(limit), 100))
    with get_db_ro() as conn:
        current = _resolve(conn, actor, allowed_roles=_CONTENT_ROLES)
        rows = conn.execute(
            """
            SELECT id, business_id, channel, title, body, status,
                   created_at, updated_at, scheduled_at, published_at,
                   failed_at, failure_reason
            FROM business_publications
            WHERE business_id=?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (current.business_id, normalized_limit),
        ).fetchall()
    return [_publication_from_row(row) for row in rows]


def get_publication_calendar_projection(
    *,
    actor: TenantContext,
    upcoming_limit: int = 4,
    recent_limit: int = 4,
    actionable_draft_limit: int = 5,
) -> PublicationCalendarProjection:
    """Project bounded display partitions and actions from canonical publications."""

    normalized_upcoming = max(1, min(int(upcoming_limit), 20))
    normalized_recent = max(1, min(int(recent_limit), 20))
    normalized_drafts = max(1, min(int(actionable_draft_limit), 20))
    select_fields = """
        SELECT id, business_id, channel, title, body, status,
               created_at, updated_at, scheduled_at, published_at,
               failed_at, failure_reason
        FROM business_publications
    """
    with get_db_ro() as conn:
        current = _resolve(conn, actor, allowed_roles=_CONTENT_ROLES)
        count_rows = conn.execute(
            """
            SELECT status, COUNT(*) AS c
            FROM business_publications
            WHERE business_id=?
            GROUP BY status
            """,
            (current.business_id,),
        ).fetchall()
        counts = {
            str(_value(row, "status", 0)): int(_value(row, "c", 1))
            for row in count_rows
        }
        upcoming_rows = conn.execute(
            select_fields
            + """
            WHERE business_id=? AND status='scheduled' AND scheduled_at IS NOT NULL
            ORDER BY scheduled_at ASC, id ASC
            LIMIT ?
            """,
            (current.business_id, normalized_upcoming),
        ).fetchall()
        recent_rows = conn.execute(
            select_fields
            + """
            WHERE business_id=?
              AND NOT (status='scheduled' AND scheduled_at IS NOT NULL)
            ORDER BY COALESCE(published_at, failed_at, updated_at, created_at) DESC, id DESC
            LIMIT ?
            """,
            (current.business_id, normalized_recent),
        ).fetchall()
        draft_rows = conn.execute(
            select_fields
            + """
            WHERE business_id=? AND status='draft'
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (current.business_id, normalized_drafts),
        ).fetchall()

    upcoming = tuple(_publication_from_row(row) for row in upcoming_rows)
    recent = tuple(_publication_from_row(row) for row in recent_rows)
    actionable_drafts = tuple(_publication_from_row(row) for row in draft_rows)
    return PublicationCalendarProjection(
        entries=upcoming + recent,
        actionable_drafts=actionable_drafts,
        draft_count=counts.get("draft", 0),
        scheduled_count=counts.get("scheduled", 0),
        published_count=counts.get("published", 0),
        failed_count=counts.get("failed", 0),
        cancelled_count=counts.get("cancelled", 0),
    )


def list_publication_calendar(
    *,
    actor: TenantContext,
    limit: int = 20,
) -> list[PublicationRecord]:
    """Compatibility view for callers that only need bounded calendar entries."""

    normalized_limit = max(2, min(int(limit), 40))
    upcoming_limit = max(1, normalized_limit // 2)
    recent_limit = max(1, normalized_limit - upcoming_limit)
    projection = get_publication_calendar_projection(
        actor=actor,
        upcoming_limit=upcoming_limit,
        recent_limit=recent_limit,
    )
    return list(projection.entries)


def _publication_from_row(row: Any) -> PublicationRecord:
    scheduled_at = _value(row, "scheduled_at", 8)
    published_at = _value(row, "published_at", 9)
    failed_at = _value(row, "failed_at", 10)
    failure_reason = _value(row, "failure_reason", 11)
    return PublicationRecord(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        channel=str(_value(row, "channel", 2)),
        title=str(_value(row, "title", 3)),
        body=str(_value(row, "body", 4)),
        status=str(_value(row, "status", 5)),
        created_at=str(_value(row, "created_at", 6)),
        updated_at=str(_value(row, "updated_at", 7)),
        scheduled_at=None if scheduled_at is None else str(scheduled_at),
        published_at=None if published_at is None else str(published_at),
        failed_at=None if failed_at is None else str(failed_at),
        failure_reason=None if failure_reason is None else str(failure_reason),
    )


_PAYMENT_SELECT = """
    SELECT p.id, p.business_id, p.customer_id, p.amount_minor, p.currency,
           p.status, p.provider, p.external_reference, p.note,
           p.created_at, p.paid_at, p.refunded_at,
           paid_evidence.idempotency_key AS idempotency_key,
           paid_evidence.outcome_event_id AS outcome_event_id,
           paid_evidence.offering_id AS offering_id,
           paid_revenue.id AS revenue_attribution_id,
           refunded_evidence.idempotency_key AS refund_idempotency_key,
           refunded_evidence.outcome_event_id AS refund_outcome_event_id,
           refunded_revenue.id AS refund_revenue_attribution_id
    FROM business_payments p
    LEFT JOIN business_payment_outcome_evidence paid_evidence
      ON paid_evidence.business_id=p.business_id
     AND paid_evidence.payment_id=p.id
     AND paid_evidence.operation='paid'
    LEFT JOIN revenue_attributions paid_revenue
      ON paid_revenue.business_id=paid_evidence.business_id
     AND paid_revenue.outcome_event_id=paid_evidence.outcome_event_id
     AND paid_revenue.model_version='first_touch_v1'
    LEFT JOIN business_payment_outcome_evidence refunded_evidence
      ON refunded_evidence.business_id=p.business_id
     AND refunded_evidence.payment_id=p.id
     AND refunded_evidence.operation='refund'
    LEFT JOIN revenue_attributions refunded_revenue
      ON refunded_revenue.business_id=refunded_evidence.business_id
     AND refunded_revenue.outcome_event_id=refunded_evidence.outcome_event_id
     AND refunded_revenue.model_version='first_touch_v1'
"""


def _payment_from_row(row: Any) -> PaymentRecord:
    customer_id = _value(row, "customer_id", 2)
    external_reference = _value(row, "external_reference", 7)
    paid_at = _value(row, "paid_at", 10)
    refunded_at = _value(row, "refunded_at", 11)
    idempotency_key = _value(row, "idempotency_key", 12)
    outcome_event_id = _value(row, "outcome_event_id", 13)
    offering_id = _value(row, "offering_id", 14)
    revenue_attribution_id = _value(row, "revenue_attribution_id", 15)
    refund_idempotency_key = _value(row, "refund_idempotency_key", 16)
    refund_outcome_event_id = _value(row, "refund_outcome_event_id", 17)
    refund_revenue_attribution_id = _value(
        row,
        "refund_revenue_attribution_id",
        18,
    )
    return PaymentRecord(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        customer_id=None if customer_id is None else str(customer_id),
        amount_minor=int(_value(row, "amount_minor", 3)),
        currency=str(_value(row, "currency", 4)),
        status=str(_value(row, "status", 5)),
        provider=str(_value(row, "provider", 6)),
        external_reference=(
            None if external_reference is None else str(external_reference)
        ),
        note=str(_value(row, "note", 8)),
        created_at=str(_value(row, "created_at", 9)),
        paid_at=None if paid_at is None else str(paid_at),
        refunded_at=None if refunded_at is None else str(refunded_at),
        idempotency_key=(
            None if idempotency_key is None else str(idempotency_key)
        ),
        outcome_event_id=(
            None if outcome_event_id is None else str(outcome_event_id)
        ),
        offering_id=None if offering_id is None else str(offering_id),
        revenue_attribution_id=(
            None
            if revenue_attribution_id is None
            else str(revenue_attribution_id)
        ),
        refund_idempotency_key=(
            None
            if refund_idempotency_key is None
            else str(refund_idempotency_key)
        ),
        refund_outcome_event_id=(
            None
            if refund_outcome_event_id is None
            else str(refund_outcome_event_id)
        ),
        refund_revenue_attribution_id=(
            None
            if refund_revenue_attribution_id is None
            else str(refund_revenue_attribution_id)
        ),
    )


def _payment_row(
    conn: Any,
    *,
    business_id: str,
    payment_id: str,
) -> PaymentRecord | None:
    row = conn.execute(
        _PAYMENT_SELECT + " WHERE p.business_id=? AND p.id=? LIMIT 1",
        (business_id, payment_id),
    ).fetchone()
    return None if row is None else _payment_from_row(row)


def _outcome_idempotency_key(*, operation: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return f"business-payment-{operation}:{digest}"


def _validate_payment_outcome(
    conn: Any,
    *,
    payment: PaymentRecord,
    evidence: _PaymentEvidence,
) -> None:
    if evidence.operation == "paid" and (
        evidence.provider != payment.provider
        or evidence.external_reference != payment.external_reference
    ):
        raise PaymentEvidenceInvariantViolation(
            "payment provider evidence disagrees with the durable payment"
        )
    event = OutcomeRepository(conn).get(
        business_id=evidence.business_id,
        event_id=evidence.outcome_event_id,
    )
    if event is None:
        raise PaymentEvidenceInvariantViolation(
            "payment evidence has no canonical outcome"
        )
    expected_type = (
        OutcomeType.ORDER_PAID
        if evidence.operation == "paid"
        else OutcomeType.REFUND_RECORDED
    )
    expected_subject = (
        f"business_offering:{evidence.offering_id}"
        if evidence.offering_id is not None
        else f"business_payment:{payment.id}"
    )
    expected_key = _outcome_idempotency_key(
        operation=evidence.operation,
        idempotency_key=evidence.idempotency_key,
    )
    if (
        event.outcome_type != expected_type
        or event.source_type != "business_payment"
        or event.source_id != payment.id
        or event.customer_id != payment.customer_id
        or event.subject_ref != expected_subject
        or event.amount_minor != payment.amount_minor
        or event.currency != payment.currency
        or event.idempotency_key != expected_key
    ):
        raise PaymentEvidenceInvariantViolation(
            "payment and canonical outcome evidence disagree"
        )


def _replay_payment(
    conn: Any,
    *,
    evidence: _PaymentEvidence,
    operation: str,
    request_fingerprint: str,
    materialized_at: datetime,
) -> PaymentRecord:
    if (
        evidence.operation != operation
        or evidence.request_fingerprint != request_fingerprint
    ):
        raise PaymentIdempotencyConflict(
            "idempotency key already belongs to a different payment fact"
        )
    payment = _payment_row(
        conn,
        business_id=evidence.business_id,
        payment_id=evidence.payment_id,
    )
    if payment is None:
        raise PaymentEvidenceInvariantViolation(
            "payment evidence has no durable payment"
        )
    _validate_payment_outcome(conn, payment=payment, evidence=evidence)
    RevenueAttributionRepository(conn).materialize_outcome(
        business_id=evidence.business_id,
        outcome_event_id=evidence.outcome_event_id,
        created_at=materialized_at,
    )
    refreshed = _payment_row(
        conn,
        business_id=evidence.business_id,
        payment_id=evidence.payment_id,
    )
    if refreshed is None:
        raise PaymentEvidenceInvariantViolation("durable payment disappeared")
    return refreshed


def _resolve_payment_offering(
    conn: Any,
    *,
    business_id: str,
    offering_id: str | None,
    currency: str,
) -> tuple[int | None, str | None]:
    if offering_id is None:
        return None, None
    row = conn.execute(
        """
        SELECT p.amount_minor, p.currency
        FROM business_offerings o
        LEFT JOIN business_offering_prices p
          ON p.business_id=o.business_id
         AND p.offering_id=o.id
         AND p.status='active'
        WHERE o.id=? AND o.business_id=? AND o.status='active'
        LIMIT 1
        """,
        (offering_id, business_id),
    ).fetchone()
    if row is None:
        raise ValueError("active offering was not found in this business")
    configured_amount = _value(row, "amount_minor", 0)
    configured_currency = _value(row, "currency", 1)
    if configured_currency is not None and str(configured_currency) != currency:
        raise PaymentStateConflict(
            "payment currency does not match the active offering price"
        )
    return (
        None if configured_amount is None else int(configured_amount),
        None if configured_currency is None else str(configured_currency),
    )


def _assert_provider_reference_available(
    conn: Any,
    *,
    business_id: str,
    operation: str,
    provider: str,
    external_reference: str | None,
) -> None:
    if external_reference is None:
        return
    evidence = _provider_evidence(
        conn,
        business_id=business_id,
        operation=operation,
        provider=provider,
        external_reference=external_reference,
    )
    if evidence is not None:
        raise PaymentIdempotencyConflict(
            "provider reference already belongs to a different idempotency key"
        )
    if operation != "paid":
        return
    legacy = conn.execute(
        """
        SELECT id
        FROM business_payments
        WHERE business_id=? AND provider=? AND external_reference=?
        LIMIT 1
        """,
        (business_id, provider, external_reference),
    ).fetchone()
    if legacy is not None:
        raise PaymentIdempotencyConflict(
            "provider reference already belongs to a payment without this evidence"
        )


def record_payment(
    *,
    actor: TenantContext,
    amount_minor: int,
    idempotency_key: str,
    currency: str = "RUB",
    customer_id: str | None = None,
    offering_id: str | None = None,
    note: str = "",
    provider: str = "manual",
    external_reference: str | None = None,
    now: datetime | None = None,
) -> PaymentRecord:
    """Confirm one customer payment and append its canonical money fact atomically."""

    normalized_amount = _amount_minor(amount_minor)
    normalized_currency = _currency(currency)
    normalized_key = _idempotency_key(idempotency_key)
    normalized_provider = _provider(provider)
    normalized_reference = _external_reference(
        external_reference,
        provider=normalized_provider,
    )
    normalized_customer = (
        None
        if customer_id in (None, "")
        else normalize_uuid(str(customer_id), field_name="customer_id")
    )
    normalized_offering = (
        None
        if offering_id in (None, "")
        else normalize_uuid(str(offering_id), field_name="offering_id")
    )
    normalized_note = str(note or "").replace("\x00", " ").strip()[:500]
    stamp = _payment_stamp(now)
    timestamp = stamp.isoformat(timespec="microseconds")
    requested_at = None if now is None else timestamp
    fingerprint = _request_fingerprint(
        {
            "operation": "paid",
            "amount_minor": normalized_amount,
            "currency": normalized_currency,
            "customer_id": normalized_customer,
            "offering_id": normalized_offering,
            "note": normalized_note,
            "provider": normalized_provider,
            "external_reference": normalized_reference,
            "occurred_at": requested_at,
        }
    )

    with get_db() as conn:
        _begin_payment_write(conn)
        current = _resolve(conn, actor, allowed_roles=_FINANCE_WRITE_ROLES)
        _serialize_payment_write(conn, business_id=current.business_id)
        replay = _evidence_by_key(
            conn,
            business_id=current.business_id,
            idempotency_key=normalized_key,
        )
        if replay is not None:
            return _replay_payment(
                conn,
                evidence=replay,
                operation="paid",
                request_fingerprint=fingerprint,
                materialized_at=stamp,
            )

        if normalized_customer is not None:
            customer = conn.execute(
                """
                SELECT id FROM customers
                WHERE id=? AND business_id=? AND status='active'
                LIMIT 1
                """,
                (normalized_customer, current.business_id),
            ).fetchone()
            if customer is None:
                raise ValueError("active customer was not found in this business")
        configured_amount, configured_currency = _resolve_payment_offering(
            conn,
            business_id=current.business_id,
            offering_id=normalized_offering,
            currency=normalized_currency,
        )
        _assert_provider_reference_available(
            conn,
            business_id=current.business_id,
            operation="paid",
            provider=normalized_provider,
            external_reference=normalized_reference,
        )

        payment_id = str(uuid4())
        conn.execute(
            """
            INSERT INTO business_payments(
                id, business_id, customer_id, amount_minor, currency,
                status, provider, external_reference, note,
                recorded_by_member_id, created_at, updated_at, paid_at, refunded_at
            ) VALUES(?, ?, ?, ?, ?, 'paid', ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                payment_id,
                current.business_id,
                normalized_customer,
                normalized_amount,
                normalized_currency,
                normalized_provider,
                normalized_reference,
                normalized_note,
                current.membership_id,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        subject_ref = (
            f"business_offering:{normalized_offering}"
            if normalized_offering is not None
            else f"business_payment:{payment_id}"
        )
        outcome = OutcomeRepository(conn).append(
            BusinessOutcomeEvent(
                id=str(uuid4()),
                business_id=current.business_id,
                outcome_type=OutcomeType.ORDER_PAID,
                occurred_at=stamp,
                source=OutcomeSource(
                    source_type="business_payment",
                    source_id=payment_id,
                ),
                customer_id=normalized_customer,
                subject_ref=subject_ref,
                money=OutcomeMoney(
                    amount_minor=normalized_amount,
                    currency=normalized_currency,
                ),
                idempotency_key=_outcome_idempotency_key(
                    operation="paid",
                    idempotency_key=normalized_key,
                ),
                metadata={
                    "payment_id": payment_id,
                    "offering_id": normalized_offering,
                    "provider": normalized_provider,
                    "external_reference": normalized_reference,
                    "configured_price_amount_minor": configured_amount,
                    "configured_price_currency": configured_currency,
                },
                metadata_version=1,
                created_at=stamp,
            )
        )
        evidence = _PaymentEvidence(
            business_id=current.business_id,
            payment_id=payment_id,
            operation="paid",
            idempotency_key=normalized_key,
            request_fingerprint=fingerprint,
            outcome_event_id=outcome.id,
            offering_id=normalized_offering,
            provider=normalized_provider,
            external_reference=normalized_reference,
        )
        _insert_payment_evidence(
            conn,
            evidence=evidence,
            recorded_by_member_id=current.membership_id,
            created_at=timestamp,
        )
        RevenueAttributionRepository(conn).materialize_outcome(
            business_id=current.business_id,
            outcome_event_id=outcome.id,
            created_at=stamp,
        )
        _audit(
            conn,
            actor=current,
            action="payment_recorded",
            subject_type="payment",
            subject_id=payment_id,
            detail=(
                f"{normalized_amount}:{normalized_currency}:"
                f"outcome={outcome.id}"
            ),
            now=timestamp,
        )
        payment = _payment_row(
            conn,
            business_id=current.business_id,
            payment_id=payment_id,
        )
        if payment is None:
            raise PaymentEvidenceInvariantViolation(
                "recorded payment was not found"
            )
        _validate_payment_outcome(conn, payment=payment, evidence=evidence)
        return payment


def refund_payment(
    *,
    actor: TenantContext,
    payment_id: str,
    idempotency_key: str,
    reason: str = "",
    provider: str = "manual",
    external_reference: str | None = None,
    now: datetime | None = None,
) -> PaymentRecord:
    """Record one full refund and its separate canonical negative money fact."""

    normalized_payment_id = normalize_uuid(payment_id, field_name="payment_id")
    normalized_key = _idempotency_key(idempotency_key)
    normalized_provider = _provider(provider)
    normalized_reference = _external_reference(
        external_reference,
        provider=normalized_provider,
    )
    normalized_reason = str(reason or "").replace("\x00", " ").strip()[:500]
    stamp = _payment_stamp(now)
    timestamp = stamp.isoformat(timespec="microseconds")
    requested_at = None if now is None else timestamp
    fingerprint = _request_fingerprint(
        {
            "operation": "refund",
            "payment_id": normalized_payment_id,
            "reason": normalized_reason,
            "provider": normalized_provider,
            "external_reference": normalized_reference,
            "occurred_at": requested_at,
        }
    )

    with get_db() as conn:
        _begin_payment_write(conn)
        current = _resolve(conn, actor, allowed_roles=_FINANCE_WRITE_ROLES)
        _serialize_payment_write(conn, business_id=current.business_id)
        replay = _evidence_by_key(
            conn,
            business_id=current.business_id,
            idempotency_key=normalized_key,
        )
        if replay is not None:
            return _replay_payment(
                conn,
                evidence=replay,
                operation="refund",
                request_fingerprint=fingerprint,
                materialized_at=stamp,
            )

        payment = _payment_row(
            conn,
            business_id=current.business_id,
            payment_id=normalized_payment_id,
        )
        if payment is None:
            raise ValueError("payment was not found in this business")
        paid_evidence = _evidence_for_payment(
            conn,
            business_id=current.business_id,
            payment_id=payment.id,
            operation="paid",
        )
        if paid_evidence is None:
            raise PaymentEvidenceInvariantViolation(
                "payment has no canonical paid outcome and cannot be refunded"
            )
        _validate_payment_outcome(
            conn,
            payment=payment,
            evidence=paid_evidence,
        )
        if payment.status != "paid":
            raise PaymentStateConflict("payment is not refundable")
        _assert_provider_reference_available(
            conn,
            business_id=current.business_id,
            operation="refund",
            provider=normalized_provider,
            external_reference=normalized_reference,
        )

        subject_ref = (
            f"business_offering:{paid_evidence.offering_id}"
            if paid_evidence.offering_id is not None
            else f"business_payment:{payment.id}"
        )
        outcome = OutcomeRepository(conn).append(
            BusinessOutcomeEvent(
                id=str(uuid4()),
                business_id=current.business_id,
                outcome_type=OutcomeType.REFUND_RECORDED,
                occurred_at=stamp,
                source=OutcomeSource(
                    source_type="business_payment",
                    source_id=payment.id,
                ),
                customer_id=payment.customer_id,
                subject_ref=subject_ref,
                money=OutcomeMoney(
                    amount_minor=payment.amount_minor,
                    currency=payment.currency,
                ),
                idempotency_key=_outcome_idempotency_key(
                    operation="refund",
                    idempotency_key=normalized_key,
                ),
                metadata={
                    "payment_id": payment.id,
                    "payment_outcome_event_id": paid_evidence.outcome_event_id,
                    "offering_id": paid_evidence.offering_id,
                    "provider": normalized_provider,
                    "external_reference": normalized_reference,
                    "reason": normalized_reason,
                },
                metadata_version=1,
                created_at=stamp,
            )
        )
        evidence = _PaymentEvidence(
            business_id=current.business_id,
            payment_id=payment.id,
            operation="refund",
            idempotency_key=normalized_key,
            request_fingerprint=fingerprint,
            outcome_event_id=outcome.id,
            offering_id=paid_evidence.offering_id,
            provider=normalized_provider,
            external_reference=normalized_reference,
        )
        _insert_payment_evidence(
            conn,
            evidence=evidence,
            recorded_by_member_id=current.membership_id,
            created_at=timestamp,
        )
        cursor = conn.execute(
            """
            UPDATE business_payments
            SET status='refunded', updated_at=?, refunded_at=?
            WHERE id=? AND business_id=? AND status='paid'
            """,
            (timestamp, timestamp, payment.id, current.business_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise PaymentStateConflict("payment refund lost a concurrent race")
        RevenueAttributionRepository(conn).materialize_outcome(
            business_id=current.business_id,
            outcome_event_id=outcome.id,
            created_at=stamp,
        )
        _audit(
            conn,
            actor=current,
            action="payment_refunded",
            subject_type="payment",
            subject_id=payment.id,
            detail=(
                f"{payment.amount_minor}:{payment.currency}:"
                f"outcome={outcome.id}"
            ),
            now=timestamp,
        )
        refunded = _payment_row(
            conn,
            business_id=current.business_id,
            payment_id=payment.id,
        )
        if refunded is None:
            raise PaymentEvidenceInvariantViolation("refunded payment disappeared")
        _validate_payment_outcome(
            conn,
            payment=refunded,
            evidence=evidence,
        )
        return refunded


def list_payments(
    *,
    actor: TenantContext,
    limit: int = 30,
) -> list[PaymentRecord]:
    normalized_limit = max(1, min(int(limit), 100))
    with get_db_ro() as conn:
        current = _resolve(conn, actor, allowed_roles=_FINANCE_READ_ROLES)
        rows = conn.execute(
            _PAYMENT_SELECT
            + " WHERE p.business_id=? "
            "ORDER BY p.created_at DESC, p.id DESC LIMIT ?",
            (current.business_id, normalized_limit),
        ).fetchall()
    return [_payment_from_row(row) for row in rows]


def set_offering_price(
    *,
    actor: TenantContext,
    offering_id: str,
    amount_minor: int,
    currency: str = "RUB",
) -> OfferingPrice:
    normalized_offering = normalize_uuid(offering_id, field_name="offering_id")
    normalized_amount = _amount_minor(amount_minor)
    normalized_currency = _currency(currency)
    timestamp = _utc_now()

    with get_db() as conn:
        current = _resolve(conn, actor, allowed_roles=_FINANCE_WRITE_ROLES)
        offering = conn.execute(
            """
            SELECT title FROM business_offerings
            WHERE id=? AND business_id=? AND status='active'
            LIMIT 1
            """,
            (normalized_offering, current.business_id),
        ).fetchone()
        if offering is None:
            raise ValueError("active offering was not found in this business")
        existing = conn.execute(
            """
            SELECT id FROM business_offering_prices
            WHERE business_id=? AND offering_id=?
            LIMIT 1
            """,
            (current.business_id, normalized_offering),
        ).fetchone()
        if existing is None:
            price_id = str(uuid4())
            conn.execute(
                """
                INSERT INTO business_offering_prices(
                    id, business_id, offering_id, amount_minor, currency,
                    status, created_by_member_id, created_at, updated_at, archived_at
                ) VALUES(?, ?, ?, ?, ?, 'active', ?, ?, ?, NULL)
                """,
                (
                    price_id,
                    current.business_id,
                    normalized_offering,
                    normalized_amount,
                    normalized_currency,
                    current.membership_id,
                    timestamp,
                    timestamp,
                ),
            )
        else:
            price_id = str(_value(existing, "id", 0))
            conn.execute(
                """
                UPDATE business_offering_prices
                SET amount_minor=?, currency=?, status='active',
                    updated_at=?, archived_at=NULL
                WHERE id=? AND business_id=? AND offering_id=?
                """,
                (
                    normalized_amount,
                    normalized_currency,
                    timestamp,
                    price_id,
                    current.business_id,
                    normalized_offering,
                ),
            )
        _audit(
            conn,
            actor=current,
            action="offering_price_set",
            subject_type="offering",
            subject_id=normalized_offering,
            detail=f"{normalized_amount}:{normalized_currency}",
            now=timestamp,
        )
        row = conn.execute(
            """
            SELECT p.id, p.business_id, p.offering_id, o.title,
                   p.amount_minor, p.currency, p.status, p.updated_at
            FROM business_offering_prices p
            JOIN business_offerings o
              ON o.id=p.offering_id AND o.business_id=p.business_id
            WHERE p.id=? AND p.business_id=?
            """,
            (price_id, current.business_id),
        ).fetchone()
    if row is None:
        raise RuntimeError("offering price was not found")
    return _price_from_row(row)


def list_offering_prices(*, actor: TenantContext) -> list[OfferingPrice]:
    with get_db_ro() as conn:
        current = _resolve(conn, actor, allowed_roles=_FINANCE_READ_ROLES)
        rows = conn.execute(
            """
            SELECT p.id, p.business_id, p.offering_id, o.title,
                   p.amount_minor, p.currency, p.status, p.updated_at
            FROM business_offering_prices p
            JOIN business_offerings o
              ON o.id=p.offering_id AND o.business_id=p.business_id
            WHERE p.business_id=? AND p.status='active' AND o.status='active'
            ORDER BY o.title, p.id
            """,
            (current.business_id,),
        ).fetchall()
    return [_price_from_row(row) for row in rows]


def _price_from_row(row: Any) -> OfferingPrice:
    return OfferingPrice(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        offering_id=str(_value(row, "offering_id", 2)),
        offering_title=str(_value(row, "title", 3)),
        amount_minor=int(_value(row, "amount_minor", 4)),
        currency=str(_value(row, "currency", 5)),
        status=str(_value(row, "status", 6)),
        updated_at=str(_value(row, "updated_at", 7)),
    )


def get_subscription_state(*, actor: TenantContext) -> SubscriptionState:
    timestamp = _utc_now()
    with get_db() as conn:
        current = _resolve(
            conn,
            actor,
            allowed_roles=frozenset({PlatformRole.OWNER}),
        )
        conn.execute(
            """
            INSERT INTO business_subscription_state(
                business_id, plan_key, status, included_staff, included_customers,
                started_at, renews_at, updated_by_member_id, updated_at
            ) VALUES(?, 'base', 'active', 5, 500, ?, NULL, ?, ?)
            ON CONFLICT(business_id) DO NOTHING
            """,
            (
                current.business_id,
                timestamp,
                current.membership_id,
                timestamp,
            ),
        )
        row = conn.execute(
            """
            SELECT business_id, plan_key, status, included_staff,
                   included_customers, started_at, renews_at, updated_at
            FROM business_subscription_state
            WHERE business_id=?
            """,
            (current.business_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("subscription state was not found")
    renews_at = _value(row, "renews_at", 6)
    return SubscriptionState(
        business_id=str(_value(row, "business_id", 0)),
        plan_key=str(_value(row, "plan_key", 1)),
        status=str(_value(row, "status", 2)),
        included_staff=int(_value(row, "included_staff", 3)),
        included_customers=int(_value(row, "included_customers", 4)),
        started_at=str(_value(row, "started_at", 5)),
        renews_at=None if renews_at is None else str(renews_at),
        updated_at=str(_value(row, "updated_at", 7)),
    )


def get_admin_setting(
    *,
    actor: TenantContext,
    key: str,
    default: str,
) -> str:
    normalized_key = _text(key, field="setting_key", maximum=120)
    with get_db_ro() as conn:
        current = _resolve(conn, actor, allowed_roles=_OBSERVABILITY_ROLES)
        row = conn.execute(
            """
            SELECT setting_value FROM business_admin_settings
            WHERE business_id=? AND setting_key=?
            """,
            (current.business_id, normalized_key),
        ).fetchone()
    if row is None:
        return str(default)
    return str(_value(row, "setting_value", 0))


def set_admin_setting(
    *,
    actor: TenantContext,
    key: str,
    value: str,
) -> str:
    normalized_key = _text(key, field="setting_key", maximum=120)
    normalized_value = _text(value, field="setting_value", maximum=1000)
    timestamp = _utc_now()
    with get_db() as conn:
        current = _resolve(conn, actor, allowed_roles=_CONTENT_ROLES)
        conn.execute(
            """
            INSERT INTO business_admin_settings(
                business_id, setting_key, setting_value,
                updated_by_member_id, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(business_id, setting_key) DO UPDATE SET
                setting_value=excluded.setting_value,
                updated_by_member_id=excluded.updated_by_member_id,
                updated_at=excluded.updated_at
            """,
            (
                current.business_id,
                normalized_key,
                normalized_value,
                current.membership_id,
                timestamp,
                timestamp,
            ),
        )
        _audit(
            conn,
            actor=current,
            action="admin_setting_updated",
            subject_type="setting",
            subject_id=normalized_key,
            detail=normalized_value,
            now=timestamp,
        )
    return normalized_value


def get_autopilot_enabled(*, actor: TenantContext) -> bool:
    return is_owner_autopilot_enabled(actor=actor)


def toggle_autopilot(*, actor: TenantContext) -> bool:
    """Toggle the owner-approved canonical AutomationPolicy mode.

    The legacy `business_admin_settings.autopilot_enabled` flag is deliberately
    no longer authoritative: it could not carry limits, version or owner approval.
    """

    return toggle_owner_autopilot(actor=actor)


def set_autopilot_enabled(*, actor: TenantContext, enabled: bool) -> bool:
    """Set the owner policy to a desired state; repeated calls are safe."""

    current = is_owner_autopilot_enabled(actor=actor)
    if current == enabled:
        return current
    set_owner_autopilot_enabled(actor=actor, enabled=enabled)
    return enabled


def _automation_money_text(amount_minor: int, currency: str) -> str:
    normalized = normalize_settlement_currency(currency)
    exponent = settlement_currency_minor_unit_exponent(normalized)
    scale = 10**exponent
    whole, fraction = divmod(int(amount_minor), scale)
    if exponent == 0:
        return f"{whole} {normalized}"
    return f"{whole},{fraction:0{exponent}d} {normalized}"


def format_automation_action_approval(item: AutomationActionApproval, *, timezone_name: str) -> str:
    candidate = item.candidate
    action = _AUTOMATION_ACTION_LABELS.get(candidate.action, "Выполнить автоматическое действие")
    channel = _AUTOMATION_CHANNEL_LABELS.get(candidate.channel or "", candidate.channel or "канал не указан")
    if item.status.value == "pending":
        status = "Нужно Ваше подтверждение"
    else:
        status = "Разрешено владельцем · ещё не исполняется автоматически"
    details = [f"{status}: {action}", f"Канал: {channel}"]
    if candidate.subject_ref is not None and candidate.payload_digest is not None:
        details.extend(
            [
                f"Цель: {candidate.subject_ref}",
                f"Отпечаток содержимого: {candidate.payload_digest}",
            ]
        )
    if candidate.amount_minor is not None and candidate.currency is not None:
        details.append(f"Сумма: {_automation_money_text(candidate.amount_minor, candidate.currency)}")
    if item.status.value == "pending":
        reasons = [
            _AUTOMATION_APPROVAL_REASON_LABELS.get(reason, "требуется подтверждение владельца")
            for reason in item.approval_reasons
        ]
        details.append("Почему: " + "; ".join(dict.fromkeys(reasons)))
    details.append(
        "Действует до: "
        + _format_publication_timestamp(item.expires_at, timezone_name=timezone_name)
    )
    return "\n".join(details)


def get_current_automation_action_approvals(*, actor: TenantContext) -> tuple[AutomationActionApproval, ...]:
    return list_current_automation_action_approvals(actor=actor, limit=20)


def approve_pending_automation_action(*, actor: TenantContext, approval_id: str) -> AutomationActionApproval:
    approval = get_automation_action_approval(actor=actor, approval_id=approval_id)
    return approve_automation_action(
        actor=actor,
        approval_id=approval.id,
        expected_request_fingerprint=approval.request_fingerprint,
    )


def reject_pending_automation_action(*, actor: TenantContext, approval_id: str) -> AutomationActionApproval:
    approval = get_automation_action_approval(actor=actor, approval_id=approval_id)
    return reject_automation_action(
        actor=actor,
        approval_id=approval.id,
        expected_request_fingerprint=approval.request_fingerprint,
    )


def revoke_approved_automation_action(*, actor: TenantContext, approval_id: str) -> AutomationActionApproval:
    approval = get_automation_action_approval(actor=actor, approval_id=approval_id)
    return revoke_automation_action_approval(
        actor=actor,
        approval_id=approval.id,
        expected_request_fingerprint=approval.request_fingerprint,
    )


def record_interaction_metric(metric: InteractionMetricInput) -> None:
    normalized_business = normalize_uuid(metric.business_id, field_name="business_id")
    timestamp = _utc_now()
    with get_db() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=int(metric.actor_user_id),
            business_id=normalized_business,
        )
        conn.execute(
            """
            INSERT INTO clientplatform_admin_interaction_metrics(
                id, business_id, actor_user_id, callback_action, success,
                ack_ms, lock_wait_ms, app_ms, telegram_ms, total_ms,
                transport_role, transport_route, transport_generation,
                error_code, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                current.business_id,
                current.user_id,
                _text(metric.callback_action, field="callback_action", maximum=160),
                1 if metric.success else 0,
                max(0, int(metric.ack_ms)),
                max(0, int(metric.lock_wait_ms)),
                max(0, int(metric.app_ms)),
                max(0, int(metric.telegram_ms)),
                max(0, int(metric.total_ms)),
                _text(metric.transport_role or "ui", field="transport_role", maximum=40),
                _text(metric.transport_route or "unknown", field="transport_route", maximum=180),
                metric.transport_generation,
                None if metric.error_code in (None, "") else str(metric.error_code)[:160],
                timestamp,
            ),
        )


def interaction_snapshot(
    *,
    actor: TenantContext,
    window_minutes: int = 60,
) -> InteractionSnapshot:
    normalized_window = max(1, min(int(window_minutes), 10080))
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=normalized_window)
    ).isoformat(timespec="seconds")
    with get_db_ro() as conn:
        current = _resolve(conn, actor, allowed_roles=_OBSERVABILITY_ROLES)
        rows = conn.execute(
            """
            SELECT success, ack_ms, lock_wait_ms, telegram_ms, total_ms
            FROM clientplatform_admin_interaction_metrics
            WHERE business_id=? AND created_at>=?
            ORDER BY created_at, id
            """,
            (current.business_id, cutoff),
        ).fetchall()
    totals = sorted(int(_value(row, "total_ms", 4)) for row in rows)
    acknowledgements = sorted(int(_value(row, "ack_ms", 1)) for row in rows)
    locks = sorted(int(_value(row, "lock_wait_ms", 2)) for row in rows)
    telegram = sorted(int(_value(row, "telegram_ms", 3)) for row in rows)
    successes = sum(int(_value(row, "success", 0)) == 1 for row in rows)
    return InteractionSnapshot(
        count=len(rows),
        successes=successes,
        failures=len(rows) - successes,
        p50_ms=_percentile(totals, 0.50),
        p95_ms=_percentile(totals, 0.95),
        max_ms=totals[-1] if totals else 0,
        ack_p95_ms=_percentile(acknowledgements, 0.95),
        lock_p95_ms=_percentile(locks, 0.95),
        telegram_p95_ms=_percentile(telegram, 0.95),
        window_minutes=normalized_window,
    )


def _percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    index = max(0, math.ceil(len(values) * fraction) - 1)
    return int(values[index])


def refresh_interaction_alerts(
    *,
    actor: TenantContext,
    window_minutes: int = 60,
    p95_warning_ms: int | None = None,
    failure_percent_warning: float | None = None,
    route_redundant: bool | None = None,
) -> list[AdminAlert]:
    snapshot = interaction_snapshot(actor=actor, window_minutes=window_minutes)
    p95_limit = (
        int(p95_warning_ms)
        if p95_warning_ms is not None
        else int(os.getenv("CLIENTPLATFORM_ADMIN_P95_ALERT_MS", "1000"))
    )
    failure_limit = (
        float(failure_percent_warning)
        if failure_percent_warning is not None
        else float(os.getenv("CLIENTPLATFORM_ADMIN_FAILURE_ALERT_PERCENT", "1"))
    )
    failure_percent = (
        0.0 if snapshot.count == 0 else snapshot.failures * 100.0 / snapshot.count
    )
    desired: dict[str, tuple[str, str]] = {}
    if snapshot.count >= 5 and snapshot.p95_ms > p95_limit:
        desired["interaction_latency"] = (
            "warning",
            f"p95 кнопок {snapshot.p95_ms} мс выше порога {p95_limit} мс",
        )
    if snapshot.count >= 5 and failure_percent > failure_limit:
        desired["interaction_failures"] = (
            "critical",
            f"Ошибки кнопок {failure_percent:.1f}% выше порога {failure_limit:.1f}%",
        )
    if route_redundant is False:
        desired["telegram_route_redundancy"] = (
            "warning",
            "У Telegram сейчас только один подтверждённый сетевой маршрут",
        )

    timestamp = _utc_now()
    with get_db() as conn:
        current = _resolve(conn, actor, allowed_roles=_OBSERVABILITY_ROLES)
        autopilot_enabled = AutomationPolicyRepository(conn).autopilot_enabled_projection(
            actor=current,
            now=timestamp,
        )
        if autopilot_enabled:
            operations = {
                "active_customers": (
                    "SELECT COUNT(*) AS c FROM customers "
                    "WHERE business_id=? AND status='active'"
                ),
                "active_offerings": (
                    "SELECT COUNT(*) AS c FROM business_offerings "
                    "WHERE business_id=? AND status='active'"
                ),
                "priced_offerings": (
                    "SELECT COUNT(*) AS c FROM business_offering_prices "
                    "WHERE business_id=? AND status='active'"
                ),
                "published": (
                    "SELECT COUNT(*) AS c FROM business_publications "
                    "WHERE business_id=? AND status='published'"
                ),
                "enrolled_customers": (
                    "SELECT COUNT(DISTINCT customer_id) AS c FROM enrollments "
                    "WHERE business_id=? AND status IN ('active','completed')"
                ),
                "stalled": (
                    "SELECT COUNT(DISTINCT e.customer_id) AS c "
                    "FROM enrollments e JOIN lesson_progress lp "
                    "ON lp.enrollment_id=e.id AND lp.business_id=e.business_id "
                    "WHERE e.business_id=? AND e.status='active' "
                    "AND lp.status IN ('pending','delivered','opened')"
                ),
            }
            counts: dict[str, int] = {}
            for name, sql in operations.items():
                count_row = conn.execute(sql, (current.business_id,)).fetchone()
                counts[name] = (
                    0
                    if count_row is None
                    else int(_value(count_row, "c", 0) or 0)
                )
            if counts["active_offerings"] > counts["priced_offerings"]:
                desired["growth_unpriced_offerings"] = (
                    "warning",
                    "Автопилот: у части предложений не задана цена",
                )
            if counts["published"] == 0:
                desired["growth_no_publications"] = (
                    "warning",
                    "Автопилот: нет ни одной отмеченной публикации",
                )
            if counts["active_customers"] > counts["enrolled_customers"]:
                desired["growth_customers_without_program"] = (
                    "warning",
                    "Автопилот: есть клиенты без активной программы",
                )
            if counts["stalled"] > 0:
                desired["growth_stalled_customers"] = (
                    "warning",
                    f"Автопилот: незавершённых клиентских прохождений — {counts['stalled']}",
                )

        existing_rows = conn.execute(
            """
            SELECT id, kind, status, occurrences, severity, message
            FROM clientplatform_admin_alerts
            WHERE business_id=?
            """,
            (current.business_id,),
        ).fetchall()
        existing = {
            str(_value(row, "kind", 1)): row
            for row in existing_rows
        }
        for kind, (severity, message) in desired.items():
            row = existing.get(kind)
            if row is None:
                conn.execute(
                    """
                    INSERT INTO clientplatform_admin_alerts(
                        id, business_id, kind, severity, message, status,
                        occurrences, first_seen_at, last_seen_at, resolved_at
                    ) VALUES(?, ?, ?, ?, ?, 'open', 1, ?, ?, NULL)
                    """,
                    (
                        str(uuid4()),
                        current.business_id,
                        kind,
                        severity,
                        message,
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                was_open = str(_value(row, "status", 2)) == "open"
                same_payload = (
                    str(_value(row, "severity", 4)) == severity
                    and str(_value(row, "message", 5)) == message
                )
                occurrences = int(_value(row, "occurrences", 3))
                if not was_open or not same_payload:
                    occurrences += 1
                conn.execute(
                    """
                    UPDATE clientplatform_admin_alerts
                    SET severity=?, message=?, status='open',
                        occurrences=?, last_seen_at=?, resolved_at=NULL
                    WHERE id=? AND business_id=?
                    """,
                    (
                        severity,
                        message,
                        occurrences,
                        timestamp,
                        str(_value(row, "id", 0)),
                        current.business_id,
                    ),
                )
        for kind, row in existing.items():
            if kind in desired or str(_value(row, "status", 2)) != "open":
                continue
            conn.execute(
                """
                UPDATE clientplatform_admin_alerts
                SET status='resolved', resolved_at=?, last_seen_at=?
                WHERE id=? AND business_id=?
                """,
                (
                    timestamp,
                    timestamp,
                    str(_value(row, "id", 0)),
                    current.business_id,
                ),
            )
    return list_open_alerts(actor=actor)


def list_open_alerts(*, actor: TenantContext) -> list[AdminAlert]:
    with get_db_ro() as conn:
        current = _resolve(conn, actor, allowed_roles=_OBSERVABILITY_ROLES)
        rows = conn.execute(
            """
            SELECT id, kind, severity, message, occurrences,
                   first_seen_at, last_seen_at
            FROM clientplatform_admin_alerts
            WHERE business_id=? AND status='open'
            ORDER BY CASE severity WHEN 'critical' THEN 0 ELSE 1 END,
                     last_seen_at DESC, id
            """,
            (current.business_id,),
        ).fetchall()
    return [
        AdminAlert(
            id=str(_value(row, "id", 0)),
            kind=str(_value(row, "kind", 1)),
            severity=str(_value(row, "severity", 2)),
            message=str(_value(row, "message", 3)),
            occurrences=int(_value(row, "occurrences", 4)),
            first_seen_at=str(_value(row, "first_seen_at", 5)),
            last_seen_at=str(_value(row, "last_seen_at", 6)),
        )
        for row in rows
    ]


def payment_summary(*, actor: TenantContext) -> PaymentSummary:
    """Return complete paid-payment facts without truncating recent rows."""

    with get_db_ro() as conn:
        current = _resolve(conn, actor, allowed_roles=_FINANCE_READ_ROLES)
        rows = conn.execute(
            """
            SELECT currency, COUNT(*) AS paid_payments,
                   COALESCE(SUM(amount_minor), 0) AS amount_minor
            FROM business_payments
            WHERE business_id=? AND status='paid'
            GROUP BY currency
            ORDER BY currency
            """,
            (current.business_id,),
        ).fetchall()
        customer_row = conn.execute(
            """
            SELECT COUNT(DISTINCT customer_id) AS paid_customers
            FROM business_payments
            WHERE business_id=? AND status='paid' AND customer_id IS NOT NULL
            """,
            (current.business_id,),
        ).fetchone()
    by_currency = tuple(
        PaymentCurrencySummary(
            currency=str(_value(row, "currency", 0)).upper(),
            paid_payments=int(_value(row, "paid_payments", 1) or 0),
            amount_minor=int(_value(row, "amount_minor", 2) or 0),
        )
        for row in rows
    )
    return PaymentSummary(
        paid_payments=sum(item.paid_payments for item in by_currency),
        paid_customers=(
            0
            if customer_row is None
            else int(_value(customer_row, "paid_customers", 0) or 0)
        ),
        by_currency=by_currency,
    )


def business_admin_insights(*, actor: TenantContext) -> AdminInsightSnapshot:
    with get_db_ro() as conn:
        current = _resolve(conn, actor, allowed_roles=_OBSERVABILITY_ROLES)

        def count(sql: str, params: tuple[object, ...]) -> int:
            row = conn.execute(sql, params).fetchone()
            if row is None:
                return 0
            return int(_value(row, "c", 0) or 0)

        business_id = current.business_id
        amount_row = conn.execute(
            """
            SELECT COALESCE(SUM(amount_minor), 0) AS amount, currency
            FROM business_payments
            WHERE business_id=? AND status='paid'
            GROUP BY currency
            ORDER BY COUNT(*) DESC, currency
            LIMIT 1
            """,
            (business_id,),
        ).fetchone()
        amount = 0 if amount_row is None else int(_value(amount_row, "amount", 0) or 0)
        currency = (
            "RUB"
            if amount_row is None
            else str(_value(amount_row, "currency", 1) or "RUB")
        )
        return AdminInsightSnapshot(
            active_customers=count(
                "SELECT COUNT(*) AS c FROM customers WHERE business_id=? AND status='active'",
                (business_id,),
            ),
            active_offerings=count(
                "SELECT COUNT(*) AS c FROM business_offerings WHERE business_id=? AND status='active'",
                (business_id,),
            ),
            active_invites=count(
                "SELECT COUNT(*) AS c FROM customer_invites WHERE business_id=? AND status='active'",
                (business_id,),
            ),
            claimed_invites=count(
                "SELECT COUNT(*) AS c FROM customer_invites WHERE business_id=? AND status='claimed'",
                (business_id,),
            ),
            enrollments=count(
                "SELECT COUNT(*) AS c FROM enrollments WHERE business_id=?",
                (business_id,),
            ),
            completed_enrollments=count(
                "SELECT COUNT(*) AS c FROM enrollments WHERE business_id=? AND status='completed'",
                (business_id,),
            ),
            publication_drafts=count(
                "SELECT COUNT(*) AS c FROM business_publications WHERE business_id=? AND status='draft'",
                (business_id,),
            ),
            publications_published=count(
                "SELECT COUNT(*) AS c FROM business_publications WHERE business_id=? AND status='published'",
                (business_id,),
            ),
            paid_payments=count(
                "SELECT COUNT(*) AS c FROM business_payments WHERE business_id=? AND status='paid'",
                (business_id,),
            ),
            paid_amount_minor=amount,
            payment_currency=currency,
            priced_offerings=count(
                "SELECT COUNT(*) AS c FROM business_offering_prices WHERE business_id=? AND status='active'",
                (business_id,),
            ),
            active_staff=count(
                "SELECT COUNT(*) AS c FROM business_members WHERE business_id=? AND status='active'",
                (business_id,),
            ),
        )


def recent_audit_events(
    *,
    actor: TenantContext,
    limit: int = 20,
) -> list[AuditEvent]:
    normalized_limit = max(1, min(int(limit), 100))
    with get_db_ro() as conn:
        current = _resolve(conn, actor, allowed_roles=_OBSERVABILITY_ROLES)
        rows = conn.execute(
            """
            SELECT action, subject_type, subject_id, detail, created_at
            FROM clientplatform_admin_audit_events
            WHERE business_id=?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (current.business_id, normalized_limit),
        ).fetchall()
    return [
        AuditEvent(
            action=str(_value(row, "action", 0)),
            subject_type=str(_value(row, "subject_type", 1)),
            subject_id=(
                None
                if _value(row, "subject_id", 2) is None
                else str(_value(row, "subject_id", 2))
            ),
            detail=str(_value(row, "detail", 3)),
            created_at=str(_value(row, "created_at", 4)),
        )
        for row in rows
    ]


def purge_old_interaction_metrics(*, retention_days: int = 14) -> int:
    normalized_days = max(1, min(int(retention_days), 365))
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=normalized_days)
    ).isoformat(timespec="seconds")
    with get_db() as conn:
        cursor = conn.execute(
            """
            DELETE FROM clientplatform_admin_interaction_metrics
            WHERE created_at<?
            """,
            (cutoff,),
        )
        return int(getattr(cursor, "rowcount", 0) or 0)


__all__ = [
    "AdminAlert",
    "AdminInsightSnapshot",
    "AuditEvent",
    "InteractionMetricInput",
    "InteractionSnapshot",
    "OfferingPrice",
    "PaymentEvidenceInvariantViolation",
    "PaymentIdempotencyConflict",
    "PaymentCurrencySummary",
    "PaymentRecord",
    "PaymentSummary",
    "PaymentStateConflict",
    "PublicationCalendarProjection",
    "PublicationRecord",
    "SubscriptionState",
    "business_admin_insights",
    "cancel_publication_schedule",
    "create_publication_draft",
    "format_publication_calendar_lines",
    "get_admin_setting",
    "get_publication_calendar_projection",
    "get_subscription_state",
    "interaction_snapshot",
    "list_offering_prices",
    "list_open_alerts",
    "list_payments",
    "payment_summary",
    "list_publication_calendar",
    "list_publications",
    "publish_publication",
    "purge_old_interaction_metrics",
    "recent_audit_events",
    "record_interaction_metric",
    "record_payment",
    "refund_payment",
    "schedule_publication",
    "refresh_interaction_alerts",
    "set_admin_setting",
    "set_offering_price",
    "get_autopilot_enabled",
    "set_autopilot_enabled",
    "toggle_autopilot",
]
