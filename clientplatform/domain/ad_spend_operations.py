from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from clientplatform.domain.ad_spend import AdSpendInvariantViolation
from clientplatform.domain.tenancy import normalize_uuid


class AdSpendOperationType(StrEnum):
    LAUNCH = "launch"
    STOP = "stop"


class AdSpendOperationStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    RETRY = "retry"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def _timestamp(value: datetime | str | None, *, name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def ad_spend_operation_key(*, business_id: str, authorization_id: str, operation_type: AdSpendOperationType | str) -> str:
    business = normalize_uuid(business_id, field_name="business_id")
    authorization = normalize_uuid(authorization_id, field_name="ad_spend_authorization_id")
    operation = AdSpendOperationType(operation_type)
    return "adspendop_" + hashlib.sha256(f"{business}:{authorization}:{operation.value}".encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class AdSpendOperation:
    id: str
    business_id: str
    authorization_id: str
    operation_type: AdSpendOperationType
    status: AdSpendOperationStatus
    idempotency_key: str
    attempts: int
    available_at: str
    created_at: str
    updated_at: str
    locked_at: str | None = None
    lock_token: str | None = None
    last_error_code: str | None = None
    completed_at: str | None = None
    dead_at: str | None = None

    def __post_init__(self) -> None:
        for name in ("id", "business_id", "authorization_id"):
            object.__setattr__(self, name, normalize_uuid(getattr(self, name), field_name=name))
        object.__setattr__(self, "operation_type", AdSpendOperationType(self.operation_type))
        object.__setattr__(self, "status", AdSpendOperationStatus(self.status))
        expected = ad_spend_operation_key(business_id=self.business_id, authorization_id=self.authorization_id, operation_type=self.operation_type)
        if self.idempotency_key != expected:
            raise AdSpendInvariantViolation("spend operation idempotency key is invalid")
        if isinstance(self.attempts, bool) or int(self.attempts) < 0:
            raise ValueError("spend operation attempts must be nonnegative")
        object.__setattr__(self, "attempts", int(self.attempts))
        for name in ("available_at", "created_at", "updated_at"):
            object.__setattr__(self, name, _timestamp(getattr(self, name), name=name))
        for name in ("locked_at", "completed_at", "dead_at"):
            object.__setattr__(self, name, _timestamp(getattr(self, name), name=name))
        if self.lock_token is not None:
            object.__setattr__(self, "lock_token", normalize_uuid(self.lock_token, field_name="lock_token"))
        if self.status == AdSpendOperationStatus.PROCESSING and not self.lock_token:
            raise AdSpendInvariantViolation("processing spend operation requires a lease")
        if self.status != AdSpendOperationStatus.PROCESSING and self.lock_token:
            raise AdSpendInvariantViolation("non-processing spend operation cannot keep a lease")
        if self.last_error_code is not None:
            code = "_".join(str(self.last_error_code).strip().lower().split())
            code = "".join(ch for ch in code if ch.isalnum() or ch in "_.-")[:120]
            if not code:
                raise ValueError("spend operation error code is invalid")
            object.__setattr__(self, "last_error_code", code)
        if self.status == AdSpendOperationStatus.SUCCEEDED and self.completed_at is None:
            raise AdSpendInvariantViolation("successful spend operation requires completion time")
        if self.status == AdSpendOperationStatus.FAILED and self.dead_at is None:
            raise AdSpendInvariantViolation("failed spend operation requires dead time")


__all__ = ["AdSpendOperation", "AdSpendOperationStatus", "AdSpendOperationType", "ad_spend_operation_key"]
