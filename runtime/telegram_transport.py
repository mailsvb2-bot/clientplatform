from __future__ import annotations

import os

from config.settings import settings

_POLLING = "polling"
_TRUTHY = frozenset({"1", "true", "yes", "on", "webhook"})
_WEBHOOK_ALIASES = frozenset({"webhook"})


def _truthy(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY
    return bool(value)


def telegram_webhook_requested() -> bool:
    """Return whether stale deployment configuration requested Telegram webhook.

    ClientPlatform intentionally ignores this request: Telegram ingress is
    polling-only. The signal exists for diagnostics and migration warnings while
    VK and MAX continue to use the independent HTTP webhook runtime.
    """

    raw_transport = (
        os.getenv("TELEGRAM_TRANSPORT")
        or getattr(settings, "TELEGRAM_TRANSPORT", _POLLING)
        or _POLLING
    ).strip().lower()
    webhook_enabled = _truthy(
        os.getenv("TELEGRAM_WEBHOOK_ENABLED")
        if os.getenv("TELEGRAM_WEBHOOK_ENABLED") is not None
        else getattr(settings, "TELEGRAM_WEBHOOK_ENABLED", False)
    )
    return raw_transport in _WEBHOOK_ALIASES or webhook_enabled


def telegram_transport() -> str:
    """Return the only supported Telegram ingress transport.

    Telegram is always consumed with long polling. Legacy webhook environment
    variables are deliberately ignored so an old server override cannot make the
    control bot silently wait on an unregistered or unreachable HTTP endpoint.
    Other messengers retain their own webhook flags and HTTP ingress runtime.
    """

    return _POLLING


__all__ = ["telegram_transport", "telegram_webhook_requested"]
