from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum

from clientplatform.domain.tenancy import normalize_uuid


class AdConnectionError(RuntimeError):
    """Base error for external advertising account operations."""


class AdConnectionNotFound(AdConnectionError):
    """A tenant-scoped advertising object does not exist."""


class AdConnectionInvariantViolation(AdConnectionError):
    """An advertising operation violates a required safety invariant."""


class AdProvider(StrEnum):
    YANDEX_DIRECT = "yandex_direct"


class AdConnectionStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    ATTENTION = "attention"
    DISABLED = "disabled"
    REVOKED = "revoked"


class AdPublicationStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    PUBLISHING = "publishing"
    RETRY = "retry"
    SUBMITTED = "submitted"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class AdConnection:
    id: str
    business_id: str
    provider: AdProvider
    external_account_id: str
    external_login: str
    status: AdConnectionStatus
    permissions: tuple[str, ...]
    created_by_member_id: str
    created_at: str
    updated_at: str
    last_success_at: str | None = None
    last_error_at: str | None = None
    last_error_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", normalize_uuid(self.id, field_name="ad_connection_id"))
        object.__setattr__(
            self,
            "business_id",
            normalize_uuid(self.business_id, field_name="business_id"),
        )
        object.__setattr__(
            self,
            "created_by_member_id",
            normalize_uuid(self.created_by_member_id, field_name="created_by_member_id"),
        )
        object.__setattr__(
            self,
            "external_account_id",
            normalize_external_account_id(self.external_account_id),
        )
        object.__setattr__(
            self,
            "external_login",
            normalize_external_login(self.external_login),
        )
        object.__setattr__(
            self,
            "permissions",
            tuple(sorted({normalize_permission(item) for item in self.permissions})),
        )


@dataclass(frozen=True, slots=True)
class AdOAuthSession:
    state_hash: str
    business_id: str
    user_id: int
    membership_id: str
    provider: AdProvider
    verifier_ciphertext: str
    expires_at: str
    created_at: str
    consumed_at: str | None = None


@dataclass(frozen=True, slots=True)
class AdPublicationJob:
    id: str
    business_id: str
    promotion_campaign_id: str
    connection_id: str
    external_campaign_id: str
    external_campaign_name: str
    region_ids: tuple[int, ...]
    source_url: str
    title: str
    text: str
    status: AdPublicationStatus
    idempotency_key: str
    attempts: int
    created_by_member_id: str
    created_at: str
    updated_at: str
    external_ad_group_id: str | None = None
    external_ad_id: str | None = None
    last_error_code: str | None = None
    submitted_at: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "id",
            "business_id",
            "promotion_campaign_id",
            "connection_id",
            "created_by_member_id",
        ):
            object.__setattr__(
                self,
                field_name,
                normalize_uuid(getattr(self, field_name), field_name=field_name),
            )
        object.__setattr__(
            self,
            "external_campaign_id",
            normalize_external_campaign_id(self.external_campaign_id),
        )
        object.__setattr__(self, "region_ids", normalize_region_ids(self.region_ids))
        if not re.fullmatch(r"adjob_[0-9a-f]{32}", self.idempotency_key):
            raise ValueError("ad publication idempotency key is invalid")
        if self.attempts < 0:
            raise ValueError("ad publication attempts must not be negative")


def normalize_external_account_id(value: object) -> str:
    normalized = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,160}", normalized):
        raise ValueError("external advertising account id is invalid")
    return normalized


def normalize_external_login(value: object) -> str:
    normalized = " ".join(str(value or "").replace("\x00", " ").split()).strip()
    if not normalized or len(normalized) > 160:
        raise ValueError("external advertising login is invalid")
    return normalized


def normalize_permission(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_.:-]{1,80}", normalized):
        raise ValueError("advertising permission is invalid")
    return normalized


def normalize_external_campaign_id(value: object) -> str:
    normalized = str(value or "").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,19}", normalized):
        raise ValueError("external campaign id is invalid")
    return normalized


_ADVERTISING_REGION_ALIASES = {
    "nn": 47,
    "нижний новгород": 47,
    "н. новгород": 47,
    "н новгород": 47,
    "moscow": 213,
    "москва": 213,
    "г. москва": 213,
    "г москва": 213,
    "spb": 2,
    "спб": 2,
    "санкт-петербург": 2,
    "санкт петербург": 2,
    "с.-петербург": 2,
    "с петербург": 2,
}


def _advertising_region_alias(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.strip().lower().replace("ё", "е").split())
    return _ADVERTISING_REGION_ALIASES.get(normalized)


def normalize_region_ids(values: object) -> tuple[int, ...]:
    """Normalize explicit Yandex region IDs or supported human region names.

    Provider-facing state remains a tuple of numeric RegionId values. Human-facing
    boundaries may also pass the canonical city aliases used by ClientPlatform's
    goal-first advertising flow. This keeps one normalization contract for manual
    and goal-first advertising instead of teaching individual Telegram handlers
    different meanings for the same region.
    """

    if isinstance(values, str):
        raw_items = [item.strip() for item in values.split(",")]
    else:
        try:
            raw_items = list(values)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ValueError("region ids must be a collection") from exc
    normalized: set[int] = set()
    for raw in raw_items:
        if raw in (None, ""):
            continue
        if isinstance(raw, bool):
            raise ValueError("region id must be a positive integer")
        alias = _advertising_region_alias(raw)
        if alias is not None:
            item = alias
        else:
            try:
                item = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("region id or supported region name is invalid") from exc
        if item <= 0 or item > 2_147_483_647:
            raise ValueError("region id must be a positive integer")
        normalized.add(item)
    if not normalized:
        raise ValueError("at least one explicit advertising region is required")
    if len(normalized) > 100:
        raise ValueError("too many advertising regions")
    return tuple(sorted(normalized))


def new_oauth_state() -> str:
    import secrets

    return secrets.token_urlsafe(32)


def new_pkce_verifier() -> str:
    import secrets

    return secrets.token_urlsafe(64)


def oauth_state_hash(state: object) -> str:
    normalized = str(state or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,160}", normalized):
        raise ValueError("OAuth state is invalid")
    return hashlib.sha256(normalized.encode("ascii")).hexdigest()


def pkce_challenge(verifier: object) -> str:
    normalized = str(verifier or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,160}", normalized):
        raise ValueError("PKCE verifier is invalid")
    digest = hashlib.sha256(normalized.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def publication_idempotency_key(
    *,
    business_id: str,
    promotion_campaign_id: str,
    connection_id: str,
    external_campaign_id: str,
    region_ids: tuple[int, ...],
    creative_id: str,
) -> str:
    payload = "|".join(
        (
            normalize_uuid(business_id, field_name="business_id"),
            normalize_uuid(promotion_campaign_id, field_name="promotion_campaign_id"),
            normalize_uuid(connection_id, field_name="connection_id"),
            normalize_external_campaign_id(external_campaign_id),
            ",".join(str(item) for item in normalize_region_ids(region_ids)),
            str(creative_id or "").strip(),
        )
    )
    return "adjob_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


__all__ = [
    "AdConnection",
    "AdConnectionError",
    "AdConnectionInvariantViolation",
    "AdConnectionNotFound",
    "AdConnectionStatus",
    "AdOAuthSession",
    "AdProvider",
    "AdPublicationJob",
    "AdPublicationStatus",
    "new_oauth_state",
    "new_pkce_verifier",
    "normalize_external_account_id",
    "normalize_external_campaign_id",
    "normalize_external_login",
    "normalize_region_ids",
    "oauth_state_hash",
    "pkce_challenge",
    "publication_idempotency_key",
]
