from __future__ import annotations

from clientplatform.domain.tenancy import TenantContext
from clientplatform.domain.visual_style_intent import VisualStyleIntent
from clientplatform.infrastructure.visual_style_preference_repository import (
    VisualStylePreferenceRepository,
)
from services.db import get_db, get_db_ro


def load_visual_style_preference(*, actor: TenantContext) -> VisualStyleIntent:
    with get_db_ro() as conn:
        return VisualStylePreferenceRepository(conn).get(actor=actor)


def save_visual_style_preference(
    *,
    actor: TenantContext,
    style: VisualStyleIntent,
) -> VisualStyleIntent:
    with get_db() as conn:
        return VisualStylePreferenceRepository(conn).save(actor=actor, style=style)


__all__ = ["load_visual_style_preference", "save_visual_style_preference"]
