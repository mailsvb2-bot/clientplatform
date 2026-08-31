from __future__ import annotations

import os
from collections.abc import Mapping

_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "webhook"})
_PROD_ENVS = frozenset({"prod", "production"})


def _value(env: Mapping[str, str], name: str, default: str = "") -> str:
    return str(env.get(name, default) or default).strip()


def resolve_payment_http_enabled(
    env: Mapping[str, str] | None = None,
    *,
    legacy_default: bool | None = None,
) -> bool:
    """Resolve the canonical external payment-ingress switch.

    An explicit PAYMENT_HTTP_ENABLED value always wins. In production, omission
    preserves the historical fail-closed checkout default. Outside production,
    callers may supply the legacy settings-derived default; otherwise the old
    MESSENGER_WEBHOOK_ENABLED environment flag remains the compatibility fallback.
    """

    values = os.environ if env is None else env
    if "PAYMENT_HTTP_ENABLED" in values:
        return _value(values, "PAYMENT_HTTP_ENABLED").lower() in _TRUE_VALUES

    app_env = _value(values, "APP_ENV", "dev").lower()
    if app_env in _PROD_ENVS:
        return True

    if legacy_default is not None:
        return bool(legacy_default)
    return _value(values, "MESSENGER_WEBHOOK_ENABLED").lower() in _TRUE_VALUES


__all__ = ["resolve_payment_http_enabled"]
