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
_OBSERVATION_KIND_RE = re.compile(r"[a-z][a-z0-9._-]{1,63}")
_OBSERVATION_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,159}")
_EXTERNAL_OBSERVATION_SCHEMA = "2026-09-20.v2"
_RESERVED_METADATA_KEYS = frozenset(
    {
        "external_product_connector_id",
        "external_product_key",
        "external_event_type",
        "acquisition_source",
        "acquisition_source_key",
        "payment_outcome_event_id",
        "external_observation_schema",
        "external_observation_kind",
        "external_observation_label",
        "external_observation_observed_at",
        "external_observation_provenance_ref",
        "external_observation_quality",
        "external_observation_limitations",
        "external_observation_key",
        "external_observation_revision",
        "external_observation_state",
        "external_observation_supersedes_external_event_id",
        "external_observation_fresh_until",
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


class ExternalProductIngressMode(StrEnum):
    SIGNED_WEBHOOK = "signed_webhook"
    TRUSTED_PULL = "trusted_pull"


class ExternalProductEventType(StrEnum):
    EVIDENCE = "evidence"
    LEAD_CREATED = "lead_created"
    LEAD_QUALIFIED = "lead_qualified"
    ORDER_PAID = "order_paid"
    REFUND_RECORDED = "refund_recorded"


class ExternalObservationState(StrEnum):
    ACTIVE = "active"
    RETRACTED = "retracted"


class ExternalObservationQuality(StrEnum):
    """Meaning of an observation without pretending it is a probability."""

    SOURCE_ASSERTED = "source_asserted"
    SOURCE_VERIFIED = "source_verified"
    DERIVED = "derived"


class ExternalObservationFeedback(StrEnum):
    """Owner feedback for value proof; never an identity mutation command."""

    USEFUL = "useful"
    INCORRECT = "incorrect"
    WRONG_CUSTOMER = "wrong_customer"


@dataclass(frozen=True, slots=True)
class ExternalProductObservation:
    """A bounded, provenance-bearing observation supplied by a trusted connector.

    This is evidence for ClientPlatform to display or evaluate. It is never an
    instruction to mutate CustomerIdentity or perform customer communication.
    """

    observation_key: str
    kind: str
    label: str
    observed_at: datetime
    provenance_ref: str
    revision: int = 1
    state: ExternalObservationState = ExternalObservationState.ACTIVE
    supersedes_external_event_id: str | None = None
    fresh_until: datetime | None = None
    quality: ExternalObservationQuality = ExternalObservationQuality.SOURCE_ASSERTED
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        observation_key = normalize_external_observation_key(self.observation_key)
        kind = str(self.kind or "").strip().lower()
        if not _OBSERVATION_KIND_RE.fullmatch(kind):
            raise ValueError("external observation kind must be a stable lowercase identifier")
        label = _normalize_bounded_text(self.label, field_name="observation label", limit=160)
        provenance_ref = _normalize_bounded_text(
            self.provenance_ref,
            field_name="observation provenance_ref",
            limit=300,
        )
        observed_at = self.observed_at
        if observed_at.tzinfo is None:
            raise ValueError("external observation observed_at must be timezone-aware")
        revision = self.revision
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("external observation revision must be a positive integer")
        state = self.state
        if not isinstance(state, ExternalObservationState):
            state = ExternalObservationState(str(state).strip().lower())
        supersedes = (
            None
            if self.supersedes_external_event_id is None
            else normalize_external_event_id(self.supersedes_external_event_id)
        )
        if revision == 1 and supersedes is not None:
            raise ValueError("first external observation revision cannot supersede an event")
        if revision > 1 and supersedes is None:
            raise ValueError("external observation revision requires supersedes_external_event_id")
        if revision == 1 and state == ExternalObservationState.RETRACTED:
            raise ValueError("first external observation revision cannot be retracted")
        fresh_until = self.fresh_until
        if fresh_until is not None:
            if fresh_until.tzinfo is None:
                raise ValueError("external observation fresh_until must be timezone-aware")
            if fresh_until < observed_at:
                raise ValueError("external observation fresh_until cannot predate observed_at")
        if state == ExternalObservationState.RETRACTED and fresh_until is not None:
            raise ValueError("retracted external observation cannot have fresh_until")
        quality = self.quality
        if not isinstance(quality, ExternalObservationQuality):
            quality = ExternalObservationQuality(str(quality).strip().lower())
        raw_limitations = tuple(self.limitations or ())
        if len(raw_limitations) > 8:
            raise ValueError("external observation supports at most 8 limitations")
        limitations = tuple(
            _normalize_bounded_text(
                item,
                field_name="observation limitation",
                limit=200,
            )
            for item in raw_limitations
        )
        object.__setattr__(self, "observation_key", observation_key)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "provenance_ref", provenance_ref)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "supersedes_external_event_id", supersedes)
        object.__setattr__(self, "fresh_until", fresh_until)
        object.__setattr__(self, "quality", quality)
        object.__setattr__(self, "limitations", limitations)

    def canonical_metadata(self) -> dict[str, Any]:
        return {
            "external_observation_schema": _EXTERNAL_OBSERVATION_SCHEMA,
            "external_observation_key": self.observation_key,
            "external_observation_revision": self.revision,
            "external_observation_state": self.state.value,
            "external_observation_supersedes_external_event_id": self.supersedes_external_event_id,
            "external_observation_fresh_until": (
                None if self.fresh_until is None else self.fresh_until.isoformat()
            ),
            "external_observation_kind": self.kind,
            "external_observation_label": self.label,
            "external_observation_observed_at": self.observed_at.isoformat(),
            "external_observation_provenance_ref": self.provenance_ref,
            "external_observation_quality": self.quality.value,
            "external_observation_limitations": list(self.limitations),
        }


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
    ingress_mode: ExternalProductIngressMode = ExternalProductIngressMode.SIGNED_WEBHOOK

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
        ingress_mode = self.ingress_mode
        if not isinstance(ingress_mode, ExternalProductIngressMode):
            ingress_mode = ExternalProductIngressMode(str(ingress_mode).strip().lower())
        object.__setattr__(self, "ingress_mode", ingress_mode)


