from __future__ import annotations

from urllib.parse import urlencode

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from runtime import payment_http
from services.payments.checkout_intent import sign_checkout_intent


def _request(*, source: str, user_id: str, package_id: str, intent: str = "") -> web.Request:
    query = {
        "source": source,
        "user_id": user_id,
        "external_user_id": user_id,
        "kind": "tokens",
        "package_id": package_id,
    }
    if intent:
        query["intent"] = intent
    return make_mocked_request("GET", f"/pay/yookassa?{urlencode(query)}")


def _assert_sanitized_intent_error(response: web.Response, *, hidden_reason: str) -> None:
    assert response.status == 403
    assert "CHECKOUT_INTENT_INVALID" in response.text
    assert hidden_reason not in response.text
    assert "CheckoutIntentError" not in response.text


@pytest.mark.asyncio
async def test_valid_source_bound_checkout_reaches_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    intent = sign_checkout_intent(
        user_id=123,
        package_id="practice_start_7",
        source="vk",
    )
    calls: list[dict[str, object]] = []

    def fake_checkout(**kwargs: object) -> str:
        calls.append(dict(kwargs))
        return "https://provider.example/confirmation"

    monkeypatch.setattr(payment_http, "_create_yookassa_payment", fake_checkout)
    with pytest.raises(web.HTTPFound) as redirect:
        await payment_http.pay_yookassa_web(
            _request(
                source="vk",
                user_id="123",
                package_id="practice_start_7",
                intent=intent,
            )
        )

    assert redirect.value.location == "https://provider.example/confirmation"
    assert len(calls) == 1
    assert calls[0]["source"] == "vk"
    assert calls[0]["user_id"] == "123"
    assert calls[0]["package_id"] == "practice_start_7"


@pytest.mark.asyncio
async def test_query_source_cannot_override_signed_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    intent = sign_checkout_intent(
        user_id=123,
        package_id="practice_start_7",
        source="vk",
    )
    monkeypatch.setattr(
        payment_http,
        "_create_yookassa_payment",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("tampered checkout must not reach provider")
        ),
    )

    response = await payment_http.pay_yookassa_web(
        _request(
            source="max",
            user_id="123",
            package_id="practice_start_7",
            intent=intent,
        )
    )

    _assert_sanitized_intent_error(response, hidden_reason="source_mismatch")


@pytest.mark.asyncio
async def test_query_package_cannot_override_signed_package(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    intent = sign_checkout_intent(
        user_id=123,
        package_id="practice_start_7",
        source="vk",
    )
    monkeypatch.setattr(
        payment_http,
        "_create_yookassa_payment",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("tampered checkout must not reach provider")
        ),
    )

    response = await payment_http.pay_yookassa_web(
        _request(
            source="vk",
            user_id="123",
            package_id="practice_60",
            intent=intent,
        )
    )

    _assert_sanitized_intent_error(response, hidden_reason="package_id_mismatch")


@pytest.mark.asyncio
async def test_unsigned_checkout_is_rejected_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("PAYMENT_CHECKOUT_SIGNING_KEY", "test-only-signing-key")
    monkeypatch.delenv("PAYMENT_CHECKOUT_INTENT_REQUIRED", raising=False)
    monkeypatch.delenv("ALLOW_UNSIGNED_PAYMENT_CHECKOUT_IN_PROD", raising=False)
    monkeypatch.setattr(
        payment_http,
        "_create_yookassa_payment",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("unsigned checkout must not reach provider")
        ),
    )

    response = await payment_http.pay_yookassa_web(
        _request(source="vk", user_id="123", package_id="practice_start_7")
    )

    _assert_sanitized_intent_error(response, hidden_reason="missing_checkout_intent")
