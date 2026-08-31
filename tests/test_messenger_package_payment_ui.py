from __future__ import annotations

from runtime import messenger_max_ui
from services.messenger.package_payment_ui import extract_labeled_urls, gift_package_text, package_payment_text
from services.messenger.reply_dispatcher import _canonical_payment_text


def test_package_payment_text_uses_canonical_packages(monkeypatch):
    monkeypatch.setenv("MESSENGER_PUBLIC_BASE_URL", "https://bot.example")

    text = package_payment_text(user_id=404, platform="vk", external_user_id="vk404")

    assert "Стартовый пакет — 2 499 ₽" in text
    assert "Полный маршрут — 4 199 ₽" in text
    assert "Антистресс-система — 8 290 ₽" in text
    assert "Персональный месяц — 24 870 ₽" in text
    assert "kind=tokens" in text
    assert "package_id=practice_start_7" in text
    assert "package_id=practice_60" in text
    assert "package_id=practice_antistress_60" in text
    assert "package_id=practice_personal_month" in text
    assert "kind=subscription" not in text
    assert "morning_5" not in text
    assert "both_20" not in text


def test_gift_package_text_uses_same_public_ladder(monkeypatch):
    monkeypatch.setenv("MESSENGER_PUBLIC_BASE_URL", "https://bot.example")

    text = gift_package_text(user_id=505, platform="max", external_user_id="max505", recipient_hint="Мария")

    assert "🎁 Подарить Метротерапию" in text
    assert "Стартовый пакет — 2 499 ₽" in text
    assert "Персональный месяц — 24 870 ₽" in text
    assert "kind=tokens" in text
    assert "package_id=practice_personal_month" in text
    assert "kind=gift" not in text


def test_extract_labeled_urls_finds_all_package_links(monkeypatch):
    monkeypatch.setenv("MESSENGER_PUBLIC_BASE_URL", "https://bot.example")

    pairs = extract_labeled_urls(package_payment_text(user_id=1, platform="vk", external_user_id="1"))

    assert len(pairs) == 4
    labels = [label for label, _url in pairs]
    urls = [url for _label, url in pairs]
    assert any("Стартовый пакет" in label for label in labels)
    assert any("Персональный месяц" in label for label in labels)
    assert all("kind=tokens" in url for url in urls)


def test_dispatcher_upgrades_legacy_payment_text_to_package_surface(monkeypatch):
    monkeypatch.setenv("PAYMENT_HTTP_ENABLED", "1")
    monkeypatch.setenv("MESSENGER_PUBLIC_BASE_URL", "https://bot.example")

    text = _canonical_payment_text(
        "vk",
        606,
        "vk606",
        "💳 Оплата доступа к Метротерапии\n\nhttps://old.example/pay/yookassa?kind=subscription",
    )

    assert "💳 Тарифы Метротерапии" in text
    assert "package_id=practice_60" in text
    assert "kind=tokens" in text
    assert "kind=subscription" not in text


def test_max_package_payment_has_link_buttons(monkeypatch):
    monkeypatch.setenv("MESSENGER_PUBLIC_BASE_URL", "https://bot.example")
    text = package_payment_text(user_id=707, platform="max", external_user_id="max707")

    attachments = messenger_max_ui.native_keyboard_attachments(text)

    assert len(attachments) == 1
    buttons = attachments[0]["payload"]["buttons"]
    flattened = [button for row in buttons for button in row]
    link_buttons = [button for button in flattened if button.get("type") == "link"]
    assert len(link_buttons) == 4
    assert any("practice_start_7" in button["url"] for button in link_buttons)
    assert any("practice_personal_month" in button["url"] for button in link_buttons)
    assert any(button.get("type") == "message" and button.get("payload", {}).get("command") == "continue" for button in flattened)
