from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from services.messenger import text_ui
from services.messenger.text_ui import handle_incoming_text
from services.payments.checkout_intent import verify_checkout_intent
from services.schema import init_db


EXPECTED_PACKAGE_IDS = {
    "practice_start_7",
    "practice_60",
    "practice_antistress_60",
    "practice_personal_month",
}


def setup_module(module):
    init_db()


def _payment_urls(text: str) -> list[str]:
    return [line.strip() for line in str(text or "").splitlines() if line.strip().startswith("http") and "/pay/yookassa" in line]


def _query(url: str) -> dict[str, str]:
    values = parse_qs(urlsplit(url).query, keep_blank_values=True)
    return {key: item[-1] for key, item in values.items() if item}


def _assert_canonical_package_payment_text(text: str, *, source: str) -> None:
    assert "💳 Тарифы Метротерапии" in text
    assert "/pay/yookassa" in text
    assert f"source={source}" in text
    assert "kind=tokens" in text
    for package_id in EXPECTED_PACKAGE_IDS:
        assert f"package_id={package_id}" in text
    assert "kind=subscription" not in text
    assert "kind=gift" not in text
    assert "morning_5" not in text
    assert "both_20" not in text


def _assert_canonical_gift_package_text(text: str, *, source: str) -> None:
    assert "🎁 Подарить Метротерапию" in text
    assert "/pay/yookassa" in text
    assert f"source={source}" in text
    assert "kind=tokens" in text
    for package_id in EXPECTED_PACKAGE_IDS:
        assert f"package_id={package_id}" in text
    assert "kind=gift" not in text
    assert "kind=subscription" not in text
    assert "morning_5" not in text
    assert "both_20" not in text


@pytest.mark.parametrize(
    ("platform", "external_user_id"),
    (("vk", "902051"), ("max", "mx902051")),
)
def test_pay_command_is_graceful_when_payment_http_is_disabled(
    monkeypatch, platform, external_user_id
):
    monkeypatch.setenv("PAYMENT_HTTP_ENABLED", "0")
    monkeypatch.delenv("PAYMENT_CHECKOUT_SIGNING_KEY", raising=False)
    monkeypatch.delenv("CHECKOUT_SIGNING_KEY", raising=False)

    user_id, replies = handle_incoming_text(
        902051,
        platform=platform,
        external_user_id=external_user_id,
        text="💳 Тарифы",
    )

    assert user_id >= (1 << 62)
    assert replies == [text_ui.MessengerReply(text=text_ui.PAYMENT_UNAVAILABLE_TEXT)]
    assert "/pay/yookassa" not in replies[0].text


def test_vk_gift_command_does_not_start_pending_flow_when_payment_is_disabled(
    monkeypatch,
):
    monkeypatch.setenv("PAYMENT_HTTP_ENABLED", "0")
    pending_calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(text_ui, "set_pending", lambda *args: pending_calls.append(args))

    _, replies = handle_incoming_text(
        902052,
        platform="vk",
        external_user_id="902052",
        text="🎁 Подарить",
    )

    assert replies == [text_ui.MessengerReply(text=text_ui.PAYMENT_UNAVAILABLE_TEXT)]
    assert pending_calls == []


def test_pending_gift_becomes_unavailable_if_payment_is_disabled_before_recipient(
    monkeypatch,
):
    monkeypatch.setenv("PAYMENT_HTTP_ENABLED", "1")
    handle_incoming_text(
        902053,
        platform="vk",
        external_user_id="902053",
        text="🎁 Подарить",
    )

    monkeypatch.setenv("PAYMENT_HTTP_ENABLED", "0")
    monkeypatch.delenv("PAYMENT_CHECKOUT_SIGNING_KEY", raising=False)
    monkeypatch.delenv("CHECKOUT_SIGNING_KEY", raising=False)
    _, replies = handle_incoming_text(
        902053,
        platform="vk",
        external_user_id="902053",
        text="Иван Петров vk.com/id12345",
    )

    assert replies == [text_ui.MessengerReply(text=text_ui.PAYMENT_UNAVAILABLE_TEXT)]
    assert "/pay/yookassa" not in replies[0].text

