from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol
from urllib.parse import urlsplit


class ConferenceProviderError(RuntimeError):
    """Base failure for the conference-provider boundary."""


class ConferenceProviderUnavailable(ConferenceProviderError):
    """The selected provider cannot currently complete the requested operation."""


class ConferenceMode(str, Enum):
    MEETING = "meeting"
    WEBINAR = "webinar"
    BROADCAST = "broadcast"
    AUDIO_ROOM = "audio_room"


class ConferenceLifecycle(str, Enum):
    SCHEDULED = "scheduled"
    WAITING = "waiting"
    LIVE = "live"
    ENDING = "ending"
    ENDED = "ended"


class ConferenceRole(str, Enum):
    OWNER = "owner"
    HOST = "host"
    MODERATOR = "moderator"
    SPEAKER = "speaker"
    ATTENDEE = "attendee"


@dataclass(frozen=True, slots=True)
class ConferenceCapabilities:
    provider_key: str
    managed_rooms: bool
    join_link: bool
    attendance: bool
    recording: bool
    max_participants: int | None = None
    production_realtime: bool = False


@dataclass(frozen=True, slots=True)
class ConferenceCreateSpec:
    tenant_id: str
    external_conference_id: str
    mode: ConferenceMode
    starts_at_unix_ms: int
    planned_end_unix_ms: int | None = None
    timezone: str | None = None

    def __post_init__(self) -> None:
        if not str(self.tenant_id or "").strip():
            raise ValueError("conference tenant_id is required")
        if not str(self.external_conference_id or "").strip():
            raise ValueError("external conference id is required")
        if int(self.starts_at_unix_ms) <= 0:
            raise ValueError("conference start time must be positive")
        if (
            self.planned_end_unix_ms is not None
            and int(self.planned_end_unix_ms) <= int(self.starts_at_unix_ms)
        ):
            raise ValueError("conference planned end must be after start")


@dataclass(frozen=True, slots=True)
class ConferenceRef:
    provider_key: str
    external_conference_id: str
    provider_conference_id: str
    lifecycle: ConferenceLifecycle | None = None

    def __post_init__(self) -> None:
        if not str(self.provider_key or "").strip():
            raise ValueError("conference provider key is required")
        if not str(self.external_conference_id or "").strip():
            raise ValueError("external conference id is required")
        if not str(self.provider_conference_id or "").strip():
            raise ValueError("provider conference id is required")


@dataclass(frozen=True, slots=True)
class ConferenceJoin:
    url: str
    session_id: str | None = None

    def __post_init__(self) -> None:
        parsed = urlsplit(str(self.url or "").strip())
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("conference join URL must be absolute HTTPS")


@dataclass(frozen=True, slots=True)
class ConferenceAttendance:
    connected: bool
    total_connected_seconds: int
    join_count: int
    reconnect_count: int = 0


class ConferenceProvider(Protocol):
    key: str

    async def capabilities(self, *, tenant_id: str) -> ConferenceCapabilities: ...

    async def create(
        self,
        spec: ConferenceCreateSpec,
        *,
        idempotency_key: str,
    ) -> ConferenceRef: ...

    async def resolve(
        self,
        *,
        tenant_id: str,
        external_conference_id: str,
    ) -> ConferenceRef: ...

    async def set_lifecycle(
        self,
        conference: ConferenceRef,
        *,
        tenant_id: str,
        lifecycle: ConferenceLifecycle,
        idempotency_key: str,
    ) -> ConferenceRef: ...

    async def set_entry_open(
        self,
        conference: ConferenceRef,
        *,
        tenant_id: str,
        open: bool,
        idempotency_key: str,
    ) -> ConferenceRef: ...

    async def ensure_participant(
        self,
        conference: ConferenceRef,
        *,
        tenant_id: str,
        external_user_id: str,
        role: ConferenceRole,
        idempotency_key: str,
    ) -> None: ...

    async def prepare(
        self,
        conference: ConferenceRef,
        *,
        tenant_id: str,
        idempotency_key: str,
    ) -> None: ...

    async def issue_join(
        self,
        conference: ConferenceRef,
        *,
        tenant_id: str,
        external_user_id: str,
        idempotency_key: str,
        ttl_seconds: int = 3600,
    ) -> ConferenceJoin: ...

    async def attendance(
        self,
        conference: ConferenceRef,
        *,
        tenant_id: str,
        external_user_id: str,
    ) -> ConferenceAttendance: ...


