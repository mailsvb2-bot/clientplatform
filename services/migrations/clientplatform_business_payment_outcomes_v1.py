from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from clientplatform.domain.money import normalize_settlement_currency
from clientplatform.domain.outcomes import (
    BusinessOutcomeEvent,
    OutcomeMoney,
    OutcomeSource,
    OutcomeType,
)
from clientplatform.infrastructure.outcome_repository import OutcomeRepository
from clientplatform.infrastructure.revenue_attribution_repository import (
    RevenueAttributionRepository,
)
from services.migrations._helpers import mark_migration, migration_applied


NAME = "clientplatform_business_payment_outcomes_v1"
_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9._-]{0,39}$")
_LEGACY_REFUND_PROVIDER = "legacy_migration"


@dataclass(frozen=True, slots=True)
class PaymentOutcomeBackfillReport:
    payments_scanned: int
    paid_evidence_created: int
    refund_evidence_created: int


@dataclass(frozen=True, slots=True)
class _Payment:
    id: str
    business_id: str
    customer_id: str | None
    amount_minor: int
    currency: str
    status: str
    provider: str
    external_reference: str | None
    recorded_by_member_id: str
    created_at: datetime
    updated_at: datetime
    paid_at: datetime
    refunded_at: datetime | None


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _timestamp(value: object, *, field: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise RuntimeError(f"legacy payment {field} is missing")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"legacy payment {field} is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _optional_timestamp(value: object, *, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    return _timestamp(value, field=field)


def _payment_from_row(row: Any) -> _Payment:
    payment_id = str(_value(row, "id", 0) or "").strip()
    business_id = str(_value(row, "business_id", 1) or "").strip()
    member_id = str(_value(row, "recorded_by_member_id", 8) or "").strip()
    if not payment_id or not business_id or not member_id:
        raise RuntimeError("legacy payment identity is incomplete")
    provider = str(_value(row, "provider", 6) or "").strip()
    if not _PROVIDER_RE.fullmatch(provider):
        raise RuntimeError("legacy payment provider is invalid")
    customer_value = _value(row, "customer_id", 2)
    external_value = _value(row, "external_reference", 7)
    amount_minor = int(_value(row, "amount_minor", 3))
    if amount_minor <= 0:
        raise RuntimeError("legacy payment amount_minor is invalid")
    created_at = _timestamp(_value(row, "created_at", 9), field="created_at")
    updated_at = _timestamp(_value(row, "updated_at", 10), field="updated_at")
    paid_at = _optional_timestamp(_value(row, "paid_at", 11), field="paid_at")
    refunded_at = _optional_timestamp(
        _value(row, "refunded_at", 12),
        field="refunded_at",
    )
    return _Payment(
        id=payment_id,
        business_id=business_id,
        customer_id=None if customer_value is None else str(customer_value),
        amount_minor=amount_minor,
        currency=normalize_settlement_currency(_value(row, "currency", 4)),
        status=str(_value(row, "status", 5)),
        provider=provider,
        external_reference=(
            None if external_value is None else str(external_value)
        ),
        recorded_by_member_id=member_id,
        created_at=created_at,
        updated_at=updated_at,
        paid_at=paid_at or created_at,
        refunded_at=refunded_at,
    )


def _evidence_key(
    *,
    business_id: str,
    payment_id: str,
    operation: str,
) -> str:
    digest = hashlib.sha256(f"{business_id}:{payment_id}".encode("utf-8")).hexdigest()
    return f"legacy-business-payment-{operation}:{digest}"


def _outcome_key(*, evidence_key: str, operation: str) -> str:
    digest = hashlib.sha256(evidence_key.encode("utf-8")).hexdigest()
    return f"business-payment-{operation}:{digest}"


def _fingerprint(
    payment: _Payment,
    *,
    operation: str,
    occurred_at: datetime,
) -> str:
    payload = {
        "migration": NAME,
        "operation": operation,
        "payment_id": payment.id,
        "business_id": payment.business_id,
        "customer_id": payment.customer_id,
        "amount_minor": payment.amount_minor,
        "currency": payment.currency,
        "provider": payment.provider,
        "external_reference": payment.external_reference,
        "occurred_at": occurred_at.isoformat(timespec="microseconds"),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _event_id(payment: _Payment, *, operation: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"clientplatform:{payment.business_id}:payment:{payment.id}:{operation}",
        )
    )


def _evidence_for_payment(
    conn: sqlite3.Connection,
    *,
    payment: _Payment,
    operation: str,
) -> Any | None:
    return conn.execute(
        """
        SELECT business_id, payment_id, operation, idempotency_key,
               request_fingerprint, outcome_event_id, offering_id,
               provider, external_reference
        FROM business_payment_outcome_evidence
        WHERE business_id=? AND payment_id=? AND operation=?
        LIMIT 1
        """,
        (payment.business_id, payment.id, operation),
    ).fetchone()


def _validate_evidence(
    conn: sqlite3.Connection,
    *,
    payment: _Payment,
    operation: str,
    evidence: Any,
    paid_offering_id: str | None = None,
) -> None:
    offering_value = _value(evidence, "offering_id", 6)
    offering_id = None if offering_value is None else str(offering_value)
    if operation == "refund" and offering_id != paid_offering_id:
        raise RuntimeError("legacy payment refund offering evidence disagrees")
    provider = str(_value(evidence, "provider", 7))
    external_value = _value(evidence, "external_reference", 8)
    external_reference = None if external_value is None else str(external_value)
    if operation == "paid" and (
        provider != payment.provider
        or external_reference != payment.external_reference
    ):
        raise RuntimeError("legacy payment provider evidence disagrees")
    event = OutcomeRepository(conn).get(
        business_id=payment.business_id,
        event_id=str(_value(evidence, "outcome_event_id", 5)),
    )
    if event is None:
        raise RuntimeError("legacy payment evidence has no outcome")
    expected_type = (
        OutcomeType.ORDER_PAID
        if operation == "paid"
        else OutcomeType.REFUND_RECORDED
    )
    expected_subject = (
        f"business_offering:{offering_id}"
        if offering_id is not None
        else f"business_payment:{payment.id}"
    )
    expected_key = _outcome_key(
        evidence_key=str(_value(evidence, "idempotency_key", 3)),
        operation=operation,
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
        raise RuntimeError("legacy payment and outcome evidence disagree")


def _insert_evidence(
    conn: sqlite3.Connection,
    *,
    payment: _Payment,
    operation: str,
    occurred_at: datetime,
    backfilled_at: datetime,
    offering_id: str | None,
    provider: str,
    external_reference: str | None,
    paid_outcome_event_id: str | None = None,
) -> Any:
    evidence_key = _evidence_key(
        business_id=payment.business_id,
        payment_id=payment.id,
        operation=operation,
    )
    outcome_type = (
        OutcomeType.ORDER_PAID
        if operation == "paid"
        else OutcomeType.REFUND_RECORDED
    )
    subject_ref = (
        f"business_offering:{offering_id}"
        if offering_id is not None
        else f"business_payment:{payment.id}"
    )
    metadata: dict[str, object] = {
        "payment_id": payment.id,
        "offering_id": offering_id,
        "provider": provider,
        "external_reference": external_reference,
        "confirmation_source": "legacy_business_payment",
        "migration": NAME,
    }
    if paid_outcome_event_id is not None:
        metadata["payment_outcome_event_id"] = paid_outcome_event_id
    outcome = OutcomeRepository(conn).append(
        BusinessOutcomeEvent(
            id=_event_id(payment, operation=operation),
            business_id=payment.business_id,
            outcome_type=outcome_type,
            occurred_at=occurred_at,
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
            idempotency_key=_outcome_key(
                evidence_key=evidence_key,
                operation=operation,
            ),
            metadata=metadata,
            metadata_version=1,
            created_at=backfilled_at,
        )
    )
    conn.execute(
        """
        INSERT INTO business_payment_outcome_evidence(
            business_id, payment_id, operation, idempotency_key,
            request_fingerprint, outcome_event_id, offering_id,
            provider, external_reference, recorded_by_member_id, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payment.business_id,
            payment.id,
            operation,
            evidence_key,
            _fingerprint(
                payment,
                operation=operation,
                occurred_at=occurred_at,
            ),
            outcome.id,
            offering_id,
            provider,
            external_reference,
            payment.recorded_by_member_id,
            backfilled_at.isoformat(timespec="microseconds"),
        ),
    )
    evidence = _evidence_for_payment(
        conn,
        payment=payment,
        operation=operation,
    )
    if evidence is None:
        raise RuntimeError("legacy payment evidence was not persisted")
    return evidence


def _audit_backfill(
    conn: sqlite3.Connection,
    *,
    payment: _Payment,
    operation: str,
    outcome_event_id: str,
    backfilled_at: datetime,
) -> None:
    member = conn.execute(
        """
        SELECT user_id FROM business_members
        WHERE id=? AND business_id=?
        LIMIT 1
        """,
        (payment.recorded_by_member_id, payment.business_id),
    ).fetchone()
    if member is None:
        raise RuntimeError("legacy payment recorder is missing")
    audit_id = str(
        uuid5(
            NAMESPACE_URL,
            f"clientplatform:{payment.business_id}:payment:{payment.id}:"
            f"{operation}:backfill-audit",
        )
    )
    conn.execute(
        """
        INSERT INTO clientplatform_admin_audit_events(
            id, business_id, actor_user_id, action, subject_type,
            subject_id, detail, created_at
        ) VALUES(?, ?, ?, 'payment_outcome_backfilled', 'payment', ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (
            audit_id,
            payment.business_id,
            int(_value(member, "user_id", 0)),
            payment.id,
            f"{operation}:{payment.amount_minor}:{payment.currency}:"
            f"outcome={outcome_event_id}",
            backfilled_at.isoformat(timespec="microseconds"),
        ),
    )


def reconcile_business_payment_outcomes(
    conn: sqlite3.Connection,
) -> PaymentOutcomeBackfillReport:
    """Backfill canonical outcomes for pre-M4 durable business payments."""

    backfilled_at = datetime.now(timezone.utc)
    rows = conn.execute(
        """
        SELECT id, business_id, customer_id, amount_minor, currency,
               status, provider, external_reference, recorded_by_member_id,
               created_at, updated_at, paid_at, refunded_at
        FROM business_payments
        WHERE status IN ('paid', 'refunded')
        ORDER BY business_id, created_at, id
        """
    ).fetchall()
    paid_created = 0
    refund_created = 0
    for row in rows:
        payment = _payment_from_row(row)
        paid_evidence = _evidence_for_payment(
            conn,
            payment=payment,
            operation="paid",
        )
        if paid_evidence is None:
            paid_evidence = _insert_evidence(
                conn,
                payment=payment,
                operation="paid",
                occurred_at=payment.paid_at,
                backfilled_at=backfilled_at,
                offering_id=None,
                provider=payment.provider,
                external_reference=payment.external_reference,
            )
            paid_created += 1
            _audit_backfill(
                conn,
                payment=payment,
                operation="paid",
                outcome_event_id=str(_value(paid_evidence, "outcome_event_id", 5)),
                backfilled_at=backfilled_at,
            )
        _validate_evidence(
            conn,
            payment=payment,
            operation="paid",
            evidence=paid_evidence,
        )
        RevenueAttributionRepository(conn).materialize_outcome(
            business_id=payment.business_id,
            outcome_event_id=str(_value(paid_evidence, "outcome_event_id", 5)),
            created_at=backfilled_at,
        )

        paid_offering_value = _value(paid_evidence, "offering_id", 6)
        paid_offering_id = (
            None if paid_offering_value is None else str(paid_offering_value)
        )
        refund_evidence = _evidence_for_payment(
            conn,
            payment=payment,
            operation="refund",
        )
        if payment.status != "refunded":
            if refund_evidence is not None:
                raise RuntimeError("non-refunded legacy payment has refund evidence")
            continue
        refund_at = payment.refunded_at or payment.updated_at
        if refund_at < payment.paid_at:
            raise RuntimeError("legacy payment refund predates payment")
        if refund_evidence is None:
            refund_evidence = _insert_evidence(
                conn,
                payment=payment,
                operation="refund",
                occurred_at=refund_at,
                backfilled_at=backfilled_at,
                offering_id=paid_offering_id,
                provider=_LEGACY_REFUND_PROVIDER,
                external_reference=payment.id,
                paid_outcome_event_id=str(
                    _value(paid_evidence, "outcome_event_id", 5)
                ),
            )
            refund_created += 1
            _audit_backfill(
                conn,
                payment=payment,
                operation="refund",
                outcome_event_id=str(
                    _value(refund_evidence, "outcome_event_id", 5)
                ),
                backfilled_at=backfilled_at,
            )
        _validate_evidence(
            conn,
            payment=payment,
            operation="refund",
            evidence=refund_evidence,
            paid_offering_id=paid_offering_id,
        )
        RevenueAttributionRepository(conn).materialize_outcome(
            business_id=payment.business_id,
            outcome_event_id=str(_value(refund_evidence, "outcome_event_id", 5)),
            created_at=backfilled_at,
        )

    return PaymentOutcomeBackfillReport(
        payments_scanned=len(rows),
        paid_evidence_created=paid_created,
        refund_evidence_created=refund_created,
    )


def apply(conn: sqlite3.Connection) -> None:
    log = logging.getLogger(__name__)
    if migration_applied(conn, NAME):
        log.info("Migration skipped (already applied): %s", NAME)
        return
    log.info("Migration start: %s", NAME)
    report = reconcile_business_payment_outcomes(conn)
    mark_migration(conn, NAME)
    log.info(
        "Migration applied: %s scanned=%s paid=%s refunds=%s",
        NAME,
        report.payments_scanned,
        report.paid_evidence_created,
        report.refund_evidence_created,
    )


__all__ = [
    "NAME",
    "PaymentOutcomeBackfillReport",
    "apply",
    "reconcile_business_payment_outcomes",
]
