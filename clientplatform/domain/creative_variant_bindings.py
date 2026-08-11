from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:@/-]{1,200}")
_COUNTRY_RE = re.compile(r"[A-Z]{2}")
_JOB_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_RENDER_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


class CreativeVariantBindingStatus(StrEnum):
    SELECTED = "selected"
    GENERATING = "generating"
    RENDERING = "rendering"
    ATTACHED = "attached"
    FAILED = "failed"


def _token(value: object, *, field: str) -> str:
    token = str(value or "").strip()
    if _TOKEN_RE.fullmatch(token) is None:
        raise ValueError(f"invalid_{field}")
    return token


@dataclass(frozen=True, slots=True)
class CreativeVariantBinding:
    publication_job_id: str
    business_id: str
    experiment_id: str
    variant_id: str
    angle_id: str
    country_code: str
    copy_digest: str
    source_job_id: str
    render_pack_id: str
    status: CreativeVariantBindingStatus
    created_by_member_id: str
    created_at: str
    updated_at: str

    def normalized(self) -> "CreativeVariantBinding":
        country = str(self.country_code or "").strip().upper()
        if country and _COUNTRY_RE.fullmatch(country) is None:
            raise ValueError("invalid_creative_country_code")
        copy_digest = str(self.copy_digest or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", copy_digest) is None:
            raise ValueError("invalid_creative_copy_digest")
        source = str(self.source_job_id or "").strip()
        render = str(self.render_pack_id or "").strip()
        if source and _JOB_RE.fullmatch(source) is None:
            raise ValueError("invalid_creative_source_job_id")
        if render and _RENDER_RE.fullmatch(render) is None:
            raise ValueError("invalid_creative_render_pack_id")
        return CreativeVariantBinding(
            publication_job_id=str(self.publication_job_id or "").strip(),
            business_id=str(self.business_id or "").strip(),
            experiment_id=_token(self.experiment_id, field="experiment_id"),
            variant_id=_token(self.variant_id, field="variant_id"),
            angle_id=_token(self.angle_id, field="angle_id"),
            country_code=country,
            copy_digest=copy_digest,
            source_job_id=source,
            render_pack_id=render,
            status=CreativeVariantBindingStatus(self.status),
            created_by_member_id=str(self.created_by_member_id or "").strip(),
            created_at=str(self.created_at or "").strip(),
            updated_at=str(self.updated_at or "").strip(),
        )


@dataclass(frozen=True, slots=True)
class ObservableCreativeVariant:
    binding: CreativeVariantBinding
    external_ad_id: str
    promotion_campaign_id: str


__all__ = [
    "CreativeVariantBinding",
    "CreativeVariantBindingStatus",
    "ObservableCreativeVariant",
]
