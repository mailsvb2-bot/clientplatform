from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from clientplatform.domain.tenancy import normalize_uuid


class ActivityError(RuntimeError):
    """Base error for a business activity profile and its connected modules."""


class ActivityNotFound(ActivityError):
    """The requested tenant-scoped activity object does not exist."""


class ActivityInvariantViolation(ActivityError):
    """An activity profile transition would violate a domain invariant."""


class BusinessProfileStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"


class CapabilityKind(StrEnum):
    PROGRAMS = "programs"
    CONSULTATIONS = "consultations"
    SERVICES = "services"
    CUSTOM = "custom"


class CapabilityStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class OfferingStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class InviteStatus(StrEnum):
    ACTIVE = "active"
    CLAIMED = "claimed"
    REVOKED = "revoked"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class ActivityConnector:
    key: str
    kind: CapabilityKind
    title: str
    description: str
    supports_offerings: bool


_CONNECTORS = (
    ActivityConnector(
        key="programs",
        kind=CapabilityKind.PROGRAMS,
        title="Программы и материалы",
        description="Курсы, уроки, аудио, видео, тексты и документы с выдачей клиентам.",
        supports_offerings=False,
    ),
    ActivityConnector(
        key="consultations",
        kind=CapabilityKind.CONSULTATIONS,
        title="Консультации",
        description="Личные, групповые, разовые или регулярные консультации.",
        supports_offerings=True,
    ),
    ActivityConnector(
        key="services",
        kind=CapabilityKind.SERVICES,
        title="Услуги",
        description="Работы, записи, заказы и другие услуги для клиентов.",
        supports_offerings=True,
    ),
    ActivityConnector(
        key="custom",
        kind=CapabilityKind.CUSTOM,
        title="Свой формат работы",
        description="Любой дополнительный формат, который владелец описывает своими словами.",
        supports_offerings=True,
    ),
)

ACTIVITY_CONNECTORS = MappingProxyType({item.key: item for item in _CONNECTORS})
if len(ACTIVITY_CONNECTORS) != len(_CONNECTORS):
    raise RuntimeError("duplicate_clientplatform_activity_connector")


def _normalize_text(value: str, *, field_name: str, maximum: int) -> str:
    raw = str(value or "").replace("\x00", " ")
    normalized = re.sub(r"\s+", " ", raw).strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} must be at most {maximum} characters")
    return normalized


def normalize_activity_description(value: str) -> str:
    return _normalize_text(value, field_name="activity description", maximum=2000)


def normalize_timezone(value: str) -> str:
    normalized = _normalize_text(value, field_name="timezone", maximum=100)
    if not re.fullmatch(r"[A-Za-z0-9._+\-/]+", normalized):
        raise ValueError("timezone contains unsupported characters")
    return normalized


def normalize_connector_key(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", normalized):
        raise ValueError("connector key must be a lowercase stable identifier")
    return normalized


def resolve_activity_connector(value: str) -> ActivityConnector:
    key = normalize_connector_key(value)
    try:
        return ACTIVITY_CONNECTORS[key]
    except KeyError as exc:
        raise ValueError(f"unsupported activity connector: {key}") from exc


def normalize_capability_title(value: str) -> str:
    return _normalize_text(value, field_name="capability title", maximum=160)


def normalize_offering_title(value: str) -> str:
    return _normalize_text(value, field_name="offering title", maximum=200)


def normalize_offering_description(value: str) -> str:
    return _normalize_text(value, field_name="offering description", maximum=2000)


def new_invite_token() -> str:
    return secrets.token_urlsafe(24)


def invite_token_hash(token: str) -> str:
    normalized = str(token or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,128}", normalized):
        raise ValueError("invalid customer invite token")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class BusinessProfile:
    business_id: str
    activity_description: str
    timezone: str
    status: BusinessProfileStatus
    created_by_member_id: str
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "business_id", normalize_uuid(self.business_id, field_name="business_id"))
        object.__setattr__(
            self,
            "created_by_member_id",
            normalize_uuid(self.created_by_member_id, field_name="created_by_member_id"),
        )
        object.__setattr__(self, "activity_description", normalize_activity_description(self.activity_description))
        object.__setattr__(self, "timezone", normalize_timezone(self.timezone))


@dataclass(frozen=True, slots=True)
class BusinessCapability:
    id: str
    business_id: str
    connector_key: str
    kind: CapabilityKind
    title: str
    status: CapabilityStatus
    created_by_member_id: str
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", normalize_uuid(self.id, field_name="capability_id"))
        object.__setattr__(self, "business_id", normalize_uuid(self.business_id, field_name="business_id"))
        object.__setattr__(self, "connector_key", normalize_connector_key(self.connector_key))
        object.__setattr__(self, "title", normalize_capability_title(self.title))
        object.__setattr__(
            self,
            "created_by_member_id",
            normalize_uuid(self.created_by_member_id, field_name="created_by_member_id"),
        )


@dataclass(frozen=True, slots=True)
class BusinessOffering:
    id: str
    business_id: str
    capability_id: str
    title: str
    description: str
    status: OfferingStatus
    created_by_member_id: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class CustomerInvite:
    id: str
    business_id: str
    status: InviteStatus
    expires_at: str
    created_by_member_id: str
    created_at: str
    claimed_customer_id: str | None = None
    claimed_at: str | None = None
    revoked_at: str | None = None


@dataclass(frozen=True, slots=True)
class IssuedCustomerInvite:
    invite: CustomerInvite
    token: str


@dataclass(frozen=True, slots=True)
class InviteClaim:
    business_id: str
    business_name: str
    customer_id: str
    already_connected: bool
