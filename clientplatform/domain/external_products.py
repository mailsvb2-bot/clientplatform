from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

from clientplatform.domain.attribution import AcquisitionSource
from clientplatform.domain.outcomes import OutcomeMoney
from clientplatform.domain.tenancy import normalize_uuid


_PRODUCT_KEY_RE = re.compile(r"[a-z][a-z0-9_-]{1,63}")
_EVENT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,159}")
_RESERVED_METADATA_KEYS = frozenset(
    {
        "external_product_connector_id",
        "external_product_key",
        "external_event_type",
        "acquisition_source",
        "acquisition_source_key",
        "payment_outcome_event_id",
    }
)


class ExternalProductError(RuntimeError):
    """Base error for trusted external-product integration work."""


class ExternalProductNotFound(ExternalProductError):
    """The connector is not available in the requested scope."""


class ExternalProductInvariantViolation(ExternalProductError):
    """An external product fact violates a durable integration invariant."""


class ExternalProductSignatureError(ExternalProductError):
    """Webhook authentication failed without exposing secret material."""


class ExternalProductConnectorStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    ATTENTION = "attention"
    DISABLED = "disabled"
    REVOKED = "revoked"


class ExternalProductEventType(StrEnum):
    EVIDENCE = "evidence"
    LEAD_CREATED = "lead_created"
    LEAD_QUALIFIED = "lead_qualified"
    ORDER_PAID = "order_paid"
    REFUND_RECORDED = "refund_recorded"


@dataclass(frozen=True, slots=True)
class ExternalProductConnector:
    id: str
    business_id: str
    product_key: str
    display_name: str
    webhook_secret_reference: str
    status: ExternalProductConnectorStatus
    created_by_member_id: str
    created_at: str
    updated_at: str
    activated_at: str | None = None
    disabled_at: str | None = None
    revoked_at: str | None = None
    last_event_at: str | None = None
    last_error_at: str | None = None
    last_error_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", normalize_uuid(self.id, field_name="connector_id"))
        object.__setattr__(
            self,
            "business_id",
            normalize_uuid(self.business_id, field_name="business_id"),
        )
        object.__setattr__(
            self,
            "created_by_member_id",
            normalize_uuid(self.created_by_member_id, field_name="created_by_member_id"),
        )
        object.__setattr__(self, "product_key", normalize_external_product_key(self.product_key))
        object.__setattr__(self, "display_name", normalize_external_product_name(self.display_name))


@dataclass(frozen=True, slots=True)
class ExternalProductAcquisition:
    source: AcquisitionSource
    source_key: str

    def __post_init__(self) -> None:
        source = self.source
        if not isinstance(source, AcquisitionSource):
            source = AcquisitionSource(str(source).strip().lower())
        source_key = " ".join(str(self.source_key or "").replace("\x00", " ").split())
        if not source_key or len(source_key) > 300:
            raise ValueError("external acquisition source_key must be 1..300 characters")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "source_key", source_key)


@dataclass(frozen=True, slots=True)
class ExternalProductEvent:
    external_event_id: str
    event_type: ExternalProductEventType
    occurred_at: datetime
    customer_ref: str | None
    subject_ref: str | None = None
    related_event_id: str | None = None
    money: OutcomeMoney | None = None
    acquisition: ExternalProductAcquisition | None = None
    metadata: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        event_id = normalize_external_event_id(self.external_event_id)
        event_type = self.event_type
        if not isinstance(event_type, ExternalProductEventType):
            event_type = ExternalProductEventType(str(event_type).strip().lower())
        if self.occurred_at.tzinfo is None:
            raise ValueError("external product occurred_at must be timezone-aware")
        customer_ref = (
            None
            if self.customer_ref is None or not str(self.customer_ref).strip()
            else normalize_external_customer_ref(str(self.customer_ref))
        )
        subject_ref = normalize_external_subject_ref(self.subject_ref)
        related_event_id = (
            None
            if self.related_event_id is None
            else normalize_external_event_id(self.related_event_id)
        )
        metadata = normalize_external_metadata(self.metadata or {})
        if event_type != ExternalProductEventType.EVIDENCE and customer_ref is None:
            raise ValueError(f"{event_type.value} requires customer_ref")
        if event_type in {
            ExternalProductEventType.ORDER_PAID,
            ExternalProductEventType.REFUND_RECORDED,
        } and self.money is None:
            raise ValueError(f"{event_type.value} requires money")
        if event_type not in {
            ExternalProductEventType.ORDER_PAID,
            ExternalProductEventType.REFUND_RECORDED,
        } and self.money is not None:
            raise ValueError(f"{event_type.value} must not include money")
        if event_type == ExternalProductEventType.REFUND_RECORDED:
            if related_event_id is None:
                raise ValueError("refund_recorded requires related_event_id")
        elif related_event_id is not None:
            raise ValueError(f"{event_type.value} must not include related_event_id")
        object.__setattr__(self, "external_event_id", event_id)
        object.__setattr__(self, "event_type", event_type)
        object.__setattr__(self, "customer_ref", customer_ref)
        object.__setattr__(self, "subject_ref", subject_ref)
        object.__setattr__(self, "related_event_id", related_event_id)
        object.__setattr__(self, "metadata", metadata)


