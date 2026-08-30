from __future__ import annotations

import pytest

from services.db import db
from services.gift_claims import claim_gift_token
from services.messenger.text_ui import handle_incoming_text
from services.payments.reconciliation import record_yookassa_webhook
from services.practice_tokens import get_wallet
from services.schema import init_db
from services.practice_token_contract import package_by_id


@pytest.fixture(autouse=True)
def _enable_payment_surface(monkeypatch):
    monkeypatch.setenv("PAYMENT_HTTP_ENABLED", "1")


def _payment_payload(*, payment_id: str, user_id: int, package_id: str, gift_token: str = "") -> dict:
    package = package_by_id(package_id)
    metadata = {
        "user_id": str(int(user_id)),
        "kind": "tokens",
        "package_id": package.package_id,
        "source": "vk",
    }
    if gift_token:
        metadata["gift_token"] = gift_token
        metadata["gift"] = "1"
    return {
        "event": "payment.succeeded",
        "object": {
            "id": payment_id,
            "status": "succeeded",
            "amount": {"value": f"{package.price_rub}.00", "currency": "RUB"},
            "metadata": metadata,
        },
    }


def _first_payment_url(text: str) -> str:
    return next(line.strip() for line in text.splitlines() if "/pay/yookassa" in line)


def _query(url: str) -> dict[str, str]:
    from urllib.parse import parse_qs, urlsplit
    return {key: values[-1] for key, values in parse_qs(urlsplit(url).query).items()}


def test_emulated_regular_payment_opens_practices_for_buyer(monkeypatch):
    monkeypatch.setenv("MESSENGER_PUBLIC_BASE_URL", "https://bot.example")
    init_db()

    buyer_id = 910001
    _, replies = handle_incoming_text(
        buyer_id,
        platform="vk",
        external_user_id=str(buyer_id),
        text="pay",
    )
    url = _first_payment_url(replies[0].text)
    query = _query(url)

    before = get_wallet(buyer_id).available_tokens
    result = record_yookassa_webhook(
        _payment_payload(
            payment_id="emulated-regular-payment-1",
            user_id=buyer_id,
            package_id=query["package_id"],
        )
    )
    after = get_wallet(buyer_id).available_tokens

    assert result.ok is True
    assert result.problem == ""
    assert result.side_effects_done is True
    assert after > before


def test_emulated_gift_payment_opens_practices_only_after_recipient_claim(monkeypatch):
    monkeypatch.setenv("MESSENGER_PUBLIC_BASE_URL", "https://bot.example")
    init_db()

    buyer_id = 910002
    recipient_id = 910003

    handle_incoming_text(
        buyer_id,
        platform="vk",
        external_user_id=str(buyer_id),
        text="🎁 Подарить",
    )
    _, replies = handle_incoming_text(
        buyer_id,
        platform="vk",
        external_user_id=str(buyer_id),
        text="Получатель Иван vk.com/id910003",
    )

    url = _first_payment_url(replies[0].text)
    query = _query(url)
    gift_token = query["gift_token"]

    buyer_before = get_wallet(buyer_id).available_tokens
    recipient_before = get_wallet(recipient_id).available_tokens

    paid = record_yookassa_webhook(
        _payment_payload(
            payment_id="emulated-gift-payment-1",
            user_id=buyer_id,
            package_id=query["package_id"],
            gift_token=gift_token,
        )
    )

    assert paid.ok is True
    assert paid.problem == ""
    assert get_wallet(buyer_id).available_tokens == buyer_before
    assert get_wallet(recipient_id).available_tokens == recipient_before

    claimed = claim_gift_token(
        gift_token=gift_token,
        recipient_user_id=recipient_id,
        platform="vk",
    )

    assert claimed.ok is True
    assert get_wallet(recipient_id).available_tokens > recipient_before
    assert get_wallet(buyer_id).available_tokens == buyer_before

    with db() as conn:
        row = conn.execute(
            "SELECT status, buyer_user_id, recipient_user_id, recipient_hint FROM gift_claims WHERE gift_token=?",
            (gift_token,),
        ).fetchone()

    assert row["status"] == "claimed"
    assert row["buyer_user_id"] == buyer_id
    assert row["recipient_user_id"] == recipient_id
    assert "Иван" in row["recipient_hint"]
