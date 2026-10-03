from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum


_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")

from clientplatform.domain.visual_typography import (
    VISUAL_TYPOGRAPHY_LABELS_RU,
    VISUAL_TYPOGRAPHY_PRESETS,
    normalize_visual_typography_preset,
)

EDITABLE_AD_FONT_PRESETS = VISUAL_TYPOGRAPHY_PRESETS
EDITABLE_AD_FONT_LABELS_RU = VISUAL_TYPOGRAPHY_LABELS_RU


def normalize_editable_ad_font_preset(value: object) -> str:
    try:
        return normalize_visual_typography_preset(value)
    except ValueError as exc:
        raise ValueError("editable_ad_font_preset_invalid") from exc



class EditableAdProjectStatus(StrEnum):
    DRAFT = "draft"
    SOURCE_READY = "source_ready"
    SOURCE_EXPIRED = "source_expired"
    FINISHED = "finished"


@dataclass(frozen=True, slots=True)
class EditableAdProject:
    """Durable composition metadata for one editable advertising visual.

    Media bytes never belong to this aggregate. source_job_id points at the
    existing transient visual-provider job; editable copy/layout can outlive
    those bytes and request a new source only after fresh owner confirmation.
    """

    id: str
    business_id: str
    created_by_member_id: str
    publication_job_id: str
    kind: str
    headline: str
    body: str
    cta: str
    layout: str
    font_preset: str
    brand_json: str
    source_job_id: str
    status: EditableAdProjectStatus
    revision: int
    created_at: str
    updated_at: str

    def brand(self) -> dict[str, str]:
        try:
            value = json.loads(self.brand_json)
        except json.JSONDecodeError as exc:
            raise ValueError("editable_ad_brand_invalid") from exc
        if not isinstance(value, dict):
            raise ValueError("editable_ad_brand_invalid")
        allowed = ("primary_color", "accent_color", "text_color")
        if set(value) != set(allowed):
            raise ValueError("editable_ad_brand_invalid")
        normalized: dict[str, str] = {}
        for key in allowed:
            token = str(value.get(key) or "").strip().upper()
            if _COLOR_RE.fullmatch(token) is None:
                raise ValueError("editable_ad_brand_invalid")
            normalized[key] = token
        return normalized

    def composition(self) -> dict[str, object]:
        if self.kind not in {"image", "video"}:
            raise ValueError("editable_ad_kind_invalid")
        if self.layout not in {"lower_card", "top_card"}:
            raise ValueError("editable_ad_layout_invalid")
        if self.revision < 1:
            raise ValueError("editable_ad_revision_invalid")
        font_preset = normalize_editable_ad_font_preset(self.font_preset)
        return {
            "headline": self.headline,
            "body": self.body,
            "cta": self.cta,
            "layout": self.layout,
            "typography": {"preset": font_preset},
            "brand": self.brand(),
        }


__all__ = [
    "EDITABLE_AD_FONT_LABELS_RU",
    "EDITABLE_AD_FONT_PRESETS",
    "EditableAdProject",
    "EditableAdProjectStatus",
    "normalize_editable_ad_font_preset",
]
