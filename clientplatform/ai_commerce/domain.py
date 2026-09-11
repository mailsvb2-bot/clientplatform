from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from clientplatform.domain.tenancy import normalize_uuid


class ProposalKind(StrEnum):
    INCLUDED = "included"
    UPGRADE = "upgrade"


def _required_text(value: object, *, field_name: str, maximum: int) -> str:
    normalized = " ".join(str(value or "").replace("\x00", " ").split()).strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} is too long")
    return normalized


def _non_negative_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a non-negative integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a non-negative integer") from exc
    if normalized < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return normalized


@dataclass(frozen=True, slots=True)
class CommerceOperation:
    operation_id: str
    business_id: str
    member_id: str
    sku: str
    requested_units: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "operation_id", _required_text(self.operation_id, field_name="operation_id", maximum=200)
        )
        object.__setattr__(
            self, "business_id", normalize_uuid(self.business_id, field_name="business_id")
        )
        object.__setattr__(
            self, "member_id", normalize_uuid(self.member_id, field_name="member_id")
        )
        object.__setattr__(self, "sku", _required_text(self.sku, field_name="sku", maximum=120))
        if isinstance(self.requested_units, bool):
            raise ValueError("requested_units must be a positive integer")
        try:
            units = int(self.requested_units)
        except (TypeError, ValueError) as exc:
            raise ValueError("requested_units must be a positive integer") from exc
        if units < 1:
            raise ValueError("requested_units must be a positive integer")
        object.__setattr__(self, "requested_units", units)


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    used: int
    allowance: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "used", _non_negative_int(self.used, field_name="used"))
        object.__setattr__(
            self, "allowance", _non_negative_int(self.allowance, field_name="allowance")
        )

    @property
    def remaining(self) -> int:
        return max(0, self.allowance - self.used)


@dataclass(frozen=True, slots=True)
class CommerceProposal:
    proposal_id: str
    operation_id: str
    business_id: str
    sku: str
    kind: ProposalKind
    reason: str
    used: int
    allowance: int
    requested_units: int
    projected_total: int

    @property
    def requires_upgrade(self) -> bool:
        return self.kind is ProposalKind.UPGRADE


class UsageSnapshotProvider(Protocol):
    def get_usage(self, *, business_id: str, sku: str) -> UsageSnapshot: ...
