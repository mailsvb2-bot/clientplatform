from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clientplatform.domain.tenancy import normalize_uuid


class EventValidationError(ValueError):
    """An event or public registration violates a stable domain invariant."""


class EventUnavailable(RuntimeError):
    """The requested public event action is not currently available."""


_EVENT_KINDS = frozenset({"webinar", "online_event", "workshop", "masterclass"})
_EVENT_STATUSES = frozenset({"draft", "published", "cancelled", "completed"})
_REGISTRATION_STATUSES = frozenset({"registered", "cancelled"})
_PROVIDER_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PUBLIC_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{20,96}$")
_CAPABILITY_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")

_PROVIDER_HOST_HINTS: tuple[tuple[str, str], ...] = (
    ("zoom.us", "zoom"),
    ("vk.com", "vk"),
    ("vkvideo.ru", "vk"),
    ("youtube.com", "youtube"),
    ("youtu.be", "youtube"),
    ("rutube.ru", "rutube"),
    ("webinar.ru", "webinar_ru"),
    ("mts-link.ru", "mts_link"),
    ("telemost.yandex.ru", "yandex_telemost"),
)

_PROVIDER_ALIASES = {
    "webinar.ru": "webinar_ru",
    "webinar-ru": "webinar_ru",
    "mts-link": "mts_link",
    "mts.link": "mts_link",
    "yandex-telemost": "yandex_telemost",
    "telemost": "yandex_telemost",
    "vk_video": "vk",
    "vk-video": "vk",
}


def _clean_text(value: object, *, field_name: str, maximum: int, required: bool = True) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split()).strip()
    if required and not text:
        raise EventValidationError(f"{field_name} is required")
    if len(text) > maximum:
        raise EventValidationError(f"{field_name} is too long")
    return text


def normalize_event_kind(value: object) -> str:
    kind = str(value or "").strip().lower()
    if kind not in _EVENT_KINDS:
        raise EventValidationError("unsupported event kind")
    return kind


def normalize_event_status(value: object) -> str:
    status = str(value or "").strip().lower()
    if status not in _EVENT_STATUSES:
        raise EventValidationError("unsupported event status")
    return status


def normalize_registration_status(value: object) -> str:
    status = str(value or "").strip().lower()
    if status not in _REGISTRATION_STATUSES:
        raise EventValidationError("unsupported registration status")
    return status


def normalize_email(value: object) -> str:
    # Reuse the canonical outbound-email parser so event registration and SMTP
    # dispatch agree on one address normalization rule.
    from clientplatform.domain.email_outbound import normalize_email_address

    try:
        return normalize_email_address(str(value or ""))
    except ValueError as exc:
        raise EventValidationError("invalid email") from exc


