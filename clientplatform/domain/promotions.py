from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from clientplatform.domain.tenancy import normalize_uuid


PUBLIC_PROMOTION_PREFIX = "cpa_"


class PromotionError(RuntimeError):
    """Base error for promotion creation, attribution and reporting."""


class PromotionNotFound(PromotionError):
    """The requested tenant-scoped promotion object does not exist."""


class PromotionInvariantViolation(PromotionError):
    """A promotion transition would violate its business invariants."""


class PromotionChannel(StrEnum):
    TELEGRAM = "telegram"
    VK = "vk"
    WHATSAPP = "whatsapp"
    WEBSITE = "website"
    OFFLINE = "offline"


class PromotionCampaignStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    CLOSED = "closed"


class PromotionEventType(StrEnum):
    OPENED = "opened"
    BOOKED = "booked"


@dataclass(frozen=True, slots=True)
class CreativeGuardrails:
    """Conservative advertising guardrails adapted from BusinesAIOS."""

    max_headline_len: int = 60
    max_primary_text_len: int = 420
    max_description_len: int = 100
    disallow_all_caps: bool = True
    disallow_excessive_punct: bool = True
    disallow_shaming_language: bool = True
    disallow_medical_claims: bool = True
    deny_phrases: tuple[str, ...] = field(
        default_factory=lambda: (
            "100% гарантия",
            "лучший в мире",
            "гарантированный результат",
        )
    )


@dataclass(frozen=True, slots=True)
class PromotionCreative:
    creative_id: str
    headline: str
    primary_text: str
    description: str
    cta: str = "Записаться"
    style: str = "direct"

    def __post_init__(self) -> None:
        creative_id = str(self.creative_id or "").strip()
        if not re.fullmatch(r"cr_[0-9a-f]{16}", creative_id):
            raise ValueError("creative_id must be a stable promotion identifier")
        object.__setattr__(self, "creative_id", creative_id)
        for field_name, maximum in (
            ("headline", 60),
            ("primary_text", 420),
            ("description", 100),
            ("cta", 40),
            ("style", 40),
        ):
            normalized = normalize_promotion_text(
                getattr(self, field_name),
                field_name=field_name,
                maximum=maximum,
            )
            object.__setattr__(self, field_name, normalized)


@dataclass(frozen=True, slots=True)
class PromotionCampaign:
    id: str
    business_id: str
    offering_id: str
    booking_slot_id: str
    channel: PromotionChannel
    source_token: str
    creative: PromotionCreative
    status: PromotionCampaignStatus
    created_by_member_id: str
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", normalize_uuid(self.id, field_name="campaign_id"))
        object.__setattr__(
            self,
            "business_id",
            normalize_uuid(self.business_id, field_name="business_id"),
        )
        object.__setattr__(
            self,
            "offering_id",
            normalize_uuid(self.offering_id, field_name="offering_id"),
        )
        object.__setattr__(
            self,
            "booking_slot_id",
            normalize_uuid(self.booking_slot_id, field_name="booking_slot_id"),
        )
        object.__setattr__(
            self,
            "created_by_member_id",
            normalize_uuid(self.created_by_member_id, field_name="created_by_member_id"),
        )
        object.__setattr__(self, "source_token", normalize_source_token(self.source_token))


@dataclass(frozen=True, slots=True)
class PromotionSourceAlias:
    source_token: str
    business_id: str
    campaign_id: str
    source_kind: str
    source_key: str
    status: str
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_token", normalize_source_token(self.source_token))
        object.__setattr__(
            self,
            "business_id",
            normalize_uuid(self.business_id, field_name="business_id"),
        )
        object.__setattr__(
            self,
            "campaign_id",
            normalize_uuid(self.campaign_id, field_name="campaign_id"),
        )
        object.__setattr__(self, "source_kind", normalize_source_kind(self.source_kind))
        object.__setattr__(self, "source_key", normalize_source_key(self.source_key))
        status = str(self.status or "").strip()
        if status not in {"active", "revoked"}:
            raise ValueError("promotion source alias status is invalid")
        object.__setattr__(self, "status", status)


@dataclass(frozen=True, slots=True)
class PromotionSourceResolution:
    campaign: PromotionCampaign
    attribution_token: str
    source_kind: str = "campaign"
    source_key: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "attribution_token",
            normalize_source_token(self.attribution_token),
        )


