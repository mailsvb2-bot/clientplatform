from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from clientplatform.application.ucr_attendance_observations import (
    UcrAttendanceGateway,
    UcrParticipantAttendance,
    read_ucr_attendance_observation,
)
from clientplatform.domain.external_products import (
    ExternalProductEvent,
    ExternalProductEventType,
    ExternalProductInvariantViolation,
    ExternalProductReceipt,
    external_customer_fingerprint,
    normalize_external_observation_key,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.external_product_repository import (
    ExternalProductRepository,
)
from clientplatform.runtime.ucr_gateway import UcrGatewayClient
from services.db import get_db, get_db_ro


@dataclass(frozen=True, slots=True)
class UcrAttendancePilotSyncResult:
    attendance: UcrParticipantAttendance
    receipt: ExternalProductReceipt | None
    persisted: bool


def _received_at(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("UCR attendance pilot received_at must be timezone-aware")
    return current.astimezone(timezone.utc)


def _event_id(*, observation_key: str, provenance_ref: str, revision: int) -> str:
    digest = hashlib.sha256(
        f"{observation_key}\x00{provenance_ref}".encode("utf-8")
    ).hexdigest()[:40]
    return f"ucr-attendance:{digest}:r{revision}"


def _payload_fingerprint(
    *,
    connector_id: str,
    customer_ref: str,
    event: ExternalProductEvent,
) -> str:
    observation = event.observation
    if observation is None:
        raise ValueError("UCR attendance pilot event requires an observation")
    payload = {
        "connector_id": connector_id,
        "customer_fingerprint": external_customer_fingerprint(
            connector_id=connector_id,
            customer_ref=customer_ref,
        ),
        "external_event_id": event.external_event_id,
        "occurred_at": event.occurred_at.astimezone(timezone.utc).isoformat(),
        "observation": observation.canonical_metadata(),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def sync_ucr_attendance_pilot(
    *,
    actor: TenantContext,
    connector_id: str,
    customer_id: str,
    customer_ref: str,
    canonical_request: Mapping[str, Any],
    observation_key: str,
    gateway: UcrAttendanceGateway | None = None,
    received_at: datetime | None = None,
) -> UcrAttendancePilotSyncResult:
    """Explicitly sync one verified UCR attendance snapshot into canonical evidence.

    This is a manual/trusted-pull pilot boundary. It does not poll in the background,
    create Sales/Outcome facts, change CRM stages, send messages, or grant automation
    authority. The customer reference must already be explicitly bound.
    """

    normalized_customer = normalize_uuid(customer_id, field_name="customer_id")
    key = normalize_external_observation_key(observation_key)
    received = _received_at(received_at)

    with get_db_ro() as conn:
        repository = ExternalProductRepository(conn)
        connector = repository.get_active_trusted_pull(
            actor=actor,
            connector_id=connector_id,
        )
        bound_customer = repository.resolve_bound_customer_ref(
            actor=actor,
            connector_id=connector.id,
            customer_ref=customer_ref,
        )
        if bound_customer != normalized_customer:
            raise ExternalProductInvariantViolation(
                "trusted pull customer reference belongs to another customer"
            )
        current_head = repository.current_observation_head(
            actor=actor,
            connector_id=connector.id,
            customer_id=normalized_customer,
            observation_key=key,
        )

    read = await read_ucr_attendance_observation(
        gateway=gateway or UcrGatewayClient(),
        canonical_request=canonical_request,
        observation_key=key,
        current_head=current_head,
    )
    observation = read.observation
    if observation is None:
        return UcrAttendancePilotSyncResult(
            attendance=read.attendance,
            receipt=None,
            persisted=False,
        )

    if (
        current_head is not None
        and current_head.observation_provenance_ref == observation.provenance_ref
    ):
        return UcrAttendancePilotSyncResult(
            attendance=read.attendance,
            receipt=current_head,
            persisted=False,
        )

    event = ExternalProductEvent(
        external_event_id=_event_id(
            observation_key=key,
            provenance_ref=observation.provenance_ref,
            revision=observation.revision,
        ),
        event_type=ExternalProductEventType.EVIDENCE,
        occurred_at=observation.observed_at,
        customer_ref=customer_ref,
        observation=observation,
        metadata={"source_adapter": "ucr_attendance_pilot"},
    )
    fingerprint = _payload_fingerprint(
        connector_id=connector.id,
        customer_ref=customer_ref,
        event=event,
    )
    with get_db() as conn:
        receipt = ExternalProductRepository(conn).ingest_trusted_pull_event(
            actor=actor,
            connector_id=connector.id,
            event=event,
            payload_fingerprint=fingerprint,
            expected_customer_id=normalized_customer,
            received_at=received,
        )
    return UcrAttendancePilotSyncResult(
        attendance=read.attendance,
        receipt=receipt,
        persisted=True,
    )


__all__ = ["UcrAttendancePilotSyncResult", "sync_ucr_attendance_pilot"]
