from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from clientplatform.domain.outcomes import (
    BusinessOutcomeEvent,
    OutcomeIdempotencyConflict,
    OutcomeMoney,
    OutcomeSource,
    OutcomeType,
)


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _serialize_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("outcome timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_datetime(value: Any) -> datetime:
    raw = str(value or "").strip()
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _metadata_json(metadata: Any) -> str:
    return json.dumps(
        dict(metadata),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


_EVENT_SELECT = """
    SELECT id, business_id, outcome_type, occurred_at,
           source_type, source_id, customer_id, subject_ref,
           amount_minor, currency, idempotency_key, metadata_json,
           metadata_version, created_at
    FROM business_outcome_events
"""


def _event_from_row(row: Any) -> BusinessOutcomeEvent:
    amount_minor = _value(row, "amount_minor", 8)
    currency = _value(row, "currency", 9)
    money = None
    if amount_minor is not None:
        money = OutcomeMoney(amount_minor=int(amount_minor), currency=str(currency))
    metadata = json.loads(str(_value(row, "metadata_json", 11)))
    if not isinstance(metadata, dict):
        raise ValueError("outcome metadata must decode to a JSON object")
    customer_id = _value(row, "customer_id", 6)
    subject_ref = _value(row, "subject_ref", 7)
    return BusinessOutcomeEvent(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        outcome_type=OutcomeType(str(_value(row, "outcome_type", 2))),
        occurred_at=_parse_datetime(_value(row, "occurred_at", 3)),
        source=OutcomeSource(
            source_type=str(_value(row, "source_type", 4)),
            source_id=str(_value(row, "source_id", 5)),
        ),
        customer_id=None if customer_id is None else str(customer_id),
        subject_ref=None if subject_ref is None else str(subject_ref),
        money=money,
        idempotency_key=str(_value(row, "idempotency_key", 10)),
        metadata=metadata,
        metadata_version=int(_value(row, "metadata_version", 12)),
        created_at=_parse_datetime(_value(row, "created_at", 13)),
    )


def _semantic_identity(event: BusinessOutcomeEvent) -> tuple[Any, ...]:
    return (
        event.business_id,
        event.outcome_type.value,
        _serialize_datetime(event.occurred_at),
        event.source_type,
        event.source_id,
        event.customer_id,
        event.subject_ref,
        event.amount_minor,
        event.currency,
        event.idempotency_key,
        _metadata_json(event.metadata),
        event.metadata_version,
    )


class OutcomeRepository:
    """Append-only access to the canonical, business-scoped outcome ledger."""

    def __init__(self, conn: Any):
        self._conn = conn

    def append(self, event: BusinessOutcomeEvent) -> BusinessOutcomeEvent:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO business_outcome_events(
                id, business_id, outcome_type, occurred_at,
                source_type, source_id, customer_id, subject_ref,
                amount_minor, currency, idempotency_key, metadata_json,
                metadata_version, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event.business_id,
                event.outcome_type.value,
                _serialize_datetime(event.occurred_at),
                event.source_type,
                event.source_id,
                event.customer_id,
                event.subject_ref,
                event.amount_minor,
                event.currency,
                event.idempotency_key,
                _metadata_json(event.metadata),
                event.metadata_version,
                _serialize_datetime(event.created_at),
            ),
        )
        accepted = self.get_by_idempotency_key(
            business_id=event.business_id,
            idempotency_key=event.idempotency_key,
        )
        if accepted is None:
            raise RuntimeError("outcome append did not produce a durable row")
        if _semantic_identity(accepted) != _semantic_identity(event):
            raise OutcomeIdempotencyConflict(
                "idempotency key already belongs to a different business outcome"
            )
        return accepted

    def get_by_idempotency_key(
        self,
        *,
        business_id: str,
        idempotency_key: str,
    ) -> BusinessOutcomeEvent | None:
        row = self._conn.execute(
            _EVENT_SELECT
            + " WHERE business_id=? AND idempotency_key=? LIMIT 1",
            (str(business_id), str(idempotency_key)),
        ).fetchone()
        return None if row is None else _event_from_row(row)

    def list_events(
        self,
        *,
        business_id: str,
        outcome_type: OutcomeType | str | None = None,
        source_type: str | None = None,
        source_id: str | None = None,
        customer_id: str | None = None,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
        limit: int = 100,
    ) -> list[BusinessOutcomeEvent]:
        normalized_limit = int(limit)
        if normalized_limit < 1 or normalized_limit > 500:
            raise ValueError("limit must be between 1 and 500")
        where = ["business_id=?"]
        params: list[Any] = [str(business_id)]
        if outcome_type is not None:
            normalized_type = (
                outcome_type if isinstance(outcome_type, OutcomeType) else OutcomeType(str(outcome_type))
            )
            where.append("outcome_type=?")
            params.append(normalized_type.value)
        if source_type is not None:
            where.append("source_type=?")
            params.append(str(source_type))
        if source_id is not None:
            where.append("source_id=?")
            params.append(str(source_id))
        if customer_id is not None:
            where.append("customer_id=?")
            params.append(str(customer_id))
        if occurred_from is not None:
            where.append("occurred_at>=?")
            params.append(_serialize_datetime(occurred_from))
        if occurred_to is not None:
            where.append("occurred_at<?")
            params.append(_serialize_datetime(occurred_to))
        params.append(normalized_limit)
        rows = self._conn.execute(
            _EVENT_SELECT
            + " WHERE "
            + " AND ".join(where)
            + " ORDER BY occurred_at DESC, created_at DESC, id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [_event_from_row(row) for row in rows]
