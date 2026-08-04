from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from enum import StrEnum

from clientplatform.domain.tenancy import normalize_uuid


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


def new_source_token() -> str:
    return normalize_source_token(secrets.token_urlsafe(12))


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
    "PromotionCampaign",
    "PromotionCampaignStatus",
    "PromotionChannel",
    "PromotionCreative",
    "PromotionError",
    "PromotionEventType",
    "PromotionInvariantViolation",
    "PromotionNotFound",
    "PromotionStats",
    "new_source_token",
    "normalize_source_token",
    "stable_creative_id",
    "validate_creative",
]