@dataclass(frozen=True, slots=True)
class ExternalProductAcquisition:
    source: AcquisitionSource
    source_key: str

    def __post_init__(self) -> None:
        source = self.source
        if not isinstance(source, AcquisitionSource):
            source = AcquisitionSource(str(source).strip().lower())
        source_key = " ".join(str(self.source_key or "").replace("\x00", " ").split())
        if not source_key or len(source_key) > 200:
            raise ValueError("external acquisition source_key must be 1..200 characters")
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
    observation: ExternalProductObservation | None = None
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
        observation = self.observation
        if observation is not None and not isinstance(observation, ExternalProductObservation):
            raise TypeError("external product observation must be ExternalProductObservation")
        if observation is not None and event_type != ExternalProductEventType.EVIDENCE:
            raise ValueError("external observation is only allowed for evidence events")
        if observation is not None and customer_ref is None:
            raise ValueError("structured external observation requires customer_ref")
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
        object.__setattr__(self, "observation", observation)
        object.__setattr__(self, "metadata", metadata)


@dataclass(frozen=True, slots=True)
class ExternalObservationFeedbackRecord:
    id: str
    business_id: str
    receipt_id: str
    customer_id: str
    feedback: ExternalObservationFeedback
    actor_member_id: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ExternalObservationValueSnapshot:
    business_id: str
    connector_id: str | None
    current_observations: int
    active_observations: int
    retracted_observations: int
    stale_active_observations: int
    unknown_freshness_observations: int
    feedback_total: int
    useful_feedback: int
    incorrect_feedback: int
    wrong_customer_feedback: int


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
    observation_key: str | None = None
    observation_revision: int | None = None
    observation_state: ExternalObservationState | None = None
    observation_supersedes_external_event_id: str | None = None
    observation_fresh_until: str | None = None
    observation_provenance_ref: str | None = None


def _normalize_bounded_text(value: object, *, field_name: str, limit: int) -> str:
    normalized = " ".join(str(value or "").replace("\x00", " ").split())
    if not normalized or len(normalized) > limit:
        raise ValueError(f"{field_name} must be 1..{limit} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ValueError(f"{field_name} contains control characters")
    return normalized


def normalize_external_observation_key(value: object) -> str:
    normalized = str(value or "").strip()
    if not _OBSERVATION_KEY_RE.fullmatch(normalized):
        raise ValueError("external observation key has an unsupported format")
    return normalized


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


def external_customer_identity_subject(*, connector_id: str, customer_ref: str) -> str:
    fingerprint = external_customer_fingerprint(
        connector_id=connector_id,
        customer_ref=customer_ref,
    )
    connector = normalize_uuid(connector_id, field_name="connector_id")
    return f"extp:{connector}:{fingerprint}"


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
    "ExternalObservationFeedback",
    "ExternalObservationFeedbackRecord",
    "ExternalObservationQuality",
    "ExternalObservationState",
    "ExternalObservationValueSnapshot",
    "ExternalProductAcquisition",
    "ExternalProductConnector",
    "ExternalProductConnectorStatus",
    "ExternalProductIngressMode",
    "ExternalProductError",
    "ExternalProductEvent",
    "ExternalProductEventType",
    "ExternalProductInvariantViolation",
    "ExternalProductNotFound",
    "ExternalProductObservation",
    "ExternalProductReceipt",
    "ExternalProductSignatureError",
    "external_customer_fingerprint",
    "external_customer_identity_subject",
    "normalize_external_customer_ref",
    "normalize_external_event_id",
    "normalize_external_observation_key",
    "normalize_external_metadata",
    "normalize_external_product_key",
    "normalize_external_product_name",
]
