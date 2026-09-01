from __future__ import annotations

import os

from config.settings import settings
from services.privacy_export_links import privacy_export_http_enabled

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _optional_env_flag(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    return raw.strip().lower() in _TRUE_VALUES



def max_webhook_enabled() -> bool:
    """Return whether MAX webhook ingress is enabled."""

    explicit = _optional_env_flag("MAX_WEBHOOK_ENABLED")
    if explicit is not None:
        return explicit
    return bool(
        getattr(settings, "MESSENGER_WEBHOOK_ENABLED", False)
        and str(getattr(settings, "MAX_BOT_TOKEN", "") or "").strip()
    )


def vk_webhook_enabled() -> bool:
    """Return whether VK webhook ingress is enabled."""

    explicit = _optional_env_flag("VK_WEBHOOK_ENABLED")
    if explicit is not None:
        return explicit
    return bool(
        getattr(settings, "MESSENGER_WEBHOOK_ENABLED", False)
        and str(getattr(settings, "VK_GROUP_TOKEN", "") or "").strip()
    )


def http_ingress_enabled() -> bool:
    return bool(
        max_webhook_enabled()
        or vk_webhook_enabled()
        or privacy_export_http_enabled()
    )
