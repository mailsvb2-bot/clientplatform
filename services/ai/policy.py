from __future__ import annotations

import os
from typing import Any

from config.settings import settings

AI_ROLE = "clientplatform_business_advisor"
AI_ROLE_LABEL_RU = "AI-помощник ClientPlatform"

AI_ALLOWED_SCOPES: tuple[str, ...] = (
    "business_analysis",
    "sales_assistance",
    "content_assistance",
    "growth_analysis",
)

AI_FORBIDDEN_SCOPES: tuple[str, ...] = (
    "credential_disclosure",
    "unapproved_financial_commitment",
    "unapproved_external_write",
    "tenant_boundary_bypass",
)


def _env_bool(name: str, default: int | str = 1) -> bool:
    raw = os.getenv(name)
    value = str(default if raw is None else raw).strip().lower()
    return value in {"1", "true", "yes", "on"}


def ai_enabled_from_settings() -> bool:
    if "AI_ENABLED" in os.environ:
        return _env_bool("AI_ENABLED", 1)
    try:
        return int(getattr(settings, "AI_ENABLED", 1) or 0) == 1
    except (TypeError, ValueError):
        return False


def ai_provider_configured() -> bool:
    from services.ai.providers.router import provider_configured
    return provider_configured()


def ai_provider_name() -> str:
    from services.ai.providers.router import provider_name
    return provider_name()


def ai_policy_snapshot() -> dict[str, Any]:
    return {
        "ai_role": AI_ROLE,
        "ai_role_label": AI_ROLE_LABEL_RU,
        "ai_enabled": ai_enabled_from_settings(),
        "ai_provider": ai_provider_name(),
        "ai_provider_configured": ai_provider_configured(),
        "ai_allowed_scopes": list(AI_ALLOWED_SCOPES),
        "ai_forbidden_scopes": list(AI_FORBIDDEN_SCOPES),
    }
