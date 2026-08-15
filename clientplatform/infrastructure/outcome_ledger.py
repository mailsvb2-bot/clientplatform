from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.outcomes import BusinessOutcomeEvent, OutcomeEventType, OutcomeSource
from clientplatform.domain.tenancy import normalize_uuid


class OutcomeIdempotencyConflict(RuntimeError):
    """One tenant attempted to reuse an idempotency key for another outcome."""


@dataclass(frozen=True, slots=True)
class RecordedBusinessOutcome:
    id: str
    event: BusinessOutcomeEvent
    created_at: str


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _record_from_row(row: Any) -> RecordedBusinessOutcome:
    amount_minor = _value(row, "amount_minor", 6)
    currency = _value(row, "currency", 7)
    correction = _value(row, "correction_of_event_id", 11)
    event = BusinessOutcomeEvent(
        business_id=str(_value(row, "business_id", 1)),
        event_type=OutcomeEventType(str(_value(row, "event_type", 2))),
        subject_type=str(_value(row, "subject_type", 3)),
        subject_id=str(_value(row, "subject_id", 4)),
        occurred_at=str(_value(row, "occurred_at", 5)),
        amount_minor=None if amount_minor is None else int(amount_minor),
        currency=None if currency is None else str(currency),
        metadata_json=str(_value(row, "metadata_json", 8)),
        idempotency_key=str(_value(row, "idempotency_key", 9)),
        source=OutcomeSource(str(_value(row, "source", 10))),
        correction_of_event_id=None if correction is None else str(correction),
    )
    return RecordedBusinessOutcome(
        id=str(_value(row, "id", 0)),
        event=event,
        created_at=str(_value(row, "created_at", 12)),
    )


_SELECT = """
    SELECT id, business_id, event_type, subject_type, subject_id, occurred_at,
           amount_minor, currency, metadata_json, idempotency_key, source,
           correction_of_event_id, created_at
    FROM business_outcome_events
"""


class OutcomeLedger:
    """Append-only durable outcomes using the caller's transaction.

    This repository intentionally never commits and never opens a second
    connection.  Business actions and their outcome records therefore share one
    transaction boundary owned by the application service.
    """

    def __init__(self, conn: Any):
        self._conn = conn

    def append(self, event: BusinessOutcomeEvent) -> RecordedBusinessOutcome:
        record_id = str(uuid4())
        created_at = _utc_now()
        self._conn.execute(
            """
            INSERT INTO business_outcome_events(
                id, business_id, event_type, subject_type, subject_id,
                occurred_at, amount_minor, currency, metadata_json,
                idempotency_key, source, correction_of_event_id, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(business_id, idempotency_key) DO NOTHING
            """,
            (
                record_id,
                event.business_id,
                event.event_type.value,
                event.subject_type,
                event.subject_id,
                event.occurred_at,
                event.amount_minor,
                event.currency,
                event.metadata_json,
                event.idempotency_key,
                event.source.value,
                event.correction_of_event_id,
                created_at,
            ),
        )
        existing = self.get_by_idempotency_key(
            business_id=event.business_id,
            idempotency_key=event.idempotency_key,
        )
        if existing is None:
            raise RuntimeError("outcome append did not persist or resolve the durable event")
        if existing.event != event:
            raise OutcomeIdempotencyConflict(
                "idempotency key is already bound to a different business outcome"
            )
        return existing

    def get_by_idempotency_key(
        self,
        *,
        business_id: str,
        idempotency_key: str,
    ) -> RecordedBusinessOutcome | None:
        normalized_business = normalize_uuid(business_id, field_name="business_id")
        normalized_key = " ".join(str(idempotency_key or "").split())
        if not normalized_key:
            raise ValueError("idempotency_key must not be empty")
        row = self._conn.execute(
            _SELECT + " WHERE business_id=? AND idempotency_key=? LIMIT 1",
            (normalized_business, normalized_key),
        ).fetchone()
        return None if row is None else _record_from_row(row)

    def list_for_subject(
        self,
        *,
        business_id: str,
        subject_type: str,
        subject_id: str,
    ) -> list[RecordedBusinessOutcome]:
        normalized_business = normalize_uuid(business_id, field_name="business_id")
        rows = self._conn.execute(
            _SELECT
            + " WHERE business_id=? AND subject_type=? AND subject_id=?"
              " ORDER BY occurred_at, created_at, id",
            (normalized_business, str(subject_type), str(subject_id)),
        ).fetchall()
        return [_record_from_row(row) for row in rows]
