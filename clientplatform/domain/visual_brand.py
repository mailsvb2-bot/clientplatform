from __future__ import annotations

import re
from dataclasses import dataclass

_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")


def _clean(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def _list(values: object, *, maximum: int, item_limit: int) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        return ()
    out: list[str] = []
    for value in values:
        token = _clean(value, item_limit)
        if token and token.casefold() not in {item.casefold() for item in out}:
            out.append(token)
        if len(out) >= maximum:
            break
    return tuple(out)


def _color(value: object, default: str) -> str:
    token = str(value or default).strip().upper()
    if not _COLOR_RE.fullmatch(token):
        raise ValueError("invalid_clientplatform_brand_color")
    return token


@dataclass(frozen=True, slots=True)
class TenantBrandDNA:
    business_id: str
    display_name: str = ""
    tone: tuple[str, ...] = ("trustworthy", "clear", "human")
    visual_keywords: tuple[str, ...] = ()
    forbidden_visuals: tuple[str, ...] = (
        "fake awards",
        "fake reviews",
        "before/after transformation",
        "invented statistics",
    )
    primary_color: str = "#172033"
    accent_color: str = "#E9C46A"
    text_color: str = "#FFFFFF"

    def normalized(self) -> "TenantBrandDNA":
        business_id = _clean(self.business_id, 160)
        if not business_id:
            raise ValueError("business_id_required")
        return TenantBrandDNA(
            business_id=business_id,
            display_name=_clean(self.display_name, 120),
            tone=_list(self.tone, maximum=8, item_limit=80),
            visual_keywords=_list(self.visual_keywords, maximum=12, item_limit=100),
            forbidden_visuals=_list(self.forbidden_visuals, maximum=16, item_limit=120),
            primary_color=_color(self.primary_color, "#172033"),
            accent_color=_color(self.accent_color, "#E9C46A"),
            text_color=_color(self.text_color, "#FFFFFF"),
        )

    def assert_business(self, business_id: str) -> None:
        if self.normalized().business_id != _clean(business_id, 160):
            raise ValueError("creative_brand_business_mismatch")

    def prompt_context(self) -> str:
        value = self.normalized()
        parts = ["ClientPlatform business brand."]
        if value.display_name:
            parts.append(f"Brand name: {value.display_name}.")
        if value.tone:
            parts.append("Tone: " + ", ".join(value.tone) + ".")
        if value.visual_keywords:
            parts.append("Visual language: " + ", ".join(value.visual_keywords) + ".")
        if value.forbidden_visuals:
            parts.append("Never show: " + ", ".join(value.forbidden_visuals) + ".")
        return " ".join(parts)[:2500]

    def render_brand(self) -> dict[str, str]:
        value = self.normalized()
        return {
            "primary_color": value.primary_color,
            "accent_color": value.accent_color,
            "text_color": value.text_color,
        }


__all__ = ["TenantBrandDNA"]
