from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from typing import Any

from clientplatform.domain.attribution import AcquisitionSource
from clientplatform.domain.external_products import (
    ExternalObservationFeedback,
    ExternalObservationFeedbackRecord,
    ExternalObservationValueSnapshot,
    ExternalProductAcquisition,
    ExternalProductConnector,
    ExternalProductIngressMode,
    ExternalProductEvent,
    ExternalProductEventType,
    ExternalObservationQuality,
    ExternalObservationState,
    ExternalProductInvariantViolation,
    ExternalProductObservation,
    ExternalProductReceipt,
    ExternalProductSignatureError,
)
from clientplatform.domain.outcomes import OutcomeMoney
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.external_product_repository import (
    ExternalProductRepository,
)
from clientplatform.runtime.secrets import (
    CredentialProvider,
    EnvironmentCredentialProvider,
    SecretReferenceError,
)
from services.db import get_db, get_db_ro


_SIGNATURE_RE = re.compile(r"sha256=([0-9a-fA-F]{64})")
_MAX_BODY_BYTES = 64 * 1024
_MAX_CLOCK_SKEW_SECONDS = 300
_ALLOWED_EVENT_KEYS = frozenset(
    {
        "version",
        "event_id",
        "type",
        "occurred_at",
        "customer_ref",
        "subject_ref",
        "related_event_id",
        "amount_minor",
        "currency",
        "acquisition",
        "observation",
        "metadata",
    }
)


def provision_external_product_connector(
    *,
    actor: TenantContext,
    product_key: str,
    display_name: str,
    webhook_secret_reference: str,
    ingress_mode: ExternalProductIngressMode | str = ExternalProductIngressMode.SIGNED_WEBHOOK,
) -> ExternalProductConnector:
    """Create a pending connector containing only a secret-manager reference."""

    with get_db() as conn:
        return ExternalProductRepository(conn).create_connector(
            actor=actor,
            product_key=product_key,
            display_name=display_name,
            webhook_secret_reference=webhook_secret_reference,
            ingress_mode=ingress_mode,
        )


def verify_and_activate_external_product_connector(
    *,
    actor: TenantContext,
    connector_id: str,
    credential_provider: CredentialProvider | None = None,
) -> ExternalProductConnector:
    """Activate only after the configured HMAC secret can be resolved safely."""

    provider = credential_provider or EnvironmentCredentialProvider()
    with get_db() as conn:
        repository = ExternalProductRepository(conn)
        connector = repository.get_connector(actor=actor, connector_id=connector_id)
        try:
            secret = str(provider.resolve(connector.webhook_secret_reference) or "")
        except SecretReferenceError as exc:
            raise ExternalProductInvariantViolation(
                "external product webhook secret is unavailable"
            ) from exc
        if len(secret.encode("utf-8")) < 32:
            raise ExternalProductInvariantViolation(
                "external product webhook secret must contain at least 32 bytes"
            )
        return repository.activate_connector(actor=actor, connector_id=connector.id)


def bind_external_product_customer(
    *,
    actor: TenantContext,
    connector_id: str,
    customer_id: str,
    customer_ref: str,
) -> str:
    """Explicitly bind one connector subject to an existing canonical Customer.

    This is a server-side consumer binding. The public webhook still cannot submit
    customer_id, business_id or mutate an existing CRM identity by itself.
    """

    with get_db() as conn:
        return ExternalProductRepository(conn).bind_customer_ref(
            actor=actor,
            connector_id=connector_id,
            customer_id=customer_id,
            customer_ref=customer_ref,
        )


def record_external_observation_feedback(
    *,
    actor: TenantContext,
    customer_id: str,
    receipt_id: str,
    feedback: ExternalObservationFeedback | str,
) -> ExternalObservationFeedbackRecord:
    with get_db() as conn:
        return ExternalProductRepository(conn).record_observation_feedback(
            actor=actor,
            customer_id=customer_id,
            receipt_id=receipt_id,
            feedback=feedback,
        )


def get_external_observation_value_snapshot(
    *,
    actor: TenantContext,
    connector_id: str | None = None,
    now: datetime | None = None,
) -> ExternalObservationValueSnapshot:
    with get_db_ro() as conn:
        return ExternalProductRepository(conn).observation_value_snapshot(
            actor=actor,
            connector_id=connector_id,
            now=now,
        )


def disable_external_product_connector(
    *,
    actor: TenantContext,
    connector_id: str,
) -> ExternalProductConnector:
    with get_db() as conn:
        return ExternalProductRepository(conn).disable_connector(
            actor=actor,
            connector_id=connector_id,
        )