class ConferenceProviderRegistry:
    """Routes to providers without owning conference business state."""

    def __init__(self, providers: tuple[ConferenceProvider, ...]) -> None:
        indexed: dict[str, ConferenceProvider] = {}
        for provider in providers:
            key = str(provider.key or "").strip().casefold()
            if not key or key in indexed:
                raise ValueError("conference provider keys must be unique")
            indexed[key] = provider
        self._providers = indexed

    def get(self, key: str) -> ConferenceProvider:
        normalized = str(key or "").strip().casefold()
        try:
            return self._providers[normalized]
        except KeyError as exc:
            raise ValueError("unsupported conference provider") from exc


class ExternalHttpsRoomProvider:
    """Link-only adapter for a room already created in another video service."""

    def __init__(self, *, key: str, room_url: str) -> None:
        normalized = str(key or "").strip().casefold()
        parsed = urlsplit(str(room_url or "").strip())
        if not normalized:
            raise ValueError("conference provider key is required")
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("external conference room must be absolute HTTPS")
        self.key = normalized
        self.room_url = str(room_url).strip()

    def _validate_ref(self, conference: ConferenceRef) -> None:
        if conference.provider_key.casefold() != self.key:
            raise ValueError("conference reference belongs to a different provider")

    async def capabilities(self, *, tenant_id: str) -> ConferenceCapabilities:
        del tenant_id
        return ConferenceCapabilities(
            provider_key=self.key,
            managed_rooms=False,
            join_link=True,
            attendance=False,
            recording=False,
            production_realtime=False,
        )

    async def create(
        self, spec: ConferenceCreateSpec, *, idempotency_key: str
    ) -> ConferenceRef:
        del idempotency_key
        return ConferenceRef(
            provider_key=self.key,
            external_conference_id=spec.external_conference_id,
            provider_conference_id=spec.external_conference_id,
        )

    async def resolve(
        self,
        *,
        tenant_id: str,
        external_conference_id: str,
    ) -> ConferenceRef:
        del tenant_id
        return ConferenceRef(
            provider_key=self.key,
            external_conference_id=external_conference_id,
            provider_conference_id=external_conference_id,
        )

    async def set_lifecycle(
        self, conference: ConferenceRef, **kwargs: object
    ) -> ConferenceRef:
        del conference, kwargs
        raise ConferenceProviderUnavailable(
            "link-only provider cannot manage conference lifecycle"
        )

    async def set_entry_open(
        self, conference: ConferenceRef, **kwargs: object
    ) -> ConferenceRef:
        del conference, kwargs
        raise ConferenceProviderUnavailable(
            "link-only provider cannot manage conference entry"
        )

    async def ensure_participant(self, conference: ConferenceRef, **kwargs: object) -> None:
        del conference, kwargs
        raise ConferenceProviderUnavailable(
            "link-only provider cannot manage participants"
        )

    async def prepare(self, conference: ConferenceRef, **kwargs: object) -> None:
        del conference, kwargs
        raise ConferenceProviderUnavailable(
            "link-only provider cannot prepare provider runtime"
        )

    async def issue_join(
        self, conference: ConferenceRef, **kwargs: object
    ) -> ConferenceJoin:
        self._validate_ref(conference)
        del kwargs
        return ConferenceJoin(url=self.room_url)

    async def attendance(
        self, conference: ConferenceRef, **kwargs: object
    ) -> ConferenceAttendance:
        del conference, kwargs
        raise ConferenceProviderUnavailable(
            "link-only provider has no verified attendance API"
        )
