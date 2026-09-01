from __future__ import annotations

from services.messenger import preflight


def test_telegram_preflight_accepts_polling_with_bot_token(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.delenv("TELEGRAM_TRANSPORT", raising=False)
    monkeypatch.delenv("TELEGRAM_WEBHOOK_ENABLED", raising=False)
    monkeypatch.setattr(preflight.settings, "BOT_TOKEN", "000000:TEST", raising=False)
    monkeypatch.setattr(preflight.settings, "TELEGRAM_TRANSPORT", "polling", raising=False)
    monkeypatch.setattr(preflight.settings, "TELEGRAM_WEBHOOK_ENABLED", False, raising=False)

    status = preflight.check_telegram_preflight()

    assert status.ok is True
    assert status.channel == "telegram"
    assert status.warnings == ()
    assert status.details == {
        "enabled": False,
        "transport": "polling",
        "webhook_enabled": False,
        "webhook_requested": False,
    }


def test_telegram_preflight_reports_stale_webhook_as_ignored_warning(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("TELEGRAM_TRANSPORT", "webhook")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_ENABLED", "1")
    monkeypatch.setattr(preflight.settings, "BOT_TOKEN", "000000:TEST", raising=False)
    monkeypatch.setattr(preflight.settings, "TELEGRAM_WEBHOOK_PUBLIC_BASE_URL", "", raising=False)
    monkeypatch.setattr(preflight.settings, "TELEGRAM_WEBHOOK_SECRET_TOKEN", "", raising=False)

    status = preflight.check_telegram_preflight()

    assert status.ok is True
    assert status.missing == ()
    assert status.warnings == (
        "Stale Telegram webhook configuration is ignored; runtime remains polling-only",
    )
    assert status.details == {
        "enabled": True,
        "transport": "polling",
        "webhook_enabled": False,
        "webhook_requested": True,
    }


def test_telegram_preflight_still_requires_bot_token(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("TELEGRAM_TRANSPORT", "webhook")
    monkeypatch.setattr(preflight.settings, "BOT_TOKEN", "", raising=False)

    status = preflight.check_telegram_preflight()

    assert status.ok is False
    assert status.missing == ("BOT_TOKEN",)
    assert status.details is not None
    assert status.details["transport"] == "polling"
    assert status.details["webhook_enabled"] is False



def test_vk_preflight_warns_when_secret_missing_for_dev_webhook(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.delenv("VK_WEBHOOK_ENABLED", raising=False)
    monkeypatch.delenv("VK_CALLBACK_SNACKBAR_ENABLED", raising=False)
    monkeypatch.setattr(preflight.settings, "APP_ENV", "dev", raising=False)
    monkeypatch.setattr(preflight.settings, "MESSENGER_WEBHOOK_ENABLED", True, raising=False)
    monkeypatch.setattr(preflight.settings, "MESSENGER_PUBLIC_BASE_URL", "https://bot.example.test", raising=False)
    monkeypatch.setattr(preflight.settings, "VK_GROUP_TOKEN", "group-token", raising=False)
    monkeypatch.setattr(preflight.settings, "VK_CONFIRMATION_TOKEN", "confirm", raising=False)
    monkeypatch.setattr(preflight.settings, "VK_GROUP_ID", "123", raising=False)
    monkeypatch.setattr(preflight.settings, "VK_SECRET", "", raising=False)
    monkeypatch.setattr(preflight.settings, "VK_API_VERSION", "5.199", raising=False)

    status = preflight.check_vk_preflight()

    assert status.ok is True
    assert status.missing == ()
    assert status.warnings == ("VK_SECRET is not configured; VK webhook secret verification is not enforced",)
    assert status.details == {
        "enabled": True,
        "webhook_url": "https://bot.example.test/webhooks/vk",
        "api_version": "5.199",
        "callback_ack_enabled": True,
    }


def test_vk_preflight_requires_secret_for_prod_webhook(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.delenv("VK_WEBHOOK_ENABLED", raising=False)
    monkeypatch.setattr(preflight.settings, "APP_ENV", "prod", raising=False)
    monkeypatch.setattr(preflight.settings, "MESSENGER_WEBHOOK_ENABLED", True, raising=False)
    monkeypatch.setattr(preflight.settings, "MESSENGER_PUBLIC_BASE_URL", "https://bot.example.test", raising=False)
    monkeypatch.setattr(preflight.settings, "VK_GROUP_TOKEN", "group-token", raising=False)
    monkeypatch.setattr(preflight.settings, "VK_CONFIRMATION_TOKEN", "confirm", raising=False)
    monkeypatch.setattr(preflight.settings, "VK_GROUP_ID", "123", raising=False)
    monkeypatch.setattr(preflight.settings, "VK_SECRET", "", raising=False)

    status = preflight.check_vk_preflight()

    assert status.ok is False
    assert "VK_SECRET" in status.missing


def test_vk_preflight_rejects_non_positive_group_id(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("VK_WEBHOOK_ENABLED", "1")
    monkeypatch.setenv("VK_GROUP_TOKEN", "group-token")
    monkeypatch.setenv("VK_CONFIRMATION_TOKEN", "confirm")
    monkeypatch.setenv("VK_SECRET", "secret")
    monkeypatch.setenv("VK_GROUP_ID", "not-a-group")
    monkeypatch.setenv("MESSENGER_PUBLIC_BASE_URL", "https://bot.example.test")

    status = preflight.check_vk_preflight()

    assert status.ok is False
    assert "VK_GROUP_ID(valid positive integer)" in status.missing


def test_max_preflight_requires_public_base_when_webhook_enabled(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.delenv("MAX_WEBHOOK_ENABLED", raising=False)
    monkeypatch.setattr(preflight.settings, "APP_ENV", "dev", raising=False)
    monkeypatch.setattr(preflight.settings, "MESSENGER_WEBHOOK_ENABLED", True, raising=False)
    monkeypatch.setattr(preflight.settings, "MESSENGER_PUBLIC_BASE_URL", "", raising=False)
    monkeypatch.setattr(preflight.settings, "MAX_BOT_TOKEN", "max-token", raising=False)
    monkeypatch.setattr(preflight.settings, "MAX_BOT_LINK_BASE", "https://max.ru/bot", raising=False)
    monkeypatch.setattr(preflight.settings, "MAX_API_BASE_URL", "https://platform-api.max.ru", raising=False)

    status = preflight.check_max_preflight()

    assert status.ok is False
    assert "MESSENGER_PUBLIC_BASE_URL" in status.missing


def test_max_preflight_requires_secret_for_prod_webhook(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.delenv("MAX_WEBHOOK_ENABLED", raising=False)
    monkeypatch.setattr(preflight.settings, "APP_ENV", "prod", raising=False)
    monkeypatch.setattr(preflight.settings, "MESSENGER_WEBHOOK_ENABLED", True, raising=False)
    monkeypatch.setattr(preflight.settings, "MESSENGER_PUBLIC_BASE_URL", "https://bot.example.test", raising=False)
    monkeypatch.setattr(preflight.settings, "MAX_BOT_TOKEN", "max-token", raising=False)
    monkeypatch.setattr(preflight.settings, "MAX_BOT_LINK_BASE", "https://max.ru/bot", raising=False)
    monkeypatch.setattr(preflight.settings, "MAX_API_BASE_URL", "https://platform-api.max.ru", raising=False)
    monkeypatch.setattr(preflight.settings, "MAX_WEBHOOK_SECRET", "", raising=False)

    status = preflight.check_max_preflight()

    assert status.ok is False
    assert "MAX_WEBHOOK_SECRET" in status.missing