def verify_external_product_signature(
    *,
    secret: str,
    timestamp_header: str,
    signature_header: str,
    body: bytes,
    now: datetime | None = None,
) -> None:
    """Verify bounded HMAC-SHA256 authentication with replay-window protection."""

    key = str(secret or "").encode("utf-8")
    if len(key) < 32:
        raise ExternalProductSignatureError("external_product_secret_invalid")
    raw_timestamp = str(timestamp_header or "").strip()
    try:
        timestamp = int(raw_timestamp)
    except (TypeError, ValueError):
        raise ExternalProductSignatureError("external_product_timestamp_invalid") from None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("signature verification clock must be timezone-aware")
    current_epoch = int(current.timestamp())
    if abs(current_epoch - timestamp) > _MAX_CLOCK_SKEW_SECONDS:
        raise ExternalProductSignatureError("external_product_timestamp_expired")
    match = _SIGNATURE_RE.fullmatch(str(signature_header or "").strip())
    if match is None:
        raise ExternalProductSignatureError("external_product_signature_invalid")
    if len(body) > _MAX_BODY_BYTES:
        raise ExternalProductSignatureError("external_product_body_too_large")
    canonical = raw_timestamp.encode("ascii") + b"." + body
    expected = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(match.group(1).lower(), expected):
        raise ExternalProductSignatureError("external_product_signature_invalid")


