from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from clientplatform.domain.connections import normalize_credential_reference
from clientplatform.domain.tenancy import normalize_uuid


class BotProvisioningError(RuntimeError):
    """Base error for managed bot provisioning."""


class BotProvisioningNotFound(BotProvisioningError):
    """No provisioning request exists in the active tenant scope."""


class BotProvisioningInvariantViolation(BotProvisioningError):
    """A provisioning transition violates the durable state machine."""


class BotProvisioningVerificationFailed(BotProvisioningError):
    """The provider could not verify or configure the requested bot."""


class BotProvisioningWebhookConflict(BotProvisioningVerificationFailed):
    """An existing bot is already attached to another webhook consumer."""


class BotProvisioningProvider(StrEnum):
    TELEGRAM_MANAGED = "telegram_managed"
    BOTFATHER = "botfather"


class BotProvisioningStatus(StrEnum):
    AWAITING_SECRET = "awaiting_secret"
    READY = "ready"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_IDEMPOTENCY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")
_USERNAME_RE = re.compile(r"[A-Za-z0-9_]{5,32}")
_ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9_]{2,63}")


def normalize_provisioning_provider(
    value: BotProvisioningProvider | str,
) -> BotProvisioningProvider:
    try:
        if isinstance(value, BotProvisioningProvider):
            return value
        return BotProvisioningProvider(str(value or "").strip().lower())
    except ValueError as exc:
        raise ValueError(f"unsupported bot provisioning provider: {value!r}") from exc


def normalize_provisioning_idempotency_key(value: str) -> str:
    normalized = str(value or "").strip()
    if not _IDEMPOTENCY_RE.fullmatch(normalized):
        raise ValueError(
            "idempotency_key must be 8-128 safe characters and start alphanumeric"
        )
    return normalized


def normalize_requested_username(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lstrip("@").lower()
    if not normalized:
        return None
    if not _USERNAME_RE.fullmatch(normalized):
        raise ValueError("requested_username must be a valid Telegram bot username")
    if not normalized.endswith("bot"):
        raise ValueError("requested_username must end with 'bot'")
    return normalized


def normalize_display_name(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).replace("\x00", " ").split())
    if not normalized:
        return None
    if len(normalized) > 64:
        raise ValueError("display_name must be at most 64 characters")
    return normalized


def normalize_provisioning_error_code(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _ERROR_CODE_RE.fullmatch(normalized):
        raise ValueError("last_error_code must be a stable lowercase code")
    return normalized


@dataclass(frozen=True, slots=True)
class ManagedBotProvisioningRequest:
    id: str
    business_id: str
    created_by_member_id: str
    provider: BotProvisioningProvider
    status: BotProvisioningStatus
    idempotency_key: str
    requested_username: str | None
    display_name: str | None
    credential_reference: str | None
    webhook_secret_reference: str | None
    external_bot_id: str | None
    verified_username: str | None
    connection_id: str | None
    managed_bot_id: str | None
    attempts: int
    created_at: str
    updated_at: str
    verification_started_at: str | None = None
    completed_at: str | None = None
    failed_at: str | None = None
    cancelled_at: str | None = None
    last_error_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "id",
            normalize_uuid(self.id, field_name="bot_provisioning_request_id"),
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
        object.__setattr__(
            self,
            "provider",
            normalize_provisioning_provider(self.provider),
        )
        object.__setattr__(
            self,
            "status",
            self.status
            if isinstance(self.status, BotProvisioningStatus)
            else BotProvisioningStatus(str(self.status)),
        )
        object.__setattr__(
            self,
            "idempotency_key",
            normalize_provisioning_idempotency_key(self.idempotency_key),
        )
        object.__setattr__(
            self,
            "requested_username",
            normalize_requested_username(self.requested_username),
        )
        object.__setattr__(
            self,
            "display_name",
            normalize_display_name(self.display_name),
        )
        if self.credential_reference is not None:
            object.__setattr__(
                self,
                "credential_reference",
                normalize_credential_reference(self.credential_reference),
            )
        if self.webhook_secret_reference is not None:
            object.__setattr__(
                self,
                "webhook_secret_reference",
                normalize_credential_reference(self.webhook_secret_reference),
            )
        if self.external_bot_id is not None:
            external_bot_id = str(self.external_bot_id).strip()
            if not external_bot_id.isdigit() or int(external_bot_id) <= 0:
                raise ValueError("external_bot_id must be a positive Telegram bot id")
            object.__setattr__(self, "external_bot_id", external_bot_id)
        if self.verified_username is not None:
            object.__setattr__(
                self,
                "verified_username",
                normalize_requested_username(self.verified_username),
            )
        if self.connection_id is not None:
            object.__setattr__(
                self,
                "connection_id",
                normalize_uuid(self.connection_id, field_name="connection_id"),
            )
        if self.managed_bot_id is not None:
            object.__setattr__(
                self,
                "managed_bot_id",
                normalize_uuid(self.managed_bot_id, field_name="managed_bot_id"),
            )
        attempts = int(self.attempts)
        if attempts < 0:
            raise ValueError("provisioning attempts must not be negative")
        object.__setattr__(self, "attempts", attempts)
        if self.last_error_code is not None:
            object.__setattr__(
                self,
                "last_error_code",
                normalize_provisioning_error_code(self.last_error_code),
            )


@dataclass(frozen=True, slots=True)
class VerifiedTelegramBot:
    external_bot_id: str
    username: str
    display_name: str | None = None

    def __post_init__(self) -> None:
        external_bot_id = str(self.external_bot_id or "").strip()
        if not external_bot_id.isdigit() or int(external_bot_id) <= 0:
            raise ValueError("external_bot_id must be a positive Telegram bot id")
        object.__setattr__(self, "external_bot_id", external_bot_id)
        username = normalize_requested_username(self.username)
        if username is None:
            raise ValueError("verified Telegram bot username is required")
        object.__setattr__(self, "username", username)
        object.__setattr__(self, "display_name", normalize_display_name(self.display_name))
