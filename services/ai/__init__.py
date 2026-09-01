"""Shared AI provider helpers used by ClientPlatform runtime diagnostics."""

from services.ai.policy import (
    AI_ALLOWED_SCOPES,
    AI_FORBIDDEN_SCOPES,
    AI_ROLE,
    AI_ROLE_LABEL_RU,
    ai_enabled_from_settings,
    ai_policy_snapshot,
    ai_provider_configured,
    ai_provider_name,
)

__all__ = [
    "AI_ALLOWED_SCOPES",
    "AI_FORBIDDEN_SCOPES",
    "AI_ROLE",
    "AI_ROLE_LABEL_RU",
    "ai_enabled_from_settings",
    "ai_policy_snapshot",
    "ai_provider_configured",
    "ai_provider_name",
]
