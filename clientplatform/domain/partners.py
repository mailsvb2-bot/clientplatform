from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit
from uuid import UUID

from clientplatform.domain.tenancy import normalize_uuid


class PartnerGrowthError(RuntimeError):
    """Base error for the partner-acquisition bounded context."""


class PartnerNotFound(PartnerGrowthError):
    pass


class PartnerInvariantViolation(PartnerGrowthError):
    pass


class PartnerAutomationMode(StrEnum):
    CAUTIOUS = "cautious"
    NORMAL = "normal"
    AUTOPILOT = "autopilot"


class PartnerCampaignStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class PartnerChannel(StrEnum):
    EMAIL = "email"
    TELEGRAM = "telegram"
    VK = "vk"
    WEBSITE_FORM = "website_form"
    MANUAL = "manual"


class ContactBasis(StrEnum):
    """Why ClientPlatform is allowed to initiate this contact.

    Public business contact data is useful for preparing a proposal, but it is
    deliberately not automatic-send authority. Only an existing relationship or
    explicit opt-in permits an automated first contact.
    """

    PUBLIC_BUSINESS_CONTACT = "public_business_contact"
    EXISTING_RELATIONSHIP = "existing_relationship"
    OPTED_IN = "opted_in"
    UNKNOWN = "unknown"
    NONE = "none"

    @property
    def permits_first_contact(self) -> bool:
        return self in {
            ContactBasis.EXISTING_RELATIONSHIP,
            ContactBasis.OPTED_IN,
        }


class PartnerCandidateStatus(StrEnum):
    DISCOVERED = "discovered"
    READY = "ready"
    CONTACTED = "contacted"
    REPLIED = "replied"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    PAID_ONLY = "paid_only"
    DO_NOT_CONTACT = "do_not_contact"
    INVALID = "invalid"


class PlacementKind(StrEnum):
    POST = "post"
    JOINT_LIVE = "joint_live"
    GUEST_ARTICLE = "guest_article"
    NEWSLETTER = "newsletter"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class PartnerCampaignGoal:
    target_count: int = 100
    deadline: str = ""
    budget_minor: int = 0
    objective: str = "new_customers"
    event_title: str = ""
    target_url: str = ""
    audience_terms: tuple[str, ...] = ()
    offer_summary: str = ""
    constraints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.target_count, bool) or int(self.target_count) < 1:
            raise ValueError("target_count must be a positive integer")
        if int(self.target_count) > 1_000_000:
            raise ValueError("target_count is unreasonably large")
        if isinstance(self.budget_minor, bool) or int(self.budget_minor) < 0:
            raise ValueError("budget_minor must be a non-negative integer")
        object.__setattr__(self, "target_count", int(self.target_count))
        object.__setattr__(self, "budget_minor", int(self.budget_minor))
        object.__setattr__(self, "objective", _text(self.objective, "objective", 160))
        object.__setattr__(self, "event_title", _optional_text(self.event_title, 240))
        object.__setattr__(self, "target_url", _optional_https_url(self.target_url))
        object.__setattr__(self, "offer_summary", _optional_text(self.offer_summary, 2000))
        object.__setattr__(self, "deadline", _optional_text(self.deadline, 80))
        object.__setattr__(
            self,
            "audience_terms",
            _tokens(self.audience_terms, maximum=48, item_maximum=100),
        )
        object.__setattr__(
            self,
            "constraints",
            _tokens(self.constraints, maximum=64, item_maximum=240),
        )


@dataclass(frozen=True, slots=True)
class PartnerCampaign:
    id: str
    business_id: str
    name: str
    goal: PartnerCampaignGoal
    automation_mode: PartnerAutomationMode
    status: PartnerCampaignStatus
    created_by_member_id: str
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", normalize_uuid(self.id, field_name="partner_campaign_id"))
        object.__setattr__(self, "business_id", normalize_uuid(self.business_id, field_name="business_id"))
        object.__setattr__(self, "created_by_member_id", normalize_uuid(self.created_by_member_id, field_name="created_by_member_id"))
        object.__setattr__(self, "name", _text(self.name, "name", 200))
        object.__setattr__(self, "automation_mode", PartnerAutomationMode(str(self.automation_mode)))
        object.__setattr__(self, "status", PartnerCampaignStatus(str(self.status)))


