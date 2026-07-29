from __future__ import annotations

from dataclasses import dataclass

from clientplatform.domain.connections import ConnectionStatus, ManagedBotStatus
from clientplatform.domain.tenancy import normalize_uuid


class ManagedBotOwnerError(RuntimeError):
    """Base error for owner-facing managed bot lifecycle operations."""


class ManagedBotWebhookOperationFailed(ManagedBotOwnerError):
    """Telegram webhook attachment or detachment could not be completed."""


def _non_negative(value: int, *, field_name: str) -> int:
    normalized = int(value)
    if normalized < 0:
        raise ValueError(f"{field_name} must not be negative")
    return normalized


@dataclass(frozen=True, slots=True)
class ManagedBotOwnerSnapshot:
    managed_bot_id: str
    business_id: str
    connection_id: str
    external_bot_id: str
    username: str | None
    display_name: str | None
    bot_status: ManagedBotStatus
    connection_status: ConnectionStatus
    pending_events: int
    processing_events: int
    retry_events: int
    processed_events: int
    dead_events: int
    bot_updated_at: str
    connection_updated_at: str
    last_event_at: str | None = None
    last_processed_at: str | None = None
    last_dead_at: str | None = None
    last_connection_success_at: str | None = None
    last_connection_error_at: str | None = None

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
        if not isinstance(self.bot_status, ManagedBotStatus):
            object.__setattr__(
                self,
                "bot_status",
                ManagedBotStatus(str(self.bot_status)),
            )
        if not isinstance(self.connection_status, ConnectionStatus):
            object.__setattr__(
                self,
                "connection_status",
                ConnectionStatus(str(self.connection_status)),
            )
        for field_name in (
            "pending_events",
            "processing_events",
            "retry_events",
            "processed_events",
            "dead_events",
        ):
            object.__setattr__(
                self,
                field_name,
                _non_negative(getattr(self, field_name), field_name=field_name),
            )

    @property
    def is_active(self) -> bool:
        return (
            self.bot_status == ManagedBotStatus.ACTIVE
            and self.connection_status == ConnectionStatus.ACTIVE
        )

    @property
    def in_flight_events(self) -> int:
        return self.pending_events + self.processing_events + self.retry_events


@dataclass(frozen=True, slots=True)
class ManagedBotOwnerLifecycleResult:
    snapshot: ManagedBotOwnerSnapshot
    webhook_synchronized: bool
    warning_code: str | None = None

    def __post_init__(self) -> None:
        if self.webhook_synchronized and self.warning_code is not None:
            raise ValueError("synchronized webhook result cannot contain a warning")
        if self.warning_code is not None:
            normalized = str(self.warning_code).strip().lower()
            if not normalized or len(normalized) > 64:
                raise ValueError("warning_code must be a stable bounded code")
            object.__setattr__(self, "warning_code", normalized)
