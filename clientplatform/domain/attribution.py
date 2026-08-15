from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping


class AcquisitionSource(StrEnum):
    ORGANIC = "organic"
    REFERRAL = "referral"
    TELEGRAM = "telegram"
    VK = "vk"
    MAX = "max"
    WEBSITE = "website"
    YANDEX_DIRECT = "yandex_direct"
    PARTNER = "partner"
    MANUAL_IMPORT = "manual_import"
    UNKNOWN = "unknown"


class AttributionModelVersion(StrEnum):
    FIRST_TOUCH_V1 = "first_touch_v1"


class AttributionInvariantViolation(ValueError):
    """A mutation would silently change established acquisition provenance."""


@dataclass(frozen=True, slots=True)
class AttributionIdentity:
    """Opaque first-party acquisition identity; raw external tokens are never stored."""

    id: str
    business_id: str
    source: AcquisitionSource
    identity_kind: str
    identity_fingerprint: str
    source_ref_type: str
    source_ref_id: str
    promotion_campaign_id: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "id",
            "business_id",
            "identity_kind",
            "identity_fingerprint",
            "source_ref_type",
            "source_ref_id",
        ):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"{field_name} must not be empty")
            object.__setattr__(self, field_name, value)
        if not isinstance(self.source, AcquisitionSource):
            object.__setattr__(self, "source", AcquisitionSource(str(self.source)))
        fingerprint = self.identity_fingerprint.lower()
        if len(fingerprint) != 64 or any(character not in "0123456789abcdef" for character in fingerprint):
            raise ValueError("identity_fingerprint must be a SHA-256 hex digest")
        object.__setattr__(self, "identity_fingerprint", fingerprint)
        if self.promotion_campaign_id is not None:
            campaign_id = str(self.promotion_campaign_id).strip()
            if not campaign_id:
                raise ValueError("promotion_campaign_id must not be blank")
            object.__setattr__(self, "promotion_campaign_id", campaign_id)
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class AcquisitionTouch:
    """Durable evidence that one customer arrived through one acquisition identity."""

    id: str
    business_id: str
    attribution_identity_id: str
    customer_id: str
    source: AcquisitionSource
    occurred_at: datetime
    metadata: Mapping[str, Any]
    metadata_version: int
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("id", "business_id", "attribution_identity_id", "customer_id"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"{field_name} must not be empty")
            object.__setattr__(self, field_name, value)
        if not isinstance(self.source, AcquisitionSource):
            object.__setattr__(self, "source", AcquisitionSource(str(self.source)))
        if self.occurred_at.tzinfo is None or self.created_at.tzinfo is None:
            raise ValueError("acquisition touch timestamps must be timezone-aware")
        if isinstance(self.metadata_version, bool) or int(self.metadata_version) < 1:
            raise ValueError("metadata_version must be a positive integer")
        object.__setattr__(self, "metadata_version", int(self.metadata_version))


@dataclass(frozen=True, slots=True)
class AttributionLink:
    """Immutable first-touch link from a touch to a customer or booked slot."""

    id: str
    business_id: str
    touch_id: str
    customer_id: str | None
    booking_slot_id: str | None
    model_version: AttributionModelVersion
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("id", "business_id", "touch_id"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"{field_name} must not be empty")
            object.__setattr__(self, field_name, value)
        has_customer = self.customer_id is not None
        has_booking = self.booking_slot_id is not None
        if has_customer == has_booking:
            raise ValueError("attribution link must target exactly one subject")
        if self.customer_id is not None:
            customer_id = str(self.customer_id).strip()
            if not customer_id:
                raise ValueError("customer_id must not be blank")
            object.__setattr__(self, "customer_id", customer_id)
        if self.booking_slot_id is not None:
            booking_slot_id = str(self.booking_slot_id).strip()
            if not booking_slot_id:
                raise ValueError("booking_slot_id must not be blank")
            object.__setattr__(self, "booking_slot_id", booking_slot_id)
        if not isinstance(self.model_version, AttributionModelVersion):
            object.__setattr__(
                self,
                "model_version",
                AttributionModelVersion(str(self.model_version)),
            )
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")

    @property
    def subject_type(self) -> str:
        return "customer" if self.customer_id is not None else "booking_slot"

    @property
    def subject_id(self) -> str:
        value = self.customer_id if self.customer_id is not None else self.booking_slot_id
        if value is None:
            raise RuntimeError("attribution link subject is missing")
        return value


@dataclass(frozen=True, slots=True)
class AttributionTrace:
    identity: AttributionIdentity
    touch: AcquisitionTouch
    link: AttributionLink
