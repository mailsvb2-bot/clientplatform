from __future__ import annotations

from core.payment_ingress import resolve_payment_http_enabled


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
