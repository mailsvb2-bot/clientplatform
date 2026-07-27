from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from a1.domain.tenancy import normalize_uuid


class CustomerError(RuntimeError):
    """Base error for tenant-scoped customer records."""


class CustomerNotFound(CustomerError):
    """No customer exists in the active business scope."""


class CustomerIdentityConflict(CustomerError):
    """An external identity already belongs to another customer in this business."""


class CustomerStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class CustomerIdentityStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class CustomerPlatform(StrEnum):
    TELEGRAM = "telegram"
    VK = "vk"
    MAX = "max"
    EMAIL = "email"
    PHONE = "phone"
    WEB = "web"
    INTERNAL = "internal"


def normalize_optional_person_name(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    raw = str(value).replace("\x00", " ")
    normalized = re.sub(r"\s+", " ", raw).strip()
    if not normalized:
        return None
    if len(normalized) > 200:
        raise ValueError(f"{field_name} must be at most 200 characters")
    return normalized


def normalize_platform(value: CustomerPlatform | str) -> CustomerPlatform:
    try:
        return value if isinstance(value, CustomerPlatform) else CustomerPlatform(str(value).strip().lower())
    except ValueError as exc:
        raise ValueError(f"unsupported customer platform: {value!r}") from exc


def normalize_external_subject(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("external_subject must not be empty")
    if len(normalized) > 512:
        raise ValueError("external_subject must be at most 512 characters")
    if any(ord(char) < 32 for char in normalized):
        raise ValueError("external_subject contains control characters")
    return normalized


@dataclass(frozen=True, slots=True)
class Customer:
    id: str
    business_id: str
    display_name: str | None
    status: CustomerStatus
    created_by_member_id: str
    created_at: str
    updated_at: str
    archived_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", normalize_uuid(self.id, field_name="customer_id"))
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


@dataclass(frozen=True, slots=True)
class CustomerIdentity:
    id: str
    business_id: str
    customer_id: str
    platform: CustomerPlatform
    external_subject: str
    username: str | None
    display_name: str | None
    status: CustomerIdentityStatus
    created_at: str
    updated_at: str
    revoked_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", normalize_uuid(self.id, field_name="identity_id"))
        object.__setattr__(
            self,
            "business_id",
            normalize_uuid(self.business_id, field_name="business_id"),
        )
        object.__setattr__(
            self,
            "customer_id",
            normalize_uuid(self.customer_id, field_name="customer_id"),
        )
        object.__setattr__(self, "platform", normalize_platform(self.platform))
        object.__setattr__(
            self,
            "external_subject",
            normalize_external_subject(self.external_subject),
        )


@dataclass(frozen=True, slots=True)
class CustomerRecord:
    customer: Customer
    identities: tuple[CustomerIdentity, ...]
