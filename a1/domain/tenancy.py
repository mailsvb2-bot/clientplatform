from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class TenancyError(RuntimeError):
    """Base error for the explicit A1 tenant boundary."""


class TenantAccessDenied(TenancyError):
    """The principal has no active membership in the requested business."""


class TenantPermissionDenied(TenancyError):
    """The active member lacks permission for the requested operation."""


class TenantInvariantViolation(TenancyError):
    """A mutation would break a required tenant invariant."""


class BusinessStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    ARCHIVED = "archived"


class MembershipStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class PlatformRole(StrEnum):
    OWNER = "owner"
    ADMINISTRATOR = "administrator"
    MANAGER = "manager"
    CONTENT_MANAGER = "content_manager"
    MARKETER = "marketer"
    ANALYST = "analyst"
    SUPPORT = "support"
    CUSTOMER = "customer"


BUSINESS_MEMBER_ROLES = frozenset(
    {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.CONTENT_MANAGER,
        PlatformRole.MARKETER,
        PlatformRole.ANALYST,
        PlatformRole.SUPPORT,
    }
)

_ADMIN_ASSIGNABLE_ROLES = frozenset(
    {
        PlatformRole.MANAGER,
        PlatformRole.CONTENT_MANAGER,
        PlatformRole.MARKETER,
        PlatformRole.ANALYST,
        PlatformRole.SUPPORT,
    }
)

_CUSTOMER_RECORD_ROLES = frozenset(
    {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.SUPPORT,
    }
)


def normalize_user_id(value: int) -> int:
    if isinstance(value, bool):
        raise ValueError("user_id must be a positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("user_id must be a positive integer") from exc
    if normalized <= 0:
        raise ValueError("user_id must be a positive integer")
    return normalized


def normalize_uuid(value: str, *, field_name: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = UUID(raw)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field_name} must be a valid UUID") from exc
    return str(parsed)


def normalize_business_name(value: str) -> str:
    raw = str(value or "").replace("\x00", " ")
    normalized = re.sub(r"\s+", " ", raw).strip()
    if not normalized:
        raise ValueError("business name must not be empty")
    if len(normalized) > 160:
        raise ValueError("business name must be at most 160 characters")
    return normalized


def parse_business_member_role(value: PlatformRole | str) -> PlatformRole:
    try:
        role = value if isinstance(value, PlatformRole) else PlatformRole(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"unsupported business role: {value!r}") from exc
    if role not in BUSINESS_MEMBER_ROLES:
        raise ValueError("customer is not a BusinessMember role")
    return role


@dataclass(frozen=True, slots=True)
class Business:
    id: str
    name: str
    status: BusinessStatus
    created_by_user_id: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class BusinessMember:
    id: str
    business_id: str
    user_id: int
    role: PlatformRole
    status: MembershipStatus
    created_at: str
    updated_at: str
    revoked_at: str | None = None


@dataclass(frozen=True, slots=True)
class BusinessAccess:
    business: Business
    membership: BusinessMember


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Server-resolved, immutable tenant context for one active membership."""

    business_id: str
    user_id: int
    membership_id: str
    role: PlatformRole

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "business_id",
            normalize_uuid(self.business_id, field_name="business_id"),
        )
        object.__setattr__(
            self,
            "membership_id",
            normalize_uuid(self.membership_id, field_name="membership_id"),
        )
        object.__setattr__(self, "user_id", normalize_user_id(self.user_id))
        object.__setattr__(self, "role", parse_business_member_role(self.role))

    def assert_business(self, object_business_id: str) -> None:
        normalized = normalize_uuid(
            object_business_id,
            field_name="object_business_id",
        )
        if normalized != self.business_id:
            raise TenantAccessDenied("object belongs to another business")

    def assert_can_manage_members(self, target_role: PlatformRole | str) -> PlatformRole:
        target = parse_business_member_role(target_role)
        if self.role == PlatformRole.OWNER:
            return target
        if self.role == PlatformRole.ADMINISTRATOR and target in _ADMIN_ASSIGNABLE_ROLES:
            return target
        raise TenantPermissionDenied("member management is not allowed for this role")

    def assert_can_manage_business(self) -> None:
        if self.role not in {PlatformRole.OWNER, PlatformRole.ADMINISTRATOR}:
            raise TenantPermissionDenied(
                "business management requires owner or administrator role"
            )

    def assert_can_view_customer_records(self) -> None:
        if self.role not in _CUSTOMER_RECORD_ROLES:
            raise TenantPermissionDenied(
                "customer records require owner, administrator, manager or support role"
            )

    def assert_can_manage_customer_records(self) -> None:
        self.assert_can_view_customer_records()
