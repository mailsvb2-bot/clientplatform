from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from clientplatform.domain.activity import ActivityNotFound
from clientplatform.domain.tenancy import TenantContext
from clientplatform.domain.visual_brand import TenantBrandDNA
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _tuple_json(value: Any, *, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_clientplatform_brand_profile") from exc
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError("invalid_clientplatform_brand_profile")
    return tuple(parsed)


def _encoded(values: tuple[str, ...]) -> str:
    return json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))


class VisualBrandRepository:
    """Persist Visual Creative Studio DNA inside the canonical business profile."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def get(self, *, actor: TenantContext) -> TenantBrandDNA:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        row = self._conn.execute(
            """
            SELECT
                bp.business_id,
                b.name AS business_name,
                bp.brand_display_name,
                bp.brand_tone_json,
                bp.brand_visual_keywords_json,
                bp.brand_forbidden_visuals_json,
                bp.brand_primary_color,
                bp.brand_accent_color,
                bp.brand_text_color
            FROM business_profiles bp
            JOIN businesses b ON b.id = bp.business_id
            WHERE bp.business_id=?
            LIMIT 1
            """,
            (current.business_id,),
        ).fetchone()
        if row is None:
            raise ActivityNotFound("business activity profile was not found")

        defaults = TenantBrandDNA(business_id=current.business_id).normalized()
        display_name = _value(row, "brand_display_name", 2)
        return TenantBrandDNA(
            business_id=current.business_id,
            display_name=(
                str(display_name).strip()
                if display_name is not None and str(display_name).strip()
                else str(_value(row, "business_name", 1))
            ),
            tone=_tuple_json(_value(row, "brand_tone_json", 3), default=defaults.tone),
            visual_keywords=_tuple_json(
                _value(row, "brand_visual_keywords_json", 4),
                default=defaults.visual_keywords,
            ),
            forbidden_visuals=_tuple_json(
                _value(row, "brand_forbidden_visuals_json", 5),
                default=defaults.forbidden_visuals,
            ),
            primary_color=str(_value(row, "brand_primary_color", 6) or defaults.primary_color),
            accent_color=str(_value(row, "brand_accent_color", 7) or defaults.accent_color),
            text_color=str(_value(row, "brand_text_color", 8) or defaults.text_color),
        ).normalized()

    def update(
        self,
        *,
        actor: TenantContext,
        brand: TenantBrandDNA,
        now: str | None = None,
    ) -> TenantBrandDNA:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_business()
        value = brand.normalized()
        value.assert_business(current.business_id)
        existing = self._conn.execute(
            "SELECT business_id FROM business_profiles WHERE business_id=? LIMIT 1",
            (current.business_id,),
        ).fetchone()
        if existing is None:
            raise ActivityNotFound("business activity profile was not found")
        timestamp = str(now or _utc_now())
        self._conn.execute(
            """
            UPDATE business_profiles
            SET brand_display_name=?,
                brand_tone_json=?,
                brand_visual_keywords_json=?,
                brand_forbidden_visuals_json=?,
                brand_primary_color=?,
                brand_accent_color=?,
                brand_text_color=?,
                brand_updated_at=?,
                updated_at=?
            WHERE business_id=?
            """,
            (
                value.display_name,
                _encoded(value.tone),
                _encoded(value.visual_keywords),
                _encoded(value.forbidden_visuals),
                value.primary_color,
                value.accent_color,
                value.text_color,
                timestamp,
                timestamp,
                current.business_id,
            ),
        )
        return self.get(actor=current)


__all__ = ["VisualBrandRepository"]
