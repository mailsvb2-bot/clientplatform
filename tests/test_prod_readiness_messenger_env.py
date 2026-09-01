from __future__ import annotations

import importlib


def _run(monkeypatch, **env):
    keys = {
        "APP_ENV",
        "BOT_TOKEN",
        "ADMIN_IDS",
        "ADMIN_ID",
        "HEALTHCHECK_ENABLED",
        "TELEGRAM_TRANSPORT",
        "TELEGRAM_WEBHOOK_ENABLED",
        "MESSENGER_WEBHOOK_ENABLED",
        "MESSENGER_PUBLIC_BASE_URL",
        "PRIVACY_EXPORT_HTTP_ENABLED",
        "PRIVACY_EXPORT_PUBLIC_BASE_URL",
        "PRIVACY_EXPORT_TOKEN_TTL_MINUTES",
        "MAX_WEBHOOK_ENABLED",
        "VK_WEBHOOK_ENABLED",
        "MAX_BOT_TOKEN",
        "MAX_BOT_LINK_BASE",
        "MAX_WEBHOOK_SECRET",
        "VK_GROUP_ID",
        "VK_GROUP_TOKEN",
        "VK_CONFIRMATION_TOKEN",
        "VK_SECRET",
    }
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    mod = importlib.import_module("scripts.prod_readiness_check")
    importlib.reload(mod)
    return mod.run()


def _base_dev_env() -> dict[str, str]:
    # Use dev mode here to test VK/MAX readiness contract without placing
    # token-shaped strings in the repository.
    return {
        "APP_ENV": "dev",
        "HEALTHCHECK_ENABLED": "1",
    }





def test_readiness_fails_when_enabled_vk_env_is_partial(monkeypatch):
    errors, warnings = _run(
        monkeypatch,
        **_base_dev_env(),
        VK_WEBHOOK_ENABLED="1",
        MESSENGER_PUBLIC_BASE_URL="https://clientplatform.ru",
        VK_GROUP_ID="238191212",
        VK_GROUP_TOKEN="vk-token-for-test",
    )

    assert any("VK_CONFIRMATION_TOKEN" in error for error in errors)


def test_readiness_accepts_complete_vk_and_max_env(monkeypatch):
    errors, warnings = _run(
        monkeypatch,
        **_base_dev_env(),
        MAX_WEBHOOK_ENABLED="1",
        VK_WEBHOOK_ENABLED="1",
        MESSENGER_PUBLIC_BASE_URL="https://clientplatform.ru",
        MAX_BOT_TOKEN="max-token-for-test",
        MAX_BOT_LINK_BASE="https://max.example/bot/{payload}",
        VK_GROUP_ID="238191212",
        VK_GROUP_TOKEN="vk-token-for-test",
        VK_CONFIRMATION_TOKEN="confirm-token-for-test",
        VK_SECRET="vk-secret-for-test",
    )

    assert not any("VK_" in error or "MAX_" in error for error in errors)
    assert not any("VK_SECRET" in warning for warning in warnings)


def test_readiness_warns_when_dev_vk_secret_absent(monkeypatch):
    errors, warnings = _run(
        monkeypatch,
        **_base_dev_env(),
        VK_WEBHOOK_ENABLED="1",
        MESSENGER_PUBLIC_BASE_URL="https://clientplatform.ru",
        VK_GROUP_ID="238191212",
        VK_GROUP_TOKEN="vk-token-for-test",
        VK_CONFIRMATION_TOKEN="confirm-token-for-test",
    )

    assert any("VK_SECRET" in warning for warning in warnings)



def test_readiness_requires_privacy_export_in_prod(monkeypatch):
    errors, warnings = _run(
        monkeypatch,
        APP_ENV="prod",
        HEALTHCHECK_ENABLED="1",
        PRIVACY_EXPORT_HTTP_ENABLED="0",
    )

    assert any("PRIVACY_EXPORT_HTTP_ENABLED" in error for error in errors)


def test_readiness_accepts_privacy_export_as_only_http_ingress(monkeypatch):
    errors, warnings = _run(
        monkeypatch,
        **_base_dev_env(),
        PRIVACY_EXPORT_HTTP_ENABLED="1",
        PRIVACY_EXPORT_PUBLIC_BASE_URL="https://clientplatform.ru",
        PRIVACY_EXPORT_TOKEN_TTL_MINUTES="10",
        MAX_WEBHOOK_ENABLED="0",
        VK_WEBHOOK_ENABLED="0",
    )

    assert not any("PRIVACY_EXPORT" in error or "privacy export" in error for error in errors)
    assert not any("HTTP ingress is disabled" in warning for warning in warnings)


def test_readiness_rejects_invalid_privacy_export_contract(monkeypatch):
    errors, warnings = _run(
        monkeypatch,
        **_base_dev_env(),
        PRIVACY_EXPORT_HTTP_ENABLED="1",
        PRIVACY_EXPORT_PUBLIC_BASE_URL="ftp://clientplatform.ru",
        PRIVACY_EXPORT_TOKEN_TTL_MINUTES="1",
    )

    assert any("full http(s) URL" in error for error in errors)
    assert any("PRIVACY_EXPORT_TOKEN_TTL_MINUTES" in error for error in errors)