@dataclass(frozen=True, slots=True)
class PromotionStats:
    campaigns: int
    people_opened: int
    bookings: int

    @property
    def conversion_percent(self) -> float:
        if self.people_opened <= 0:
            return 0.0
        return round((self.bookings / self.people_opened) * 100.0, 1)


def normalize_promotion_text(value: object, *, field_name: str, maximum: int) -> str:
    raw = str(value or "").replace("\x00", " ")
    normalized = re.sub(r"\s+", " ", raw).strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} must be at most {maximum} characters")
    return normalized


def normalize_source_token(value: object) -> str:
    normalized = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{12,32}", normalized):
        raise ValueError("promotion source token is invalid")
    return normalized


def normalize_source_kind(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", normalized):
        raise ValueError("promotion source kind is invalid")
    return normalized


def normalize_source_key(value: object) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").replace("\x00", " ")).strip()
    if not normalized or len(normalized) > 300:
        raise ValueError("promotion source key is invalid")
    return normalized


def new_source_token() -> str:
    return normalize_source_token(secrets.token_urlsafe(12))


def rewrite_promotion_source_url(
    value: object,
    *,
    from_token: str,
    to_token: str,
) -> str:
    """Replace exactly one Telegram promotion start token in an HTTPS URL."""

    source = str(value or "").strip()
    parsed = urlsplit(source)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("advertising destination must be an HTTPS URL")
    expected = PUBLIC_PROMOTION_PREFIX + normalize_source_token(from_token)
    replacement = PUBLIC_PROMOTION_PREFIX + normalize_source_token(to_token)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    matches = sum(1 for key, item in pairs if key == "start" and item == expected)
    if matches != 1:
        raise PromotionInvariantViolation(
            "advertising destination is not bound to the expected promotion source"
        )
    rewritten = [
        (key, replacement if key == "start" and item == expected else item)
        for key, item in pairs
    ]
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(rewritten), parsed.fragment)
    )


def stable_creative_id(*parts: object) -> str:
    payload = "||".join(str(part or "").strip() for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return "cr_" + digest[:16]


_RE_EXCESSIVE_PUNCT = re.compile(r"([!?])\1\1+")


def _is_all_caps(value: str) -> bool:
    letters = [character for character in value if character.isalpha()]
    return bool(letters) and all(character.isupper() for character in letters)


def validate_creative(
    creative: PromotionCreative,
    guardrails: CreativeGuardrails | None = None,
) -> tuple[bool, str]:
    rules = guardrails or CreativeGuardrails()
    if len(creative.headline) > rules.max_headline_len:
        return False, "headline_too_long"
    if len(creative.primary_text) > rules.max_primary_text_len:
        return False, "primary_text_too_long"
    if len(creative.description) > rules.max_description_len:
        return False, "description_too_long"

    text = " ".join(
        (creative.headline, creative.primary_text, creative.description)
    ).strip()
    lowered = text.lower()
    if rules.disallow_all_caps and (
        _is_all_caps(creative.headline) or _is_all_caps(creative.primary_text)
    ):
        return False, "all_caps_disallowed"
    if rules.disallow_excessive_punct and _RE_EXCESSIVE_PUNCT.search(text):
        return False, "excessive_punctuation"
    if rules.disallow_shaming_language and any(
        phrase in lowered
        for phrase in (
            "стыдно",
            "позор",
            "ты виноват",
            "ты плохой",
            "ленивый",
            "слабак",
        )
    ):
        return False, "shaming_language"
    if rules.disallow_medical_claims and any(
        phrase in lowered
        for phrase in (
            "вылечим",
            "гарантированно вылечит",
            "исцеляет",
            "без побочных эффектов",
            "избавим навсегда",
        )
    ):
        return False, "medical_claims"
    if any(phrase.lower() in lowered for phrase in rules.deny_phrases):
        return False, "deny_phrase"
    return True, "ok"


__all__ = [
    "CreativeGuardrails",
    "PUBLIC_PROMOTION_PREFIX",
    "PromotionCampaign",
    "PromotionCampaignStatus",
    "PromotionChannel",
    "PromotionCreative",
    "PromotionError",
    "PromotionEventType",
    "PromotionInvariantViolation",
    "PromotionNotFound",
    "PromotionSourceAlias",
    "PromotionSourceResolution",
    "PromotionStats",
    "new_source_token",
    "normalize_source_key",
    "normalize_source_kind",
    "normalize_source_token",
    "rewrite_promotion_source_url",
    "stable_creative_id",
    "validate_creative",
]
