from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping

from clientplatform.domain.tenancy import normalize_uuid


class OutcomeInvariantViolation(ValueError):
    """A durable business outcome violates the canonical event contract."""


class OutcomeEventType(StrEnum):
    LEAD_CREATED = "lead_created"
    BOOKING_CREATED = "booking_created"
    BOOKING_COMPLETED = "booking_completed"
    PAYMENT_SUCCEEDED = "payment_succeeded"
    PAYMENT_REFUNDED = "payment_refunded"
    INVOICE_SIGNED = "invoice_signed"
    REVIEW_RECEIVED = "review_received"


class OutcomeSource(StrEnum):
    CLIENTPLATFORM = "clientplatform"
    TELEGRAM_ADAPTER = "telegram_adapter"
    WEBSITE_WIDGET = "website_widget"
    PAYMENT_WEBHOOK = "payment_webhook"
    LEGACY_ADAPTER = "legacy_adapter"


def normalize_outcome_datetime(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise OutcomeInvariantViolation("occurred_at must not be empty")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OutcomeInvariantViolation("occurred_at must be an ISO datetime") from exc
    if parsed.tzinfo is None:
        raise OutcomeInvariantViolation("occurred_at must include timezone information")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def canonical_metadata_json(metadata: Mapping[str, Any] | None = None) -> str:
    """Serialize non-PII outcome metadata deterministically.

    Producers remain responsible for selecting privacy-safe fields.  The ledger
    stores only this canonical JSON representation so retries compare exactly.
    """

    try:
        value = dict(metadata or {})
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise OutcomeInvariantViolation("outcome metadata must be JSON serializable") from exc


def normalize_metadata_json(value: str) -> str:
    raw = str(value or "{}").strip() or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OutcomeInvariantViolation("metadata_json must contain valid JSON") from exc
    if not isinstance(parsed, dict):
        raise OutcomeInvariantViolation("metadata_json must contain a JSON object")
    return canonical_metadata_json(parsed)


def _normalize_text(value: str, *, field_name: str, max_length: int = 255) -> str:
    normalized = " ".join(str(value or "").split())
    if not normalized:
        raise OutcomeInvariantViolation(f"{field_name} must not be empty")
    if len(normalized) > max_length:
        raise OutcomeInvariantViolation(f"{field_name} is too long")
    return normalized


@dataclass(frozen=True, slots=True)
class BusinessOutcomeEvent:
    business_id: str
    event_type: OutcomeEventType
    subject_type: str
    subject_id: str
    occurred_at: str
    idempotency_key: str
    source: OutcomeSource
    amount_minor: int | None = None
    currency: str | None = None
    metadata_json: str = "{}"
    correction_of_event_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "business_id", normalize_uuid(self.business_id, field_name="business_id"))
        object.__setattr__(self, "event_type", OutcomeEventType(self.event_type))
        object.__setattr__(self, "source", OutcomeSource(self.source))
        object.__setattr__(self, "subject_type", _normalize_text(self.subject_type, field_name="subject_type", max_length=80))
        object.__setattr__(self, "subject_id", _normalize_text(self.subject_id, field_name="subject_id"))
        object.__setattr__(
            self,
            "idempotency_key",
            _normalize_text(self.idempotency_key, field_name="idempotency_key", max_length=255),
        )
        object.__setattr__(self, "occurred_at", normalize_outcome_datetime(self.occurred_at))
        object.__setattr__(self, "metadata_json", normalize_metadata_json(self.metadata_json))

        amount = self.amount_minor
        currency = self.currency
        if (amount is None) != (currency is None):
            raise OutcomeInvariantViolation("amount_minor and currency must be provided together")
        if amount is not None:
            if isinstance(amount, bool) or not isinstance(amount, int):
                raise OutcomeInvariantViolation("amount_minor must be an integer")
            normalized_currency = str(currency or "").strip().upper()
            if re.fullmatch(r"[A-Z]{3}", normalized_currency) is None:
                raise OutcomeInvariantViolation("currency must be an uppercase ISO 4217 code")
            object.__setattr__(self, "currency", normalized_currency)

        if self.correction_of_event_id is not None:
            object.__setattr__(
                self,
                "correction_of_event_id",
                normalize_uuid(self.correction_of_event_id, field_name="correction_of_event_id"),
            )
