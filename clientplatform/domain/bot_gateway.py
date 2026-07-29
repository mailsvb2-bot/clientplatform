from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from clientplatform.domain.connections import normalize_credential_reference
from clientplatform.domain.tenancy import normalize_uuid


class BotGatewayError(RuntimeError):
    """Base error for tenant-safe managed bot ingress."""


class ManagedBotRouteNotFound(BotGatewayError):
    """No active globally unique managed bot route exists."""


class BotGatewayAdmissionRejected(BotGatewayError):
    """An authenticated update cannot enter the bounded ingress queue."""


class BotGatewayReplayConflict(BotGatewayError):
    """A provider update id was reused with different content."""


class BotGatewayLeaseLost(BotGatewayError):
    """A worker attempted to mutate an event after losing its lease."""


class IngressEventStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    RETRY = "retry"
    PROCESSED = "processed"
    DEAD = "dead"


_UPDATE_ID_RE = re.compile(r"[0-9]{1,32}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def normalize_provider_update_id(value: int | str) -> str:
    normalized = str(value).strip()
    if not _UPDATE_ID_RE.fullmatch(normalized):
        raise ValueError("provider_update_id must be a positive decimal identifier")
    return normalized


def normalize_payload_sha256(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise ValueError("payload_sha256 must be a lowercase SHA-256 digest")
    return normalized


@dataclass(frozen=True, slots=True)
class ManagedBotRoute:
    managed_bot_id: str
    business_id: str
    connection_id: str
    external_bot_id: str
    credential_reference: str
    webhook_secret_reference: str
    username: str | None = None
    display_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "managed_bot_id",
            normalize_uuid(self.managed_bot_id, field_name="managed_bot_id"),
        )
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
        external_bot_id = str(self.external_bot_id or "").strip()
        if not external_bot_id.isdigit() or int(external_bot_id) <= 0:
            raise ValueError("external_bot_id must be a positive Telegram bot id")
        object.__setattr__(self, "external_bot_id", external_bot_id)
        object.__setattr__(
            self,
            "credential_reference",
            normalize_credential_reference(self.credential_reference),
        )
        object.__setattr__(
            self,
            "webhook_secret_reference",
            normalize_credential_reference(self.webhook_secret_reference),
        )


@dataclass(frozen=True, slots=True)
class IngressEvent:
    id: str
    business_id: str
    managed_bot_id: str
    provider_update_id: str
    payload_sha256: str
    payload_json: str | None
    status: IngressEventStatus
    attempts: int
    available_at: str
    created_at: str
    updated_at: str
    locked_at: str | None = None
    lock_token: str | None = None
    last_error_code: str | None = None
    processed_at: str | None = None
    dead_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", normalize_uuid(self.id, field_name="ingress_event_id"))
        object.__setattr__(
            self,
            "business_id",
            normalize_uuid(self.business_id, field_name="business_id"),
        )
        object.__setattr__(
            self,
            "managed_bot_id",
            normalize_uuid(self.managed_bot_id, field_name="managed_bot_id"),
        )
        object.__setattr__(
            self,
            "provider_update_id",
            normalize_provider_update_id(self.provider_update_id),
        )
        object.__setattr__(
            self,
            "payload_sha256",
            normalize_payload_sha256(self.payload_sha256),
        )
        attempts = int(self.attempts)
        if attempts < 0:
            raise ValueError("attempts must not be negative")
        object.__setattr__(self, "attempts", attempts)


@dataclass(frozen=True, slots=True)
class AdmittedIngressEvent:
    event: IngressEvent
    duplicate: bool


@dataclass(frozen=True, slots=True)
class ClaimedIngressEvent:
    event: IngressEvent
    route: ManagedBotRoute