@dataclass(frozen=True, slots=True)
class PartnerCandidate:
    id: str
    business_id: str
    campaign_id: str
    name: str
    source_url: str
    audience_summary: str
    recent_topic: str
    channel: PartnerChannel
    contact_value: str
    contact_basis: ContactBasis
    follower_count: int | None = None
    tags: tuple[str, ...] = ()
    competitor: bool = False
    referral_token: str = ""
    status: PartnerCandidateStatus = PartnerCandidateStatus.DISCOVERED
    discovered_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", normalize_uuid(self.id, field_name="partner_candidate_id"))
        object.__setattr__(self, "business_id", normalize_uuid(self.business_id, field_name="business_id"))
        object.__setattr__(self, "campaign_id", normalize_uuid(self.campaign_id, field_name="partner_campaign_id"))
        object.__setattr__(self, "name", _text(self.name, "name", 240))
        object.__setattr__(self, "source_url", _optional_https_url(self.source_url))
        object.__setattr__(self, "audience_summary", _optional_text(self.audience_summary, 3000))
        object.__setattr__(self, "recent_topic", _optional_text(self.recent_topic, 1000))
        object.__setattr__(self, "channel", PartnerChannel(str(self.channel)))
        object.__setattr__(self, "contact_value", _optional_text(self.contact_value, 500))
        object.__setattr__(self, "contact_basis", ContactBasis(str(self.contact_basis)))
        object.__setattr__(self, "status", PartnerCandidateStatus(str(self.status)))
        object.__setattr__(self, "referral_token", _optional_text(self.referral_token, 128))
        object.__setattr__(self, "tags", _tokens(self.tags, maximum=64, item_maximum=100))
        if self.follower_count is not None:
            if isinstance(self.follower_count, bool) or int(self.follower_count) < 0:
                raise ValueError("follower_count must be non-negative")
            object.__setattr__(self, "follower_count", int(self.follower_count))

    @property
    def first_contact_permitted(self) -> bool:
        return (
            not self.competitor
            and self.status not in {
                PartnerCandidateStatus.DECLINED,
                PartnerCandidateStatus.DO_NOT_CONTACT,
                PartnerCandidateStatus.INVALID,
            }
            and self.contact_basis.permits_first_contact
            and bool(self.contact_value)
        )


@dataclass(frozen=True, slots=True)
class PartnerFitScore:
    candidate_id: str
    total: float
    relevance: float
    audience_quality: float
    contactability: float
    collaboration_fit: float
    risk_penalty: float
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", normalize_uuid(self.candidate_id, field_name="partner_candidate_id"))
        for name in (
            "total",
            "relevance",
            "audience_quality",
            "contactability",
            "collaboration_fit",
            "risk_penalty",
        ):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 100.0:
                raise ValueError(f"{name} must be between 0 and 100")
            object.__setattr__(self, name, round(value, 2))
        object.__setattr__(self, "reasons", _tokens(self.reasons, maximum=32, item_maximum=240))


@dataclass(frozen=True, slots=True)
class PartnerContentPack:
    candidate_id: str
    subject: str
    outreach_message: str
    ready_post: str
    followup_message: str
    collaboration_angle: str
    cta: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", normalize_uuid(self.candidate_id, field_name="partner_candidate_id"))
        object.__setattr__(self, "subject", _text(self.subject, "subject", 180))
        object.__setattr__(self, "outreach_message", _text(self.outreach_message, "outreach_message", 4000))
        object.__setattr__(self, "ready_post", _text(self.ready_post, "ready_post", 5000))
        object.__setattr__(self, "followup_message", _text(self.followup_message, "followup_message", 2000))
        object.__setattr__(self, "collaboration_angle", _text(self.collaboration_angle, "collaboration_angle", 500))
        object.__setattr__(self, "cta", _text(self.cta, "cta", 300))


