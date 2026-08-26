from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.money import normalize_settlement_currency
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
    published_at: str | None


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


def create_publication_draft(
    *,
    actor: TenantContext,
    title: str,
    body: str,
    channel: str = "telegram",
) -> PublicationRecord:
    normalized_channel = str(channel or "telegram").strip().lower()
    if normalized_channel not in {"telegram", "vk", "max", "other"}:
        raise ValueError("unsupported publication channel")
    normalized_title = _text(title, field="title", maximum=180)
    normalized_body = _body(body)
    timestamp = _utc_now()
    publication_id = str(uuid4())

    with get_db() as conn:
        current = _resolve(conn, actor, allowed_roles=_CONTENT_ROLES)
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
                   created_at, updated_at, published_at
            FROM business_publications
            WHERE id=? AND business_id=?
            """,
            (publication_id, current.business_id),
        ).fetchone()
    if row is None:
        raise RuntimeError("created publication was not found")
    return _publication_from_row(row)


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
                   created_at, updated_at, published_at
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
                   created_at, updated_at, published_at
            FROM business_publications
            WHERE business_id=?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (current.business_id, normalized_limit),
        ).fetchall()
    return [_publication_from_row(row) for row in rows]


def _publication_from_row(row: Any) -> PublicationRecord:
    published_at = _value(row, "published_at", 8)
    return PublicationRecord(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        channel=str(_value(row, "channel", 2)),
        title=str(_value(row, "title", 3)),
        body=str(_value(row, "body", 4)),
        status=str(_value(row, "status", 5)),
        created_at=str(_value(row, "created_at", 6)),
        updated_at=str(_value(row, "updated_at", 7)),
        published_at=None if published_at is None else str(published_at),
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


def toggle_autopilot(*, actor: TenantContext) -> bool:
    current = get_admin_setting(actor=actor, key="autopilot_enabled", default="false")
    enabled = current.strip().lower() not in {"1", "true", "yes", "on"}
    set_admin_setting(
        actor=actor,
        key="autopilot_enabled",
        value="true" if enabled else "false",
    )
    return enabled


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
    p95_limit = int(
        p95_warning_ms
        if p95_warning_ms is not None
        else os.getenv("CLIENTPLATFORM_ADMIN_P95_ALERT_MS", "1000")
    )
    failure_limit = float(
        failure_percent_warning
        if failure_percent_warning is not None
        else os.getenv("CLIENTPLATFORM_ADMIN_FAILURE_ALERT_PERCENT", "1")
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
        autopilot_row = conn.execute(
            """
            SELECT setting_value FROM business_admin_settings
            WHERE business_id=? AND setting_key='autopilot_enabled'
            LIMIT 1
            """,
            (current.business_id,),
        ).fetchone()
        autopilot_enabled = bool(
            autopilot_row is not None
            and str(_value(autopilot_row, "setting_value", 0)).strip().lower()
            in {"1", "true", "yes", "on"}
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
    "PaymentRecord",
    "PaymentStateConflict",
    "PublicationRecord",
    "SubscriptionState",
    "business_admin_insights",
    "create_publication_draft",
    "get_admin_setting",
    "get_subscription_state",
    "interaction_snapshot",
    "list_offering_prices",
    "list_open_alerts",
    "list_payments",
    "list_publications",
    "publish_publication",
    "purge_old_interaction_metrics",
    "recent_audit_events",
    "record_interaction_metric",
    "record_payment",
    "refund_payment",
    "refresh_interaction_alerts",
    "set_admin_setting",
    "set_offering_price",
    "toggle_autopilot",
]
