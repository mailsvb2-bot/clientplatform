from __future__ import annotations

import importlib


def _reload_setup(monkeypatch, **env):
    keys = {
        "APP_ENV",
        "TELEGRAM_BOT_USERNAME",
        "CLIENTPLATFORM_PRODUCTION_BOT_USERNAME",
        "MAX_BOT_LINK_BASE",
        "MAX_BOT_TOKEN",
        "MAX_BOT_NAME",
        "MAX_WEBHOOK_ENABLED",
        "VK_GROUP_ID",
        "VK_GROUP_TOKEN",
        "VK_CONFIRMATION_TOKEN",
        "VK_SECRET",
        "VK_WEBHOOK_ENABLED",
        "MESSENGER_PUBLIC_BASE_URL",
        "MESSENGER_WEBHOOK_ENABLED",
        "TELEGRAM_TRANSPORT",
        "TELEGRAM_WEBHOOK_ENABLED",
        "TELEGRAM_WEBHOOK_PUBLIC_BASE_URL",
    }
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    settings_mod = importlib.import_module("config.settings")
    ingress_flags_mod = importlib.import_module("runtime.ingress_flags")
    setup_mod = importlib.import_module("services.messenger.setup")
    importlib.reload(settings_mod)
    importlib.reload(ingress_flags_mod)
    importlib.reload(setup_mod)
    return setup_mod


def test_vk_and_max_setup_status_is_ready_with_required_env(monkeypatch):
    setup_mod = _reload_setup(
        monkeypatch,
        TELEGRAM_BOT_USERNAME="metrotherapybot",
        MAX_BOT_LINK_BASE="https://max.ru/bot/{payload}",
        MAX_BOT_TOKEN="max-token",
        MAX_BOT_NAME="metrotherapy",
        VK_GROUP_ID="238191212",
        VK_GROUP_TOKEN="vk-token",
        VK_CONFIRMATION_TOKEN="confirm-token",
        MESSENGER_PUBLIC_BASE_URL="https://metrotherapy.ru",
        MESSENGER_WEBHOOK_ENABLED="1",
    )

    status = setup_mod.build_setup_status()

    assert status.max_ok is True
    assert status.vk_ok is True
    assert status.webhook_runtime_ok is True
    assert status.vk_webhook_url == "https://metrotherapy.ru/webhooks/vk"
    assert status.max_webhook_url == "https://metrotherapy.ru/webhooks/max"
    assert status.missing == ()


def test_vk_setup_is_not_ready_without_confirmation_token(monkeypatch):
    setup_mod = _reload_setup(
        monkeypatch,
        TELEGRAM_BOT_USERNAME="metrotherapybot",
        MAX_BOT_LINK_BASE="https://max.ru/bot/{payload}",
        MAX_BOT_TOKEN="max-token",
        VK_GROUP_ID="238191212",
        VK_GROUP_TOKEN="vk-token",
        MESSENGER_PUBLIC_BASE_URL="https://metrotherapy.ru",
        MESSENGER_WEBHOOK_ENABLED="1",
    )

    status = setup_mod.build_setup_status()

    assert status.vk_ok is False
    assert "VK_CONFIRMATION_TOKEN" in status.missing


def test_max_setup_warns_when_link_base_has_no_payload_placeholder(monkeypatch):
    setup_mod = _reload_setup(
        monkeypatch,
        TELEGRAM_BOT_USERNAME="metrotherapybot",
        MAX_BOT_LINK_BASE="https://max.ru/metrotherapy",
        MAX_BOT_TOKEN="max-token",
        VK_GROUP_ID="238191212",
        VK_GROUP_TOKEN="vk-token",
        VK_CONFIRMATION_TOKEN="confirm-token",
        MESSENGER_PUBLIC_BASE_URL="https://metrotherapy.ru",
        MESSENGER_WEBHOOK_ENABLED="1",
    )

    status = setup_mod.build_setup_status()

    assert status.max_ok is True
    assert any("{payload}" in warning for warning in status.warnings)


def test_public_base_url_must_be_full_url(monkeypatch):
    setup_mod = _reload_setup(
        monkeypatch,
        TELEGRAM_BOT_USERNAME="metrotherapybot",
        MAX_BOT_LINK_BASE="https://max.ru/bot/{payload}",
        MAX_BOT_TOKEN="max-token",
        VK_GROUP_ID="238191212",
        VK_GROUP_TOKEN="vk-token",
        VK_CONFIRMATION_TOKEN="confirm-token",
        MESSENGER_PUBLIC_BASE_URL="metrotherapy.ru",
        MESSENGER_WEBHOOK_ENABLED="1",
    )

    status = setup_mod.build_setup_status()

    assert any("MESSENGER_PUBLIC_BASE_URL" in warning for warning in status.warnings)


def test_setup_accepts_canonical_production_telegram_username(monkeypatch):
    setup_mod = _reload_setup(
        monkeypatch,
        CLIENTPLATFORM_PRODUCTION_BOT_USERNAME="clientplatform_bot",
        TELEGRAM_TRANSPORT="polling",
    )

    status = setup_mod.build_setup_status()

    assert status.telegram_ok is True
    assert "TELEGRAM_BOT_USERNAME" not in status.missing


def test_max_setup_fails_closed_when_bot_placeholder_has_no_bot_name(monkeypatch):
    setup_mod = _reload_setup(
        monkeypatch,
        TELEGRAM_BOT_USERNAME="metrotherapybot",
        MAX_WEBHOOK_ENABLED="1",
        MAX_BOT_LINK_BASE="https://max.ru/{bot}?payload={payload}",
        MAX_BOT_TOKEN="max-token",
        MESSENGER_PUBLIC_BASE_URL="https://metrotherapy.ru",
    )

    status = setup_mod.build_setup_status()

    assert status.max_ok is False
    assert "MAX_BOT_NAME" in status.missing