@dataclass(frozen=True, slots=True)
class PartnerCampaignStats:
    campaigns: int = 0
    candidates: int = 0
    ready: int = 0
    contacted: int = 0
    replies: int = 0
    accepted: int = 0
    placements: int = 0
    attributed_visits: int = 0
    attributed_results: int = 0

    @property
    def reply_rate_percent(self) -> float:
        return 0.0 if self.contacted <= 0 else round(self.replies / self.contacted * 100.0, 1)

    @property
    def acceptance_rate_percent(self) -> float:
        return 0.0 if self.contacted <= 0 else round(self.accepted / self.contacted * 100.0, 1)


_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def normalize_public_contact(channel: PartnerChannel | str, value: object) -> str:
    selected = channel if isinstance(channel, PartnerChannel) else PartnerChannel(str(channel))
    text = _optional_text(value, 500)
    if not text:
        return ""
    if selected == PartnerChannel.EMAIL:
        lowered = text.casefold()
        if not _EMAIL_RE.fullmatch(lowered):
            raise ValueError("public partner email is invalid")
        return lowered
    if selected in {PartnerChannel.TELEGRAM, PartnerChannel.VK, PartnerChannel.WEBSITE_FORM}:
        if text.startswith("https://"):
            return _optional_https_url(text)
    return text


def partner_source_fingerprint(*, campaign_id: str, source_url: str, name: str) -> str:
    campaign = normalize_uuid(campaign_id, field_name="partner_campaign_id")
    normalized_url = _optional_https_url(source_url)
    normalized_name = " ".join(str(name or "").split()).casefold()
    digest = hashlib.sha256(f"{campaign}|{normalized_url}|{normalized_name}".encode("utf-8")).hexdigest()
    return "partner_" + digest[:32]


def is_valid_uuid(value: object) -> bool:
    try:
        UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def _text(value: object, field_name: str, maximum: int) -> str:
    normalized = " ".join(str(value or "").replace("\x00", " ").split()).strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} must be at most {maximum} characters")
    return normalized


def _optional_text(value: object, maximum: int) -> str:
    normalized = " ".join(str(value or "").replace("\x00", " ").split()).strip()
    if len(normalized) > maximum:
        normalized = normalized[:maximum].rstrip()
    return normalized


def _optional_https_url(value: object) -> str:
    text = _optional_text(value, 2048)
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("partner URL must be a public HTTPS URL")
    return text


def _tokens(values: object, *, maximum: int, item_maximum: int) -> tuple[str, ...]:
    if isinstance(values, str):
        raw = [item.strip() for item in values.split(",")]
    else:
        try:
            raw = list(values or ())
        except TypeError as exc:
            raise ValueError("token collection is invalid") from exc
    out: list[str] = []
    seen: set[str] = set()
    for value in raw:
        token = _optional_text(value, item_maximum)
        folded = token.casefold()
        if token and folded not in seen:
            seen.add(folded)
            out.append(token)
        if len(out) >= maximum:
            break
    return tuple(out)


__all__ = [
    "ContactBasis",
    "PartnerAutomationMode",
    "PartnerCampaign",
    "PartnerCampaignGoal",
    "PartnerCampaignStats",
    "PartnerCampaignStatus",
    "PartnerCandidate",
    "PartnerCandidateStatus",
    "PartnerChannel",
    "PartnerContentPack",
    "PartnerFitScore",
    "PartnerGrowthError",
    "PartnerInvariantViolation",
    "PartnerNotFound",
    "PlacementKind",
    "normalize_public_contact",
    "partner_source_fingerprint",
]
