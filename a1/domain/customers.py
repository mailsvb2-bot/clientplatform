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


def normalize_optional_person_name(
    value: str | None,
    *,
    field_name: str,
) -> str | None:
    if value is None:
        return None
    raw = str(value).replace("\x00", " ")
    normalized = re.sub(r"\s+", " ", raw).strip()
    if not normalized:
        return None
    if len(normalized) > 200:
        raise ValueError(f"{field_name} must be at most 200 characters")
    return normalized


def normalize_optional_handle(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if len(normalized) > 200:
        raise ValueError("username must be at most 200 characters")
    if any(ord(char) < 32 for char in normalized):
        raise ValueError("username contains control characters")
    return normalized


def normalize_platform(value: CustomerPlatform | str) -> CustomerPlatform:
    try:
        if isinstance(value, CustomerPlatform):
            return value
        return CustomerPlatform(str(value).strip().lower())
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


def normalize_identity_subject(
    platform: CustomerPlatform | str,
    value: str,
) -> tuple[CustomerPlatform, str]:
    normalized_platform = normalize_platform(platform)
    normalized_subject = normalize_external_subject(value)
    if normalized_platform == CustomerPlatform.EMAIL:
        normalized_subject = normalized_subject.casefold()
    elif normalized_platform == CustomerPlatform.PHONE:
        digits = "".join(char for char in normalized_subject if char.isdigit())
        if not 7 <= len(digits) <= 15:
            raise ValueError("phone identity must contain 7 to 15 digits")
        normalized_subject = digits
    return normalized_platform, normalized_subject


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
        object.__setattr__(
            self,
            "id",
            normalize_uuid(self.id, field_name="customer_id"),
        )
        object.__setattr__(
            self,
            "business_id",
            normalize_uuid(self.business_id, field_name="business_id"),
        )
        object.__setattr__(
            self,
            "created_by_member_id",
            normalize_uuid(
                self.created_by_member_id,
                field_name="created_by_member_id",
            ),
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
        object.__setattr__(
            self,
            "id",
            normalize_uuid(self.id, field_name="identity_id"),
        )
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
        platform, subject = normalize_identity_subject(
            self.platform,
            self.external_subject,
        )
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "external_subject", subject)


@dataclass(frozen=True, slots=True)
class CustomerRecord:
    customer: Customer
    identities: tuple[CustomerIdentity, ...]