def parse_external_product_event(body: bytes) -> ExternalProductEvent:
    if not body or len(body) > _MAX_BODY_BYTES:
        raise ValueError("external product body must be 1..65536 bytes")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("external product body must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("external product body must be a JSON object")
    unknown = set(payload) - _ALLOWED_EVENT_KEYS
    if unknown:
        raise ValueError("external product body contains unsupported fields")
    if payload.get("version") != 1:
        raise ValueError("external product event version must be 1")
    occurred_at = _parse_occurred_at(payload.get("occurred_at"))
    event_type = ExternalProductEventType(str(payload.get("type") or "").strip())
    money = _parse_money(payload, event_type=event_type)
    acquisition = _parse_acquisition(payload.get("acquisition"))
    observation = _parse_observation(payload.get("observation"))
    metadata = payload.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("external product metadata must be an object")
    return ExternalProductEvent(
        external_event_id=str(payload.get("event_id") or ""),
        event_type=event_type,
        occurred_at=occurred_at,
        customer_ref=(
            None
            if payload.get("customer_ref") is None
            else str(payload.get("customer_ref") or "")
        ),
        subject_ref=(
            None if payload.get("subject_ref") is None else str(payload["subject_ref"])
        ),
        related_event_id=(
            None
            if payload.get("related_event_id") is None
            else str(payload["related_event_id"])
        ),
        money=money,
        acquisition=acquisition,
        observation=observation,
        metadata=metadata,
    )


def authenticate_external_product_webhook(
    *,
    connector_id: str,
    timestamp_header: str,
    signature_header: str,
    body: bytes,
    credential_provider: CredentialProvider | None = None,
    now: datetime | None = None,
) -> tuple[ExternalProductConnector, str, datetime]:
    """Authenticate raw bytes and return the tenant-bound connector plus fingerprint.

    Product-specific adapters reuse this boundary so they cannot bypass the same
    secret lookup, clock-skew protection or payload fingerprint semantics.
    """

    provider = credential_provider or EnvironmentCredentialProvider()
    received = now or datetime.now(timezone.utc)
    if received.tzinfo is None:
        raise ValueError("received clock must be timezone-aware")
    with get_db() as conn:
        repository = ExternalProductRepository(conn)
        connector = repository.get_active_for_ingress(connector_id=connector_id)
        try:
            secret = str(provider.resolve(connector.webhook_secret_reference) or "")
        except SecretReferenceError as exc:
            raise ExternalProductSignatureError(
                "external_product_secret_unavailable"
            ) from exc
        verify_external_product_signature(
            secret=secret,
            timestamp_header=timestamp_header,
            signature_header=signature_header,
            body=body,
            now=received,
        )
        return connector, hashlib.sha256(body).hexdigest(), received


def ingest_authenticated_external_product_event(
    *,
    connector: ExternalProductConnector,
    event: ExternalProductEvent,
    payload_fingerprint: str,
    received_at: datetime,
) -> ExternalProductReceipt:
    """Persist an already-authenticated, product-normalized event canonically."""

    with get_db() as conn:
        return ExternalProductRepository(conn).ingest_event(
            connector=connector,
            event=event,
            payload_fingerprint=payload_fingerprint,
            received_at=received_at,
        )


def ingest_external_product_webhook(
    *,
    connector_id: str,
    timestamp_header: str,
    signature_header: str,
    body: bytes,
    credential_provider: CredentialProvider | None = None,
    now: datetime | None = None,
) -> ExternalProductReceipt:
    """Authenticate and atomically ingest one generic external product fact."""

    connector, payload_fingerprint, received = authenticate_external_product_webhook(
        connector_id=connector_id,
        timestamp_header=timestamp_header,
        signature_header=signature_header,
        body=body,
        credential_provider=credential_provider,
        now=now,
    )
    event = parse_external_product_event(body)
    return ingest_authenticated_external_product_event(
        connector=connector,
        event=event,
        payload_fingerprint=payload_fingerprint,
        received_at=received,
    )


def _parse_occurred_at(value: Any) -> datetime:
    raw = str(value or "").strip()
    if not raw or len(raw) > 80:
        raise ValueError("external product occurred_at is invalid")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("external product occurred_at is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("external product occurred_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def _parse_observation(value: Any) -> ExternalProductObservation | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("external product observation must be an object")
    allowed = {
        "observation_key",
        "kind",
        "label",
        "observed_at",
        "provenance_ref",
        "revision",
        "state",
        "supersedes_external_event_id",
        "fresh_until",
        "quality",
        "limitations",
    }
    if set(value) - allowed:
        raise ValueError("external product observation contains unsupported fields")
    limitations = value.get("limitations") or []
    if not isinstance(limitations, list) or any(
        not isinstance(item, str) for item in limitations
    ):
        raise ValueError("external product observation limitations must be strings")
    fresh_until = (
        None
        if value.get("fresh_until") is None
        else _parse_occurred_at(value.get("fresh_until"))
    )
    revision = value.get("revision", 1)
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise ValueError("external product observation revision must be an integer")
    return ExternalProductObservation(
        observation_key=str(value.get("observation_key") or ""),
        kind=str(value.get("kind") or ""),
        label=str(value.get("label") or ""),
        observed_at=_parse_occurred_at(value.get("observed_at")),
        provenance_ref=str(value.get("provenance_ref") or ""),
        revision=revision,
        state=ExternalObservationState(
            str(value.get("state") or ExternalObservationState.ACTIVE.value)
            .strip()
            .lower()
        ),
        supersedes_external_event_id=(
            None
            if value.get("supersedes_external_event_id") is None
            else str(value.get("supersedes_external_event_id") or "")
        ),
        fresh_until=fresh_until,
        quality=ExternalObservationQuality(
            str(value.get("quality") or ExternalObservationQuality.SOURCE_ASSERTED.value)
            .strip()
            .lower()
        ),
        limitations=tuple(limitations),
    )


def _parse_money(
    payload: dict[str, Any],
    *,
    event_type: ExternalProductEventType,
) -> OutcomeMoney | None:
    has_amount = "amount_minor" in payload
    has_currency = "currency" in payload
    if has_amount != has_currency:
        raise ValueError("external product money requires amount_minor and currency")
    if not has_amount:
        return None
    amount = payload.get("amount_minor")
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise ValueError("external product amount_minor must be an integer")
    if event_type == ExternalProductEventType.ORDER_PAID and amount < 0:
        raise ValueError("external product order_paid amount must not be negative")
    if event_type == ExternalProductEventType.REFUND_RECORDED and amount < 0:
        raise ValueError("external product refund amount must not be negative")
    return OutcomeMoney(amount_minor=amount, currency=str(payload.get("currency") or ""))


def _parse_acquisition(value: Any) -> ExternalProductAcquisition | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"source", "source_key"}:
        raise ValueError("external product acquisition is invalid")
    return ExternalProductAcquisition(
        source=AcquisitionSource(str(value.get("source") or "").strip().lower()),
        source_key=str(value.get("source_key") or ""),
    )


__all__ = [
    "authenticate_external_product_webhook",
    "get_external_observation_value_snapshot",
    "bind_external_product_customer",
    "disable_external_product_connector",
    "ingest_authenticated_external_product_event",
    "ingest_external_product_webhook",
    "parse_external_product_event",
    "provision_external_product_connector",
    "record_external_observation_feedback",
    "verify_and_activate_external_product_connector",
    "verify_external_product_signature",
]
