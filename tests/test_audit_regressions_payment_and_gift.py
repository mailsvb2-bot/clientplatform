from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable

import pytest

from handlers import payments, start
from runtime import payment_http


async def _direct_to_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


class _FakeStartMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.from_user = SimpleNamespace(
            id=7,
            username="user",
            full_name="User Name",
            first_name="User",
        )
        self.answers: list[tuple[str, dict[str, Any]]] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))


class _FakeStarsMessage:
    def __init__(self) -> None:
        self.from_user = SimpleNamespace(id=7)
        self.successful_payment = SimpleNamespace(
            currency="XTR",
            invoice_payload="payload",
            total_amount=50,
            telegram_payment_charge_id="charge",
            provider_payment_charge_id="provider",
        )
        self.answers: list[tuple[str, dict[str, Any]]] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))


class _FakePaymentRequest:
    def __init__(self) -> None:
        self.query = {
            "source": "vk",
            "user_id": "7",
            "external_user_id": "vk-7",
            "package_id": "audit-package",
            "kind": "tokens",
        }


@pytest.mark.asyncio
async def test_legacy_gift_parser_removes_only_the_leading_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(start.asyncio, "to_thread", _direct_to_thread)
    monkeypatch.setattr(start, "normalize_gift_token", lambda payload: payload)
    monkeypatch.setattr(start, "is_gift_token", lambda _token: False)
    monkeypatch.setattr(start, "_register_user_entry_safe", lambda *_args: None)

    opened: list[Any] = []

    async def open_menu(message: Any, **_kwargs: Any) -> None:
        opened.append(message)

    monkeypatch.setattr(start, "_open_main_menu_fail_open", open_menu)

    from handlers import gift_flow

    received_codes: list[str] = []

    async def send_intro(_message: Any, code: str) -> None:
        received_codes.append(code)

    monkeypatch.setattr(gift_flow, "send_gift_intro", send_intro)

    message = _FakeStartMessage("/start gift_ABgift_CD")
    await start.start_cmd(message)

    assert received_codes == ["ABgift_CD"]
    assert opened == [message]


@pytest.mark.asyncio
async def test_duplicate_stars_payment_gets_clear_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(payments.asyncio, "to_thread", _direct_to_thread)
    monkeypatch.setattr(
        payments,
        "record_successful_stars_payment",
        lambda **_kwargs: SimpleNamespace(duplicate=True, gift_token="", wallet_balance=None),
    )
    monkeypatch.setattr(payments, "kb_after_paid", lambda: "after-paid")

    message = _FakeStarsMessage()
    await payments._successful_payment(message)

    assert len(message.answers) == 1
    text, kwargs = message.answers[0]
    assert "уже обработан" in text
    assert "Повторного начисления не было" in text
    assert kwargs["reply_markup"] == "after-paid"


@pytest.mark.asyncio
async def test_duplicate_stars_gift_redelivers_existing_gift_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(payments.asyncio, "to_thread", _direct_to_thread)
    monkeypatch.setattr(
        payments,
        "record_successful_stars_payment",
        lambda **_kwargs: SimpleNamespace(
            duplicate=True,
            gift_token="gift_existing-code",
            wallet_balance=None,
        ),
    )
    delivered: list[str] = []

    async def deliver(_message: Any, code: str) -> None:
        delivered.append(code)

    monkeypatch.setattr(payments, "deliver_gift_message", deliver)

    message = _FakeStarsMessage()
    await payments._successful_payment(message)

    assert delivered == ["existing-code"]
    assert message.answers == []


@pytest.mark.asyncio
async def test_yookassa_checkout_error_does_not_expose_exception_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(payment_http.asyncio, "to_thread", _direct_to_thread)
    monkeypatch.setattr(payment_http, "package_by_id", lambda _package_id: SimpleNamespace(price_rub=100))
    monkeypatch.setattr(payment_http, "_checkout_intent_error_response", lambda **_kwargs: None)

    def fail_checkout(**_kwargs: Any) -> str:
        raise ValueError("provider details must remain private")

    monkeypatch.setattr(payment_http, "_create_yookassa_payment", fail_checkout)

    response = await payment_http.pay_yookassa_web(_FakePaymentRequest())
    body = response.text

    assert response.status == 500
    assert "PAYMENT_CREATE_FAILED" in body
    assert "ValueError" not in body
    assert "provider details" not in body


def test_checkout_intent_rejection_does_not_expose_verifier_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(payment_http, "checkout_intent_required", lambda: True)

    def reject_intent(*_args: Any, **_kwargs: Any) -> None:
        raise payment_http.CheckoutIntentError(
            "signature mismatch: expected internal-signature-value"
        )

    monkeypatch.setattr(payment_http, "verify_checkout_intent", reject_intent)

    response = payment_http._checkout_intent_error_response(
        intent="attacker-controlled-intent",
        user_id="7",
        package_id="practice_start_7",
        kind="tokens",
        source="vk",
        amount_minor=10000,
        currency="RUB",
        gift_token="",
    )
    assert response is not None
    assert response.status == 403
    assert "CHECKOUT_INTENT_INVALID" in response.text
    assert "signature mismatch" not in response.text
    assert "internal-signature-value" not in response.text
    assert "CheckoutIntentError" not in response.text
