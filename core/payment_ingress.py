from __future__ import annotations

import os
from collections.abc import Mapping

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_PROD_ENVS = frozenset({"prod", "production"})


def _value(env: Mapping[str, str], name: str, default: str = "") -> str:
    return str(env.get(name, default) or default).strip()


class PaymentIngressConfigurationError(ValueError):
    """Raised when the canonical payment-ingress flag is malformed."""


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
    app_env = _value(values, "APP_ENV", "dev").lower()
    if "PAYMENT_HTTP_ENABLED" in values:
        raw = _value(values, "PAYMENT_HTTP_ENABLED")
        normalized = raw.lower()
        if normalized in _TRUE_VALUES:
            return True
        if normalized in _FALSE_VALUES:
            return False
        if app_env in _PROD_ENVS:
            rendered = raw if raw else "<empty>"
            raise PaymentIngressConfigurationError(
                "PAYMENT_HTTP_ENABLED must be an explicit boolean in production "
                f"(1/0, true/false, yes/no, on/off); got {rendered!r}"
            )
        # Preserve historical non-production compatibility for unknown values.
        return False

    if app_env in _PROD_ENVS:
        return True

    if legacy_default is not None:
        return bool(legacy_default)
    return _value(values, "MESSENGER_WEBHOOK_ENABLED").lower() in _TRUE_VALUES


__all__ = ["PaymentIngressConfigurationError", "resolve_payment_http_enabled"]
