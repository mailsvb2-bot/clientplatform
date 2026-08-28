from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum

from clientplatform.domain.programs import ContentKind, normalize_content_kind, normalize_content_ref
from clientplatform.domain.tenancy import normalize_uuid


class ConnectionError(RuntimeError):
    """Base error for external clientplatform connections."""


class ConnectionNotFound(ConnectionError):
    """No usable connection exists in the active business."""


class ConnectionInvariantViolation(ConnectionError):
    """Connection metadata violates platform or secret-boundary rules."""


class DispatchError(RuntimeError):
    """Base error for transport-neutral dispatch work."""


class DispatchNotFound(DispatchError):
    """No dispatch exists in the requested tenant scope."""


class DispatchInvariantViolation(DispatchError):
    """A dispatch transition is not allowed."""


class DispatchLeaseLost(DispatchError):
    """A worker attempted to mutate a dispatch after losing its lease."""


class ConnectionPlatform(StrEnum):
    TELEGRAM = "telegram"
    VK = "vk"
    MAX = "max"
    EMAIL = "email"


class ConnectionType(StrEnum):
    TELEGRAM_SHARED_BOT = "telegram_shared_bot"
    TELEGRAM_MANAGED_BOT = "telegram_managed_bot"
    TELEGRAM_BUSINESS = "telegram_business"
    TELEGRAM_CHANNEL = "telegram_channel"
    VK_COMMUNITY = "vk_community"
    MAX_SHARED_BOT = "max_shared_bot"
    MAX_PERSONAL_BOT = "max_personal_bot"
    EMAIL_SMTP = "email_smtp"


class ConnectionStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    ATTENTION = "attention"
    DISABLED = "disabled"
    REVOKED = "revoked"


class ManagedBotStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    REVOKED = "revoked"


class DispatchStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    RETRY = "retry"
    SENT = "sent"
    DEAD = "dead"
    CANCELLED = "cancelled"


_CONNECTION_TYPE_PLATFORM = {
    ConnectionType.TELEGRAM_SHARED_BOT: ConnectionPlatform.TELEGRAM,
    ConnectionType.TELEGRAM_MANAGED_BOT: ConnectionPlatform.TELEGRAM,
    ConnectionType.TELEGRAM_BUSINESS: ConnectionPlatform.TELEGRAM,
    ConnectionType.TELEGRAM_CHANNEL: ConnectionPlatform.TELEGRAM,
    ConnectionType.VK_COMMUNITY: ConnectionPlatform.VK,
    ConnectionType.MAX_SHARED_BOT: ConnectionPlatform.MAX,
    ConnectionType.MAX_PERSONAL_BOT: ConnectionPlatform.MAX,
    ConnectionType.EMAIL_SMTP: ConnectionPlatform.EMAIL,
}


def normalize_connection_platform(
    value: ConnectionPlatform | str,
) -> ConnectionPlatform:
    try:
        if isinstance(value, ConnectionPlatform):
            return value
        return ConnectionPlatform(str(value).strip().lower())
    except ValueError as exc:
        raise ValueError(f"unsupported connection platform: {value!r}") from exc


def normalize_connection_type(
    value: ConnectionType | str,
) -> ConnectionType:
    try:
        if isinstance(value, ConnectionType):
            return value
        return ConnectionType(str(value).strip().lower())
    except ValueError as exc:
        raise ValueError(f"unsupported connection type: {value!r}") from exc


def validate_connection_type_platform(
    *,
    platform: ConnectionPlatform | str,
    connection_type: ConnectionType | str,
) -> tuple[ConnectionPlatform, ConnectionType]:
    normalized_platform = normalize_connection_platform(platform)
    normalized_type = normalize_connection_type(connection_type)
    if _CONNECTION_TYPE_PLATFORM[normalized_type] != normalized_platform:
        raise ConnectionInvariantViolation(
            "connection type does not belong to the selected platform"
        )
    return normalized_platform, normalized_type


def normalize_external_account_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("external_account_id must not be empty")
    if len(normalized) > 512:
        raise ValueError("external_account_id must be at most 512 characters")
    if any(ord(char) < 32 for char in normalized):
        raise ValueError("external_account_id contains control characters")
    return normalized


