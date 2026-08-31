from __future__ import annotations

import os

from config.settings import settings
from core.payment_ingress import resolve_payment_http_enabled
from services.privacy_export_links import privacy_export_http_enabled

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _optional_env_flag(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    return raw.strip().lower() in _TRUE_VALUES


def payment_http_enabled() -> bool:
    """Return whether the YooKassa HTTP checkout/reconciliation ingress is enabled.

    PAYMENT_HTTP_ENABLED is the canonical flag. The legacy common messenger flag
    remains a compatibility fallback so existing deployments do not lose payment
    routes during rollout of the split ingress contract.
    """

    return resolve_payment_http_enabled(
        os.environ,
        legacy_default=bool(getattr(settings, "MESSENGER_WEBHOOK_ENABLED", False) or False),
    )


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
        payment_http_enabled()
        or max_webhook_enabled()
        or vk_webhook_enabled()
        or privacy_export_http_enabled()
    )
