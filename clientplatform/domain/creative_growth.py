from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class CreativeTrialStatus(StrEnum):
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"


class CreativeAttributionScope(StrEnum):
    VARIANT = "variant"
    SHARED_CAMPAIGN = "shared_campaign"
    UNAVAILABLE = "unavailable"


def _uuid(value: str, *, field_name: str) -> str:
    try:
        return str(UUID(str(value or "").strip()))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a valid UUID") from exc


def _token(value: str, *, field_name: str, maximum: int = 200) -> str:
    token = re.sub(r"\s+", " ", str(value or "").replace("\x00", " ")).strip()
    if not token:
        raise ValueError(f"{field_name} must not be empty")
    if len(token) > maximum:
        raise ValueError(f"{field_name} must be at most {maximum} characters")
    return token


@dataclass(frozen=True, slots=True)
class CreativeTrialArm:
    variant_id: str
    publication_job_id: str
    allocation_bps: int
    promotion_campaign_id: str = ""

    def normalized(self) -> "CreativeTrialArm":
        allocation = int(self.allocation_bps)
        if allocation <= 0 or allocation > 10_000:
            raise ValueError("creative allocation must be between 1 and 10000 basis points")
        campaign = str(self.promotion_campaign_id or "").strip()
        if campaign:
            campaign = _uuid(campaign, field_name="promotion_campaign_id")
        return CreativeTrialArm(
            variant_id=_token(self.variant_id, field_name="variant_id"),
            publication_job_id=_uuid(
                self.publication_job_id,
                field_name="publication_job_id",
            ),
            allocation_bps=allocation,
            promotion_campaign_id=campaign,
        )


@dataclass(frozen=True, slots=True)
class CreativeTrafficPlan:
    trial_id: str
    business_id: str
    status: CreativeTrialStatus
    revision: int
    arms: tuple[CreativeTrialArm, ...]

    def normalized(self) -> "CreativeTrafficPlan":
        revision = int(self.revision)
        if revision < 1:
            raise ValueError("creative trial revision must be positive")
        arms = tuple(arm.normalized() for arm in self.arms)
        if len(arms) < 2 or len(arms) > 8:
            raise ValueError("creative trial must contain between 2 and 8 variants")
        if len({arm.variant_id for arm in arms}) != len(arms):
            raise ValueError("creative trial variants must be unique")
        if len({arm.publication_job_id for arm in arms}) != len(arms):
            raise ValueError("creative trial publication jobs must be unique")
        if sum(arm.allocation_bps for arm in arms) != 10_000:
            raise ValueError("creative trial allocations must total 10000 basis points")
        return CreativeTrafficPlan(
            trial_id=_uuid(self.trial_id, field_name="trial_id"),
            business_id=_uuid(self.business_id, field_name="business_id"),
            status=CreativeTrialStatus(self.status),
            revision=revision,
            arms=arms,
        )

    def assign(self, subject_key: str) -> CreativeTrialArm:
        plan = self.normalized()
        if plan.status != CreativeTrialStatus.RUNNING:
            raise ValueError("creative trial is not running")
        subject = _token(subject_key, field_name="subject_key", maximum=500)
        digest = hashlib.sha256(
            f"{plan.trial_id}:{plan.revision}:{subject}".encode("utf-8")
        ).digest()
        bucket = int.from_bytes(digest[:8], "big") % 10_000
        cursor = 0
        for arm in plan.arms:
            cursor += arm.allocation_bps
            if bucket < cursor:
                return arm
        raise RuntimeError("creative trial allocation invariant failed")


@dataclass(frozen=True, slots=True)
class CreativeVariantOutcome:
    variant_id: str
    publication_job_id: str
    promotion_campaign_id: str
    attribution_scope: CreativeAttributionScope
    leads: int = 0
    bookings: int = 0
    won: int = 0

    @property
    def booking_rate(self) -> float | None:
        return (self.bookings / self.leads) if self.leads else None

    @property
    def win_rate(self) -> float | None:
        return (self.won / self.leads) if self.leads else None


@dataclass(frozen=True, slots=True)
class CreativeTrialOutcomeSnapshot:
    trial_id: str
    date_from: str
    date_to: str
    variants: tuple[CreativeVariantOutcome, ...]

    @property
    def has_variant_level_downstream_attribution(self) -> bool:
        return bool(self.variants) and all(
            item.attribution_scope == CreativeAttributionScope.VARIANT
            for item in self.variants
        )


__all__ = [
    "CreativeAttributionScope",
    "CreativeTrafficPlan",
    "CreativeTrialArm",
    "CreativeTrialOutcomeSnapshot",
    "CreativeTrialStatus",
    "CreativeVariantOutcome",
]
