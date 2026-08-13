from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum

from clientplatform.domain.ad_connections import AdProvider, normalize_external_campaign_id
from clientplatform.domain.tenancy import normalize_uuid


class ManagedAdCampaignStatus(StrEnum):
    PROVISIONING = "provisioning"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ManagedAdCampaign:
    id: str
    business_id: str
    promotion_campaign_id: str
    connection_id: str
    provider: AdProvider
    provisioning_key: str
    external_campaign_name: str
    status: ManagedAdCampaignStatus
    created_by_member_id: str
    created_at: str
    updated_at: str
    external_campaign_id: str | None = None
    last_error_code: str | None = None

    def __post_init__(self) -> None:
        for name in ("id", "business_id", "promotion_campaign_id", "connection_id", "created_by_member_id"):
            object.__setattr__(self, name, normalize_uuid(getattr(self, name), field_name=name))
        object.__setattr__(self, "provisioning_key", normalize_managed_campaign_key(self.provisioning_key))
        object.__setattr__(self, "external_campaign_name", normalize_managed_campaign_name(self.external_campaign_name))
        if self.external_campaign_id not in (None, ""):
            object.__setattr__(self, "external_campaign_id", normalize_external_campaign_id(self.external_campaign_id))
        elif self.status == ManagedAdCampaignStatus.READY:
            raise ValueError("ready managed campaign requires an external campaign id")


def managed_campaign_provisioning_key(*, business_id: str, promotion_campaign_id: str, connection_id: str) -> str:
    payload = "|".join((
        normalize_uuid(business_id, field_name="business_id"),
        normalize_uuid(promotion_campaign_id, field_name="promotion_campaign_id"),
        normalize_uuid(connection_id, field_name="connection_id"),
    ))
    return "cpmc_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def normalize_managed_campaign_key(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"cpmc_[0-9a-f]{32}", normalized):
        raise ValueError("managed campaign provisioning key is invalid")
    return normalized


def managed_campaign_name(provisioning_key: str) -> str:
    return f"ClientPlatform · {normalize_managed_campaign_key(provisioning_key)}"


def normalize_managed_campaign_name(value: object) -> str:
    normalized = " ".join(str(value or "").split())
    if not normalized.startswith("ClientPlatform · cpmc_") or len(normalized) > 255:
        raise ValueError("managed campaign name is invalid")
    return normalized


def normalize_managed_campaign_error(value: object) -> str:
    normalized = "_".join(str(value or "provider_error").strip().lower().split())
    filtered = "".join(ch for ch in normalized if ch.isalnum() or ch in "_.-")
    return (filtered or "provider_error")[:120]


__all__ = [
    "ManagedAdCampaign", "ManagedAdCampaignStatus", "managed_campaign_name",
    "managed_campaign_provisioning_key", "normalize_managed_campaign_error",
    "normalize_managed_campaign_key", "normalize_managed_campaign_name",
]
