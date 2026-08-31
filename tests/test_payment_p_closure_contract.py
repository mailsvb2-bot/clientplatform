from __future__ import annotations

import pytest

from runtime import payment_http
from services.payments import yookassa_checkout
from services.payments.public_url import payment_public_base_url
from services.practice_tokens import enforcement_mode
from services.validators.base import ValidationError
from services.validators.prod import validate_prod_monetization_contract


def _set_valid_prod_monetization_env(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("TOKEN_ECONOMY_ENABLED", "1")
    monkeypatch.setenv("TOKEN_ENFORCEMENT_MODE", "hard")
    monkeypatch.setenv("YOOKASSA_RECEIPT_EMAIL", "billing@example.com")
    monkeypatch.delenv("YOOKASSA_PROVIDER_VERIFICATION_REQUIRED", raising=False)
    monkeypatch.delenv("PAYMENT_CHECKOUT_INTENT_REQUIRED", raising=False)
    monkeypatch.delenv("ALLOW_UNVERIFIED_YOOKASSA_WEBHOOK_IN_PROD", raising=False)
    monkeypatch.delenv("ALLOW_UNSIGNED_PAYMENT_CHECKOUT_IN_PROD", raising=False)


def test_prod_monetization_guard_requires_hard_token_enforcement(monkeypatch):
    _set_valid_prod_monetization_env(monkeypatch)
    monkeypatch.delenv("TOKEN_ENFORCEMENT_MODE", raising=False)

    with pytest.raises(ValidationError, match="TOKEN_ENFORCEMENT_MODE"):
        validate_prod_monetization_contract(strict=True)

    monkeypatch.setenv("TOKEN_ENFORCEMENT_MODE", "soft")
    with pytest.raises(ValidationError, match="TOKEN_ENFORCEMENT_MODE"):
        validate_prod_monetization_contract(strict=True)

    monkeypatch.setenv("TOKEN_ENFORCEMENT_MODE", "off")
    with pytest.raises(ValidationError, match="TOKEN_ENFORCEMENT_MODE"):
        validate_prod_monetization_contract(strict=True)


def test_prod_monetization_guard_rejects_disabled_token_economy(monkeypatch):
    _set_valid_prod_monetization_env(monkeypatch)
    monkeypatch.setenv("TOKEN_ECONOMY_ENABLED", "0")

    with pytest.raises(ValidationError, match="TOKEN_ECONOMY_ENABLED"):
        validate_prod_monetization_contract(strict=True)


def test_prod_monetization_guard_requires_explicit_receipt_email(monkeypatch):
    _set_valid_prod_monetization_env(monkeypatch)
    monkeypatch.delenv("YOOKASSA_RECEIPT_EMAIL", raising=False)
    monkeypatch.delenv("PAYMENT_RECEIPT_EMAIL", raising=False)
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)

    with pytest.raises(ValidationError, match="RECEIPT_EMAIL|ADMIN_EMAIL"):
        validate_prod_monetization_contract(strict=True)


def test_prod_monetization_guard_skips_yookassa_receipt_when_payment_disabled(monkeypatch):
    _set_valid_prod_monetization_env(monkeypatch)
    monkeypatch.setenv("PAYMENT_HTTP_ENABLED", "0")
    monkeypatch.delenv("YOOKASSA_RECEIPT_EMAIL", raising=False)
    monkeypatch.delenv("PAYMENT_RECEIPT_EMAIL", raising=False)
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)

    validate_prod_monetization_contract(strict=True)


def test_prod_monetization_guard_rejects_direct_security_disable_flags(monkeypatch):
    _set_valid_prod_monetization_env(monkeypatch)
    monkeypatch.setenv("YOOKASSA_PROVIDER_VERIFICATION_REQUIRED", "0")

    with pytest.raises(ValidationError, match="YOOKASSA_PROVIDER_VERIFICATION_REQUIRED"):
        validate_prod_monetization_contract(strict=True)

    monkeypatch.setenv("YOOKASSA_PROVIDER_VERIFICATION_REQUIRED", "1")
    monkeypatch.setenv("PAYMENT_CHECKOUT_INTENT_REQUIRED", "off")
    with pytest.raises(ValidationError, match="PAYMENT_CHECKOUT_INTENT_REQUIRED"):
        validate_prod_monetization_contract(strict=True)