@dataclass(frozen=True, slots=True)
class ExternalProductReceipt:
    id: str
    business_id: str
    connector_id: str
    external_event_id: str
    event_type: ExternalProductEventType
    customer_id: str | None
    customer_fingerprint: str | None
    payload_fingerprint: str
    outcome_event_id: str | None
    occurred_at: str
    received_at: str


def normalize_external_product_key(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _PRODUCT_KEY_RE.fullmatch(normalized):
        raise ValueError("product_key must be a lowercase stable identifier")
    return normalized


def normalize_external_product_name(value: str) -> str:
    normalized = " ".join(str(value or "").replace("\x00", " ").split())
    if not normalized or len(normalized) > 160:
        raise ValueError("external product display_name must be 1..160 characters")
    return normalized


def normalize_external_event_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not _EVENT_ID_RE.fullmatch(normalized):
        raise ValueError("external event id has an unsupported format")
    return normalized


def normalize_external_customer_ref(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 512:
        raise ValueError("external customer_ref must be 1..512 characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ValueError("external customer_ref contains control characters")
    return normalized


def normalize_external_subject_ref(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).replace("\x00", " ").split())
    if not normalized:
        return None
    if len(normalized) > 300:
        raise ValueError("external subject_ref must be at most 300 characters")
    return normalized


def external_customer_fingerprint(*, connector_id: str, customer_ref: str) -> str:
    connector = normalize_uuid(connector_id, field_name="connector_id")
    customer = normalize_external_customer_ref(customer_ref)
    return hashlib.sha256(f"{connector}\x00{customer}".encode("utf-8")).hexdigest()


def _safe_json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        raise ValueError("external product metadata nesting is too deep")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("external product metadata contains a non-finite number")
        return value
    if isinstance(value, str):
        normalized = value.replace("\x00", "")
        if len(normalized) > 1000:
            raise ValueError("external product metadata string is too long")
        return normalized
    if isinstance(value, (list, tuple)):
        if len(value) > 50:
            raise ValueError("external product metadata list is too large")
        return [_safe_json_value(item, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        if len(value) > 50:
            raise ValueError("external product metadata object is too large")
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key or "").strip()
            if not key or len(key) > 64:
                raise ValueError("external product metadata key is invalid")
            result[key] = _safe_json_value(raw_value, depth=depth + 1)
        return result
    raise ValueError("external product metadata contains an unsupported value")


def normalize_external_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _safe_json_value(metadata)
    if not isinstance(normalized, dict):
        raise ValueError("external product metadata must be an object")
    if _RESERVED_METADATA_KEYS.intersection(normalized):
        raise ValueError("external product metadata contains a reserved key")
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(encoded.encode("utf-8")) > 8192:
        raise ValueError("external product metadata is too large")
    return normalized


__all__ = [
    "ExternalProductAcquisition",
    "ExternalProductConnector",
    "ExternalProductConnectorStatus",
    "ExternalProductError",
    "ExternalProductEvent",
    "ExternalProductEventType",
    "ExternalProductInvariantViolation",
    "ExternalProductNotFound",
    "ExternalProductReceipt",
    "ExternalProductSignatureError",
    "external_customer_fingerprint",
    "normalize_external_customer_ref",
    "normalize_external_event_id",
    "normalize_external_metadata",
    "normalize_external_product_key",
    "normalize_external_product_name",
]
