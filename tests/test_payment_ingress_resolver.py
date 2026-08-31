from __future__ import annotations

import pytest

from core.payment_ingress import PaymentIngressConfigurationError, resolve_payment_http_enabled


def test_explicit_payment_flag_always_wins() -> None:
    assert resolve_payment_http_enabled({"APP_ENV": "prod", "PAYMENT_HTTP_ENABLED": "0"}) is False
    assert resolve_payment_http_enabled({"APP_ENV": "dev", "PAYMENT_HTTP_ENABLED": "1"}) is True


def test_production_omission_preserves_fail_closed_default() -> None:
    assert resolve_payment_http_enabled({"APP_ENV": "prod", "MESSENGER_WEBHOOK_ENABLED": "0"}) is True
    assert resolve_payment_http_enabled({"APP_ENV": "production"}) is True


def test_nonproduction_omission_uses_legacy_fallback() -> None:
    assert resolve_payment_http_enabled({"APP_ENV": "test", "MESSENGER_WEBHOOK_ENABLED": "1"}) is True
    assert resolve_payment_http_enabled({"APP_ENV": "test", "MESSENGER_WEBHOOK_ENABLED": "0"}) is False
    assert resolve_payment_http_enabled({"APP_ENV": "test"}, legacy_default=True) is True


@pytest.mark.parametrize("raw", ["", "tru", "enabled", "2", "webhook"])
def test_production_rejects_malformed_explicit_payment_flag(raw: str) -> None:
    with pytest.raises(PaymentIngressConfigurationError, match="PAYMENT_HTTP_ENABLED"):
        resolve_payment_http_enabled({"APP_ENV": "prod", "PAYMENT_HTTP_ENABLED": raw})


def test_nonproduction_keeps_legacy_unknown_value_compatibility() -> None:
    assert resolve_payment_http_enabled({"APP_ENV": "dev", "PAYMENT_HTTP_ENABLED": "legacy-value"}) is False