def normalize_credential_reference(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("credential_reference must not be empty")
    if len(normalized) > 512:
        raise ValueError("credential_reference must be at most 512 characters")
    if not re.fullmatch(r"(?:secret|kms|vault)://[A-Za-z0-9._:/@+-]+", normalized):
        raise ConnectionInvariantViolation(
            "credentials must be stored by reference, never as a raw token"
        )
    return normalized


def normalize_permissions(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(
        sorted(
            {
                item
                for item in (str(value or "").strip().lower() for value in values)
                if item
            }
        )
    )
    if any(len(item) > 120 for item in normalized):
        raise ValueError("connection permission must be at most 120 characters")
    return normalized


def encode_permissions(values: list[str] | tuple[str, ...]) -> str:
    return json.dumps(
        list(normalize_permissions(values)),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_permissions(raw: str) -> tuple[str, ...]:
    loaded = json.loads(str(raw or "[]"))
    if not isinstance(loaded, list) or not all(
        isinstance(item, str) for item in loaded
    ):
        raise ConnectionInvariantViolation(
            "connection permissions must be a JSON string list"
        )
    return normalize_permissions(loaded)


@dataclass(frozen=True, slots=True)
class Connection:
    id: str
    business_id: str
    platform: ConnectionPlatform
    connection_type: ConnectionType
    external_account_id: str
    credential_reference: str
    permissions: tuple[str, ...]
    status: ConnectionStatus
    created_by_member_id: str
    created_at: str
    updated_at: str
    last_success_at: str | None = None
    last_error_at: str | None = None
    last_error_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "id",
            normalize_uuid(self.id, field_name="connection_id"),
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
        platform, connection_type = validate_connection_type_platform(
            platform=self.platform,
            connection_type=self.connection_type,
        )
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "connection_type", connection_type)
        object.__setattr__(
            self,
            "external_account_id",
            normalize_external_account_id(self.external_account_id),
        )
        object.__setattr__(
            self,
            "credential_reference",
            normalize_credential_reference(self.credential_reference),
        )
        object.__setattr__(
            self,
            "permissions",
            normalize_permissions(self.permissions),
        )


@dataclass(frozen=True, slots=True)
class ManagedBot:
    id: str
    business_id: str
    connection_id: str
    platform: ConnectionPlatform
    external_bot_id: str
    username: str | None
    display_name: str | None
    webhook_secret_reference: str
    status: ManagedBotStatus
    created_at: str
    updated_at: str
    revoked_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", normalize_uuid(self.id, field_name="managed_bot_id"))
        object.__setattr__(
            self,
            "business_id",
            normalize_uuid(self.business_id, field_name="business_id"),
        )
        object.__setattr__(
            self,
            "connection_id",
            normalize_uuid(self.connection_id, field_name="connection_id"),
        )
        object.__setattr__(
            self,
            "platform",
            normalize_connection_platform(self.platform),
        )
        object.__setattr__(
            self,
            "external_bot_id",
            normalize_external_account_id(self.external_bot_id),
        )
        object.__setattr__(
            self,
            "webhook_secret_reference",
            normalize_credential_reference(self.webhook_secret_reference),
        )


@dataclass(frozen=True, slots=True)
class Dispatch:
    id: str
    business_id: str
    platform: ConnectionPlatform
    logical_delivery_id: str
    connection_id: str
    customer_identity_id: str
    payload_kind: ContentKind
    payload_ref: str
    idempotency_key: str
    status: DispatchStatus
    attempts: int
    available_at: str
    created_at: str
    updated_at: str
    locked_at: str | None = None
    lock_token: str | None = None
    provider_message_id: str | None = None
    last_error: str | None = None
    sent_at: str | None = None
    dead_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", normalize_uuid(self.id, field_name="dispatch_id"))
        object.__setattr__(
            self,
            "business_id",
            normalize_uuid(self.business_id, field_name="business_id"),
        )
        object.__setattr__(
            self,
            "logical_delivery_id",
            normalize_uuid(self.logical_delivery_id, field_name="logical_delivery_id"),
        )
        object.__setattr__(
            self,
            "connection_id",
            normalize_uuid(self.connection_id, field_name="connection_id"),
        )
        object.__setattr__(
            self,
            "customer_identity_id",
            normalize_uuid(
                self.customer_identity_id,
                field_name="customer_identity_id",
            ),
        )
        object.__setattr__(
            self,
            "platform",
            normalize_connection_platform(self.platform),
        )
        object.__setattr__(
            self,
            "payload_kind",
            normalize_content_kind(self.payload_kind),
        )
        object.__setattr__(
            self,
            "payload_ref",
            normalize_content_ref(self.payload_ref),
        )
        if self.attempts < 0:
            raise ValueError("dispatch attempts must be non-negative")
        if not str(self.idempotency_key or "").strip():
            raise ValueError("dispatch idempotency_key must not be empty")


@dataclass(frozen=True, slots=True)
class ClaimedDispatch:
    dispatch: Dispatch
    external_subject: str
    credential_reference: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "credential_reference",
            normalize_credential_reference(self.credential_reference),
        )
        object.__setattr__(
            self,
            "external_subject",
            normalize_external_account_id(self.external_subject),
        )