def test_vk_pay_command_returns_canonical_package_payment_text():
    user_id, replies = handle_incoming_text(
        902001,
        platform="vk",
        external_user_id="902001",
        text="💳 Тарифы",
    )

    assert user_id >= (1 << 62)
    assert replies
    assert replies[0].kind == "text"
    _assert_canonical_package_payment_text(replies[0].text, source="vk")


def test_max_pay_command_returns_canonical_package_payment_text():
    user_id, replies = handle_incoming_text(
        902002,
        platform="max",
        external_user_id="mx902002",
        text="pay",
    )

    assert user_id >= (1 << 62)
    assert replies
    assert replies[0].kind == "text"
    _assert_canonical_package_payment_text(replies[0].text, source="max")


def test_vk_gift_command_asks_recipient_before_payment_links():
    user_id, replies = handle_incoming_text(
        902003,
        platform="vk",
        external_user_id="902003",
        text="🎁 Подарить",
    )

    assert user_id >= (1 << 62)
    assert replies
    assert replies[0].kind == "text"
    assert "Кому подарить" in replies[0].text
    assert "/pay/yookassa" not in replies[0].text


def test_vk_gift_recipient_then_returns_canonical_package_gift_text():
    handle_incoming_text(
        902033,
        platform="vk",
        external_user_id="902033",
        text="🎁 Подарить",
    )

    user_id, replies = handle_incoming_text(
        902033,
        platform="vk",
        external_user_id="902033",
        text="Иван Петров vk.com/id12345",
    )

    assert user_id >= (1 << 62)
    assert replies
    assert replies[0].kind == "text"
    assert "Получатель: Иван Петров vk.com/id12345" in replies[0].text
    _assert_canonical_gift_package_text(replies[0].text, source="vk")


def test_max_gift_command_asks_recipient_before_payment_links():
    user_id, replies = handle_incoming_text(
        902004,
        platform="max",
        external_user_id="mx902004",
        text="gift",
    )

    assert user_id >= (1 << 62)
    assert replies
    assert replies[0].kind == "text"
    assert "Кому подарить" in replies[0].text
    assert "/pay/yookassa" not in replies[0].text


def test_max_payment_link_uses_canonical_user_id_and_preserves_external_id():
    user_id, replies = handle_incoming_text(
        902002,
        platform="max",
        external_user_id="mx902002",
        text="pay",
    )

    assert user_id >= (1 << 62)
    url = _payment_urls(replies[0].text)[0]
    query = _query(url)
    assert query["user_id"] == str(user_id)
    assert query["external_user_id"] == "mx902002"
    assert query["source"] == "max"
    assert query["kind"] == "tokens"
    assert query["package_id"] in EXPECTED_PACKAGE_IDS
    verify_checkout_intent(
        query["intent"],
        expected_user_id=str(user_id),
        expected_package_id=query["package_id"],
        expected_kind="tokens",
        expected_source="max",
        expected_amount_minor=int(query["amount_minor"]),
        expected_currency=query["currency"],
    )


def test_max_gift_link_uses_reserved_gift_token_and_canonical_intent():
    handle_incoming_text(
        902044,
        platform="max",
        external_user_id="mx902044",
        text="gift",
    )
    user_id, replies = handle_incoming_text(
        902044,
        platform="max",
        external_user_id="mx902044",
        text="Мария из MAX",
    )

    assert user_id >= (1 << 62)
    url = _payment_urls(replies[0].text)[0]
    query = _query(url)
    assert query["user_id"] == str(user_id)
    assert query["external_user_id"] == "mx902044"
    assert query["source"] == "max"
    assert query["kind"] == "tokens"
    assert query["gift"] == "1"
    assert query["gift_token"].startswith("gift_")
    verify_checkout_intent(
        query["intent"],
        expected_user_id=str(user_id),
        expected_package_id=query["package_id"],
        expected_kind="tokens",
        expected_source="max",
        expected_amount_minor=int(query["amount_minor"]),
        expected_currency=query["currency"],
        expected_gift_token=query["gift_token"],
    )