def test_prod_monetization_guard_rejects_legacy_payment_bypasses_even_with_drill_flag(monkeypatch):
    _set_valid_prod_monetization_env(monkeypatch)
    monkeypatch.setenv("PAYMENT_DANGEROUS_OVERRIDES_ALLOWED", "1")
    monkeypatch.setenv("ALLOW_UNVERIFIED_YOOKASSA_WEBHOOK_IN_PROD", "1")

    with pytest.raises(ValidationError, match="ALLOW_UNVERIFIED_YOOKASSA_WEBHOOK_IN_PROD"):
        validate_prod_monetization_contract(strict=True)

    monkeypatch.delenv("ALLOW_UNVERIFIED_YOOKASSA_WEBHOOK_IN_PROD", raising=False)
    monkeypatch.setenv("ALLOW_UNSIGNED_PAYMENT_CHECKOUT_IN_PROD", "1")
    with pytest.raises(ValidationError, match="ALLOW_UNSIGNED_PAYMENT_CHECKOUT_IN_PROD"):
        validate_prod_monetization_contract(strict=True)


def test_prod_monetization_guard_accepts_hard_tokens_and_receipt_email(monkeypatch):
    _set_valid_prod_monetization_env(monkeypatch)

    validate_prod_monetization_contract(strict=True)


def test_prod_receipt_builder_fails_closed_without_explicit_contact(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.delenv("YOOKASSA_RECEIPT_EMAIL", raising=False)
    monkeypatch.delenv("PAYMENT_RECEIPT_EMAIL", raising=False)
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)

    with pytest.raises(yookassa_checkout.YooKassaCheckoutError, match="RECEIPT_EMAIL|ADMIN_EMAIL"):
        yookassa_checkout.build_yookassa_receipt(amount_value="1.00", description="Metrotherapy")


def test_yookassa_checkout_intent_id_is_stable_for_same_signed_intent():
    first = yookassa_checkout._checkout_intent_id("signed-body.signature-a")
    second = yookassa_checkout._checkout_intent_id("signed-body.signature-a")
    changed_signature_same_body = yookassa_checkout._checkout_intent_id("signed-body.signature-b")
    changed_body = yookassa_checkout._checkout_intent_id("another-body.signature-a")

    assert first == second
    assert first == changed_signature_same_body
    assert first != changed_body


def test_runtime_payment_passes_checkout_intent_to_provider(monkeypatch):
    captured: dict[str, object] = {}

    def fake_create_confirmation_url(**kwargs):
        captured.update(kwargs)
        return "https://payment.example/ok"

    monkeypatch.setattr(payment_http, "create_yookassa_confirmation_url", fake_create_confirmation_url)

    result = payment_http._create_yookassa_payment(
        source="telegram",
        external_user_id="123",
        kind="tokens",
        package_id="practice_start_7",
        gift_token=None,
        checkout_intent="signed-body.signature",
    )

    assert result == "https://payment.example/ok"
    assert captured["checkout_intent"] == "signed-body.signature"


def test_dev_token_enforcement_defaults_to_off(monkeypatch):
    monkeypatch.delenv("TOKEN_ENFORCEMENT_MODE", raising=False)
    monkeypatch.setenv("APP_ENV", "dev")

    assert enforcement_mode() == "off"


def test_explicit_dev_token_enforcement_modes_still_win(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("TOKEN_ENFORCEMENT_MODE", "hard")
    assert enforcement_mode() == "hard"

    monkeypatch.setenv("TOKEN_ENFORCEMENT_MODE", "off")
    assert enforcement_mode() == "off"


def test_legacy_public_payment_kinds_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_LEGACY_PUBLIC_PAYMENT_KINDS", raising=False)

    assert payment_http._legacy_kind_error_response("subscription") is not None
    assert payment_http._legacy_kind_error_response("gift") is not None
    assert payment_http._legacy_kind_error_response("tokens") is None


def test_legacy_public_payment_kinds_can_be_enabled_explicitly(monkeypatch):
    monkeypatch.setenv("ENABLE_LEGACY_PUBLIC_PAYMENT_KINDS", "1")

    assert payment_http._legacy_kind_error_response("subscription") is None
    assert payment_http._legacy_kind_error_response("gift") is None


def test_payment_kind_normalization_prefers_tokens_and_package_links():
    assert payment_http._normalize_payment_kind(None, "") == "tokens"
    assert payment_http._normalize_payment_kind("unknown", "") == "tokens"
    assert payment_http._normalize_payment_kind("subscription", "practice_start_7") == "tokens"
    assert payment_http._normalize_payment_kind("gift", "practice_start_7") == "tokens"
    assert payment_http._normalize_payment_kind("tokens", "practice_60") == "tokens"


def test_payment_specific_public_base_url_takes_precedence(monkeypatch):
    monkeypatch.setenv("MESSENGER_PUBLIC_BASE_URL", "https://messenger.example/")
    monkeypatch.setenv("PAYMENT_PUBLIC_BASE_URL", "https://payment.example")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://public.example")

    assert payment_public_base_url() == "https://payment.example"