def normalize_phone(value: object | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    digits = "".join(char for char in raw if char.isdigit())
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if not 7 <= len(digits) <= 15:
        raise EventValidationError("invalid phone")
    return digits


def normalize_utc(value: datetime | str, *, field_name: str = "datetime") -> datetime:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    except (TypeError, ValueError) as exc:
        raise EventValidationError(f"{field_name} is invalid") from exc
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise EventValidationError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def normalize_timezone_name(value: object) -> str:
    name = _clean_text(value, field_name="timezone_name", maximum=80)
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise EventValidationError("unknown timezone") from exc
    return name


def validate_external_https_url(value: object, *, field_name: str = "url") -> str:
    url = str(value or "").strip()
    if not url or len(url) > 2048:
        raise EventValidationError(f"{field_name} is invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise EventValidationError(f"{field_name} contains control characters")
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise EventValidationError(f"{field_name} must be HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise EventValidationError(f"{field_name} must not contain credentials")
    return url


def infer_provider_key(join_url: object) -> str:
    url = validate_external_https_url(join_url, field_name="join_url")
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    for suffix, key in _PROVIDER_HOST_HINTS:
        if host == suffix or host.endswith("." + suffix):
            return key
    return "external"


def normalize_provider_key(value: object | None, *, join_url: object) -> str:
    raw = str(value or "").strip().lower()
    if not raw or raw == "auto":
        return infer_provider_key(join_url)
    normalized = _PROVIDER_ALIASES.get(raw, raw)
    normalized = normalized.replace(" ", "_")
    if not _PROVIDER_KEY_RE.fullmatch(normalized):
        raise EventValidationError("invalid provider_key")
    return normalized


def normalize_provider_label(value: object | None) -> str | None:
    label = _clean_text(value, field_name="provider_label", maximum=120, required=False)
    return label or None


def new_public_slug() -> str:
    return secrets.token_urlsafe(24)


def new_registration_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True, slots=True)
class Event:
    id: str
    business_id: str
    created_by_member_id: str
    kind: str
    status: str
    title: str
    description: str
    starts_at: datetime
    ends_at: datetime | None
    timezone_name: str
    provider_key: str
    provider_label: str | None
    join_url: str
    offer_url: str | None
    public_slug: str
    consent_version: str
    notification_connection_id: str | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", normalize_uuid(self.id, field_name="event_id"))
        object.__setattr__(
            self, "business_id", normalize_uuid(self.business_id, field_name="business_id")
        )
        object.__setattr__(
            self,
            "created_by_member_id",
            normalize_uuid(self.created_by_member_id, field_name="created_by_member_id"),
        )
        object.__setattr__(self, "kind", normalize_event_kind(self.kind))
        object.__setattr__(self, "status", normalize_event_status(self.status))
        object.__setattr__(self, "title", _clean_text(self.title, field_name="title", maximum=180))
        description = str(self.description or "").replace("\x00", "").strip()
        if len(description) > 12_000:
            raise EventValidationError("description is too long")
        object.__setattr__(self, "description", description)
        start = normalize_utc(self.starts_at, field_name="starts_at")
        end = None if self.ends_at is None else normalize_utc(self.ends_at, field_name="ends_at")
        if end is not None and end <= start:
            raise EventValidationError("event must end after it starts")
        object.__setattr__(self, "starts_at", start)
        object.__setattr__(self, "ends_at", end)
        object.__setattr__(self, "timezone_name", normalize_timezone_name(self.timezone_name))
        join_url = validate_external_https_url(self.join_url, field_name="join_url")
        object.__setattr__(self, "join_url", join_url)
        object.__setattr__(
            self,
            "provider_key",
            normalize_provider_key(self.provider_key, join_url=join_url),
        )
        object.__setattr__(self, "provider_label", normalize_provider_label(self.provider_label))
        object.__setattr__(
            self,
            "offer_url",
            None if not self.offer_url else validate_external_https_url(self.offer_url, field_name="offer_url"),
        )
        if not _PUBLIC_SLUG_RE.fullmatch(str(self.public_slug or "")):
            raise EventValidationError("invalid public_slug")
        consent_version = _clean_text(
            self.consent_version, field_name="consent_version", maximum=120
        )
        object.__setattr__(self, "consent_version", consent_version)
        if self.notification_connection_id is not None:
            object.__setattr__(
                self,
                "notification_connection_id",
                normalize_uuid(
                    self.notification_connection_id,
                    field_name="notification_connection_id",
                ),
            )
        object.__setattr__(self, "created_at", normalize_utc(self.created_at, field_name="created_at"))
        object.__setattr__(self, "updated_at", normalize_utc(self.updated_at, field_name="updated_at"))

    def accepts_registrations(self, *, now: datetime | None = None) -> bool:
        current = normalize_utc(now or datetime.now(timezone.utc), field_name="now")
        return self.status == "published" and self.starts_at > current

    def join_click_counts_for_funnel(self, *, now: datetime | None = None) -> bool:
        current = normalize_utc(now or datetime.now(timezone.utc), field_name="now")
        # A redirect click is only a show-up signal close to the event, never proof
        # of provider attendance. Provider webhooks may later confirm attendance.
        close_at = (self.ends_at or (self.starts_at + timedelta(hours=4))) + timedelta(hours=12)
        return self.starts_at - timedelta(minutes=45) <= current <= close_at

    def local_start_label(self) -> str:
        return self.starts_at.astimezone(ZoneInfo(self.timezone_name)).strftime("%d.%m.%Y %H:%M")


@dataclass(frozen=True, slots=True)
class PublicEvent:
    public_slug: str
    kind: str
    title: str
    description: str
    starts_at: datetime
    ends_at: datetime | None
    timezone_name: str
    provider_key: str
    provider_label: str | None

    def local_start_label(self) -> str:
        return self.starts_at.astimezone(ZoneInfo(self.timezone_name)).strftime("%d.%m.%Y %H:%M")


@dataclass(frozen=True, slots=True)
class EventRegistration:
    id: str
    event_id: str
    business_id: str
    customer_id: str | None
    status: str
    name: str
    email: str
    phone: str | None
    source: str | None
    campaign_ref: str | None
    token: str
    consent_version: str
    consented_at: datetime
    registered_at: datetime
    first_join_click_at: datetime | None = None
    attendance_confirmed_at: datetime | None = None
    attendance_source: str | None = None
    offer_clicked_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", normalize_uuid(self.id, field_name="registration_id"))
        object.__setattr__(self, "event_id", normalize_uuid(self.event_id, field_name="event_id"))
        object.__setattr__(self, "business_id", normalize_uuid(self.business_id, field_name="business_id"))
        if self.customer_id is not None:
            object.__setattr__(
                self,
                "customer_id",
                normalize_uuid(self.customer_id, field_name="customer_id"),
            )
        object.__setattr__(self, "status", normalize_registration_status(self.status))
        object.__setattr__(self, "name", _clean_text(self.name, field_name="name", maximum=120))
        object.__setattr__(self, "email", normalize_email(self.email))
        object.__setattr__(self, "phone", normalize_phone(self.phone))
        if not _CAPABILITY_TOKEN_RE.fullmatch(str(self.token or "")):
            raise EventValidationError("invalid registration token")
        object.__setattr__(self, "consent_version", _clean_text(self.consent_version, field_name="consent_version", maximum=120))
        object.__setattr__(self, "consented_at", normalize_utc(self.consented_at, field_name="consented_at"))
        object.__setattr__(self, "registered_at", normalize_utc(self.registered_at, field_name="registered_at"))
        for field in (
            "first_join_click_at",
            "attendance_confirmed_at",
            "offer_clicked_at",
        ):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, normalize_utc(value, field_name=field))
        if self.attendance_source is not None:
            object.__setattr__(
                self,
                "attendance_source",
                _clean_text(self.attendance_source, field_name="attendance_source", maximum=120),
            )


__all__ = [
    "Event",
    "EventRegistration",
    "EventUnavailable",
    "EventValidationError",
    "PublicEvent",
    "infer_provider_key",
    "new_public_slug",
    "new_registration_token",
    "normalize_email",
    "normalize_event_kind",
    "normalize_event_status",
    "normalize_phone",
    "normalize_provider_key",
    "normalize_provider_label",
    "normalize_registration_status",
    "normalize_timezone_name",
    "normalize_utc",
    "validate_external_https_url",
]
